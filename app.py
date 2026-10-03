import os
import sqlite3
from datetime import date, timedelta
from functools import wraps

from flask import Flask, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

APP_NAME = "TripDesk"
DB_ENV = "TRIPDESK_DB"
DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tripdesk.db")

ROLE_OWNER = "owner"
ROLE_MANAGER = "branch_manager"
ROLE_AGENT = "agent"
ROLE_ACCOUNTANT = "accountant"
ROLES = (ROLE_OWNER, ROLE_MANAGER, ROLE_AGENT, ROLE_ACCOUNTANT)

PAGE_RULES = {
    "dashboard": {ROLE_OWNER, ROLE_MANAGER, ROLE_AGENT, ROLE_ACCOUNTANT},
    "clients": {ROLE_OWNER, ROLE_MANAGER, ROLE_AGENT},
    "trips": {ROLE_OWNER, ROLE_MANAGER, ROLE_AGENT},
    "payments": {ROLE_OWNER, ROLE_ACCOUNTANT, ROLE_MANAGER},
    "reminders": {ROLE_OWNER, ROLE_MANAGER, ROLE_AGENT},
    "reports": {ROLE_OWNER, ROLE_ACCOUNTANT},
}

NAV_ITEMS = [
    ("dashboard", "Dashboard", "M3 12l9-9 9 9M5 10v10h5v-6h4v6h5V10", "/"),
]


def create_app(test_db=None):
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "tripdesk-dev-key")
    app.config["DB_PATH"] = test_db or os.environ.get(DB_ENV, DEFAULT_DB)

    with app.app_context():
        init_db(app.config["DB_PATH"])

    @app.before_request
    def load_user():
        g.user = None
        g.branches = []
        if session.get("user_id"):
            db = get_db()
            g.user = db.execute(
                "SELECT u.*, b.name AS branch_name FROM users u "
                "LEFT JOIN branches b ON b.id = u.branch_id WHERE u.id = ?",
                (session["user_id"],),
            ).fetchone()
            if g.user is None:
                session.clear()
            else:
                g.branches = visible_branches(db, g.user)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if g.user:
            return redirect(url_for("dashboard"))
        error = None
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            db = get_db()
            user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            if user and check_password_hash(user["password_hash"], password):
                session.clear()
                session["user_id"] = user["id"]
                return redirect(url_for("dashboard"))
            error = "Email or password is incorrect."
        return render_template("login.html", error=error)

    @app.route("/logout", methods=["GET", "POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    def dashboard():
        if not g.user:
            return redirect(url_for("login"))
        if not page_allowed(g.user["role"], "dashboard"):
            return render_template("denied.html"), 403
        db = get_db()
        today = date.today().isoformat()
        branch_ids = visible_branch_ids(g.user)
        departures = query_departures(db, today, branch_ids)
        unpaid = query_unpaid(db, branch_ids)
        stats = query_branch_stats(db, today, branch_ids)
        totals = {
            "departures": len(departures),
            "unpaid_count": len(unpaid),
            "unpaid_total": round(sum(t["balance"] for t in unpaid), 2),
            "active_trips": sum(s["active_trips"] for s in stats),
        }
        return render_template(
            "dashboard.html",
            user=g.user,
            nav=NAV_ITEMS,
            today=today,
            departures=departures,
            unpaid=unpaid,
            stats=stats,
            totals=totals,
        )

    @app.route("/health")
    def health():
        return {"ok": True}

    app.teardown_appcontext(close_db)

    return app


def page_allowed(role, page):
    return role in PAGE_RULES.get(page, set())


def require_page(page):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not g.user:
                return redirect(url_for("login"))
            if not page_allowed(g.user["role"], page):
                return render_template("denied.html"), 403
            return view(*args, **kwargs)

        return wrapped

    return decorator


def visible_branch_ids(user):
    if user["role"] == ROLE_OWNER:
        return None
    return [user["branch_id"]] if user["branch_id"] else []


def visible_branches(db, user):
    if user["role"] == ROLE_OWNER:
        return db.execute("SELECT * FROM branches ORDER BY name").fetchall()
    if user["branch_id"]:
        return db.execute("SELECT * FROM branches WHERE id = ?", (user["branch_id"],)).fetchall()
    return []


def branch_filter(branch_ids, column="t.branch_id"):
    if branch_ids is None:
        return "", []
    if not branch_ids:
        return " AND 1 = 0 ", []
    marks = ",".join("?" for _ in branch_ids)
    return f" AND {column} IN ({marks}) ", list(branch_ids)


def query_departures(db, today, branch_ids):
    extra, params = branch_filter(branch_ids)
    return db.execute(
        "SELECT t.*, c.full_name AS client_name, b.name AS branch_name, u.name AS agent_name "
        "FROM trips t JOIN clients c ON c.id = t.client_id "
        "JOIN branches b ON b.id = t.branch_id "
        "LEFT JOIN users u ON u.id = t.agent_id "
        f"WHERE t.departure_date = ? AND t.status != 'cancelled'{extra} "
        "ORDER BY c.full_name",
        [today, *params],
    ).fetchall()


def query_unpaid(db, branch_ids):
    extra, params = branch_filter(branch_ids)
    rows = db.execute(
        "SELECT t.*, c.full_name AS client_name, b.name AS branch_name, "
        "COALESCE((SELECT SUM(CASE WHEN p.kind = 'refund' THEN -p.amount ELSE p.amount END) "
        "FROM payments p WHERE p.trip_id = t.id), 0) AS paid "
        "FROM trips t JOIN clients c ON c.id = t.client_id "
        "JOIN branches b ON b.id = t.branch_id "
        f"WHERE t.status != 'cancelled'{extra} ORDER BY t.departure_date",
        params,
    ).fetchall()
    result = []
    for row in rows:
        balance = round(row["total_price"] - (row["paid"] or 0), 2)
        if balance > 0.50:
            item = dict(row)
            item["balance"] = balance
            item["paid"] = round(row["paid"] or 0, 2)
            result.append(item)
    return result


def query_branch_stats(db, today, branch_ids):
    extra, params = branch_filter(branch_ids, column="t.branch_id")
    month = date.today().strftime("%Y-%m")
    rows = db.execute(
        "SELECT b.id, b.name, "
        "SUM(CASE WHEN t.status IN ('pending','confirmed') THEN 1 ELSE 0 END) AS active_trips, "
        "SUM(CASE WHEN t.departure_date = ? AND t.status != 'cancelled' THEN 1 ELSE 0 END) AS departures_today, "
        "COALESCE(SUM(CASE WHEN strftime('%Y-%m', p.created_on) = ? AND p.kind != 'refund' THEN p.amount ELSE 0 END), 0) AS collected_month "
        "FROM branches b LEFT JOIN trips t ON t.branch_id = b.id "
        "LEFT JOIN payments p ON p.trip_id = t.id "
        f"WHERE 1 = 1{extra.replace('t.branch_id', 'b.id') if extra else ''} "
        "GROUP BY b.id, b.name ORDER BY b.name",
        ([today, month, *params] if extra else [today, month]),
    ).fetchall()
    stats = []
    for row in rows:
        outstanding = outstanding_for_branch(db, row["id"])
        stats.append(
            {
                "id": row["id"],
                "name": row["name"],
                "active_trips": row["active_trips"] or 0,
                "departures_today": row["departures_today"] or 0,
                "collected_month": round(row["collected_month"] or 0, 2),
                "outstanding": outstanding,
            }
        )
    return stats


def outstanding_for_branch(db, branch_id):
    rows = db.execute(
        "SELECT t.total_price, COALESCE((SELECT SUM(CASE WHEN p.kind = 'refund' THEN -p.amount ELSE p.amount END) "
        "FROM payments p WHERE p.trip_id = t.id), 0) AS paid "
        "FROM trips t WHERE t.branch_id = ? AND t.status != 'cancelled'",
        (branch_id,),
    ).fetchall()
    total = 0.0
    for row in rows:
        balance = (row["total_price"] or 0) - (row["paid"] or 0)
        if balance > 0.50:
            total += balance
    return round(total, 2)


def get_db():
    from flask import current_app

    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DB_PATH"])
        g.db.row_factory = sqlite3.Row
    return g.db


def close_db(exc=None):
    from flask import g as flask_g

    db = flask_g.pop("db", None)
    if db is not None:
        db.close()


def init_db(path):
    fresh = not os.path.exists(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    needs_seed = fresh or conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    if needs_seed:
        seed(conn)
    conn.close()


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS branches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    address TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL,
    branch_id INTEGER REFERENCES branches(id)
);
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL,
    email TEXT NOT NULL,
    phone TEXT NOT NULL,
    address TEXT NOT NULL,
    passport_number TEXT,
    passport_expiry TEXT,
    branch_id INTEGER REFERENCES branches(id),
    agent_id INTEGER REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS suppliers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    phone TEXT,
    email TEXT
);
CREATE TABLE IF NOT EXISTS trips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    branch_id INTEGER NOT NULL REFERENCES branches(id),
    agent_id INTEGER REFERENCES users(id),
    title TEXT NOT NULL,
    departure_date TEXT NOT NULL,
    return_date TEXT NOT NULL,
    total_price REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS itinerary_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    item_date TEXT NOT NULL,
    price REAL NOT NULL DEFAULT 0,
    supplier_id INTEGER REFERENCES suppliers(id),
    voucher TEXT
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    amount REAL NOT NULL,
    kind TEXT NOT NULL DEFAULT 'deposit',
    method TEXT NOT NULL DEFAULT 'card',
    receipt_no TEXT NOT NULL,
    created_on TEXT NOT NULL,
    received_by INTEGER REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER REFERENCES trips(id) ON DELETE CASCADE,
    client_id INTEGER REFERENCES clients(id),
    kind TEXT NOT NULL,
    channel TEXT NOT NULL,
    message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ready',
    due_date TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id),
    action TEXT NOT NULL,
    entity TEXT NOT NULL,
    entity_id INTEGER,
    created_at TEXT NOT NULL
);
"""


def seed(conn):
    today = date.today()
    branches = [
        ("Downtown Branch", "(512) 555-0104", "410 Congress Ave, Suite 200"),
        ("North Branch", "(512) 555-0117", "8200 Burnet Rd, Suite 110"),
        ("West Branch", "(512) 555-0142", "3300 Bee Caves Rd, Suite 50"),
    ]
    branch_ids = []
    for name, phone, address in branches:
        cur = conn.execute(
            "INSERT INTO branches (name, phone, address) VALUES (?, ?, ?)", (name, phone, address)
        )
        branch_ids.append(cur.lastrowid)

    users = [
        ("Maya Coleman", "owner@tripdesk.local", ROLE_OWNER, None),
        ("Daniel Reyes", "manager@tripdesk.local", ROLE_MANAGER, branch_ids[0]),
        ("Priya Nair", "agent@tripdesk.local", ROLE_AGENT, branch_ids[0]),
        ("Tom Becker", "accountant@tripdesk.local", ROLE_ACCOUNTANT, branch_ids[0]),
        ("Sofia Grant", "manager2@tripdesk.local", ROLE_MANAGER, branch_ids[1]),
        ("Leo Martinez", "agent2@tripdesk.local", ROLE_AGENT, branch_ids[1]),
        ("Grace Liu", "agent3@tripdesk.local", ROLE_AGENT, branch_ids[2]),
    ]
    user_ids = {}
    for name, email, role, branch in users:
        cur = conn.execute(
            "INSERT INTO users (name, email, password_hash, role, branch_id) VALUES (?, ?, ?, ?, ?)",
            (name, email, generate_password_hash("Passw0rd!"), role, branch),
        )
        user_ids[email] = cur.lastrowid

    suppliers = [
        ("Skyline Air", "airline", "(512) 555-0131", "book@skylineair.example"),
        ("Harborview Hotel Group", "hotel", "(512) 555-0155", "stay@harborview.example"),
        ("Bluebonnet Tours", "tour", "(512) 555-0178", "hello@bluebonnet.example"),
    ]
    supplier_ids = []
    for name, kind, phone, email in suppliers:
        cur = conn.execute(
            "INSERT INTO suppliers (name, kind, phone, email) VALUES (?, ?, ?, ?)",
            (name, kind, phone, email),
        )
        supplier_ids.append(cur.lastrowid)

    clients = [
        ("Hannah Whitfield", "h.whitfield@example.com", "(512) 555-0142", branch_ids[0], 30),
        ("Marcus Bell", "m.bell@example.com", "(512) 555-0160", branch_ids[0], 400),
        ("Elena Rodriguez", "e.rodriguez@example.com", "(512) 555-0129", branch_ids[0], 200),
        ("James Carter", "j.carter@example.com", "(512) 555-0173", branch_ids[1], 90),
        ("Olivia Bennett", "o.bennett@example.com", "(512) 555-0191", branch_ids[1], 500),
        ("Nathan Price", "n.price@example.com", "(512) 555-0118", branch_ids[2], 45),
        ("Rachel Kim", "r.kim@example.com", "(512) 555-0136", branch_ids[2], 300),
        ("Samuel Ortiz", "s.ortiz@example.com", "(512) 555-0152", branch_ids[0], 120),
    ]
    client_ids = []
    for i, (name, email, phone, branch, expiry_days) in enumerate(clients):
        expiry = (today + timedelta(days=expiry_days)).isoformat()
        cur = conn.execute(
            "INSERT INTO clients (full_name, email, phone, address, passport_number, passport_expiry, branch_id, agent_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                name,
                email,
                phone,
                f"{120 + i * 37} Palm Ct, Apt {i + 1}",
                f"X{4102000 + i * 137}",
                expiry,
                branch,
                user_ids["agent@tripdesk.local"],
            ),
        )
        client_ids.append(cur.lastrowid)

    def iso(delta):
        return (today + timedelta(days=delta)).isoformat()

    trips = [
        (client_ids[0], branch_ids[0], user_ids["agent@tripdesk.local"], "Family beach getaway", 0, 7, 4850.00, "confirmed", 1200.00),
        (client_ids[1], branch_ids[0], user_ids["agent@tripdesk.local"], "Anniversary city break", 0, 4, 3200.00, "confirmed", 3200.00),
        (client_ids[3], branch_ids[1], user_ids["agent2@tripdesk.local"], "Lakeside retreat", 0, 5, 2740.00, "pending", 500.00),
        (client_ids[2], branch_ids[0], user_ids["agent@tripdesk.local"], "Mountain lodge escape", 2, 9, 5100.00, "confirmed", 1500.00),
        (client_ids[5], branch_ids[2], user_ids["agent3@tripdesk.local"], "Desert stargazing trip", 3, 8, 1980.00, "pending", 0.00),
        (client_ids[4], branch_ids[1], user_ids["agent2@tripdesk.local"], "Heritage rail journey", 12, 18, 3560.00, "confirmed", 3560.00),
        (client_ids[6], branch_ids[2], user_ids["agent3@tripdesk.local"], "Coastal food tour", -20, -14, 2430.00, "completed", 2430.00),
        (client_ids[7], branch_ids[0], user_ids["agent@tripdesk.local"], "Island honeymoon", 21, 28, 6900.00, "pending", 1000.00),
    ]
    for i, (client, branch, agent, title, dep, ret, total, status, paid) in enumerate(trips):
        cur = conn.execute(
            "INSERT INTO trips (client_id, branch_id, agent_id, title, departure_date, return_date, total_price, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (client, branch, agent, title, iso(dep), iso(ret), total, status),
        )
        trip_id = cur.lastrowid
        conn.execute(
            "INSERT INTO itinerary_items (trip_id, kind, title, item_date, price, supplier_id, voucher)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (trip_id, "flight", f"{title} - flight", iso(dep), round(total * 0.45, 2), supplier_ids[0], f"SKY-{1000 + i}"),
        )
        conn.execute(
            "INSERT INTO itinerary_items (trip_id, kind, title, item_date, price, supplier_id, voucher)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (trip_id, "hotel", f"{title} - stay", iso(dep + 1), round(total * 0.40, 2), supplier_ids[1], f"HTL-{2000 + i}"),
        )
        conn.execute(
            "INSERT INTO itinerary_items (trip_id, kind, title, item_date, price, supplier_id, voucher)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (trip_id, "tour", f"{title} - guided tour", iso(dep + 2), round(total * 0.15, 2), supplier_ids[2], f"TUR-{3000 + i}"),
        )
        if paid:
            conn.execute(
                "INSERT INTO payments (trip_id, amount, kind, method, receipt_no, created_on, received_by)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (trip_id, paid, "deposit", "card", f"R-{5100 + i}", iso(-6 if dep <= 0 else -2), user_ids["accountant@tripdesk.local"]),
            )

    reminders = [
        (1, client_ids[0], "deposit", "sms", "Balance due reminder", 5),
        (3, client_ids[3], "departure", "email", "Departure in 2 days", 2),
        (1, client_ids[0], "passport", "sms", "Passport expiring soon", 30),
    ]
    for trip_offset, client, kind, channel, text, due in reminders:
        conn.execute(
            "INSERT INTO reminders (trip_id, client_id, kind, channel, message, status, due_date)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (trip_offset, client, kind, channel, text, "ready", iso(due)),
        )

    conn.execute(
        "INSERT INTO audit_log (user_id, action, entity, entity_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_ids["owner@tripdesk.local"], "seed", "database", None, today.isoformat()),
    )
    conn.commit()


if __name__ == "__main__":
    application = create_app()
    port = int(os.environ.get("PORT", "5000"))
    application.run(host="127.0.0.1", port=port)
