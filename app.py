import os
import re
import secrets
import sqlite3
from datetime import date, timedelta
from functools import wraps

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for
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

TRIP_STATUSES = ("pending", "confirmed", "completed", "cancelled")
ITEM_KINDS = ("flight", "hotel", "tour", "transfer", "other")
PAYMENT_KINDS = ("deposit", "payment", "refund")
PAYMENT_METHODS = ("card", "cash", "transfer", "check")
REMINDER_KINDS = ("deposit", "departure", "passport")
REMINDER_CHANNELS = ("sms", "email")
REMINDER_STATUSES = ("ready", "sent")
COMMISSION_RATE = 0.05
PASSPORT_SOON_DAYS = 90
PHONE_PATTERN = re.compile(r"^\(\d{3}\) \d{3}-\d{4}$")
PHONE_HINT = "Use the (512) 555-0142 format."


def build_nav(role, active):
    entries = [("dashboard", "Dashboard", "/", "M3 12l9-9 9 9M5 10v10h5v-6h4v6h5V10")]
    if page_allowed(role, "clients"):
        entries.append(
            (
                "clients",
                "Clients",
                "/clients",
                "M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75",
            )
        )
    if page_allowed(role, "trips"):
        entries.append(("trips", "Trips", "/trips", "M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z"))
    if page_allowed(role, "payments"):
        entries.append(
            (
                "payments",
                "Payments",
                "/payments",
                "M3 10h18M7 15h4M5 19h14a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2z",
            )
        )
    if page_allowed(role, "reminders"):
        entries.append(
            (
                "reminders",
                "Reminders",
                "/reminders",
                "M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9M10.3 21a2 2 0 0 0 3.4 0",
            )
        )
    if page_allowed(role, "reports"):
        entries.append(
            (
                "reports",
                "Reports",
                "/reports",
                "M3 3v18h18M8 17V9m5 8V5m5 12v-6",
            )
        )
    return [
        {"key": key, "label": label, "url": url, "icon": icon, "current": key == active}
        for key, label, url, icon in entries
    ]


def parse_iso_day(value):
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def passport_info(expiry):
    day = parse_iso_day(expiry)
    if day is None:
        return {"label": "No passport on file", "tone": "missing", "days": None}
    delta = (day - date.today()).days
    if delta < 0:
        return {"label": "Expired " + day.isoformat(), "tone": "expired", "days": delta}
    if delta <= PASSPORT_SOON_DAYS:
        return {
            "label": "Expires " + day.isoformat() + " (" + str(delta) + " days)",
            "tone": "soon",
            "days": delta,
        }
    return {"label": "Valid to " + day.isoformat(), "tone": "ok", "days": delta}


def trip_money(db, trip_id, total_price):
    row = db.execute(
        "SELECT COALESCE(SUM(CASE WHEN kind = 'refund' THEN -amount ELSE amount END), 0) AS paid"
        " FROM payments WHERE trip_id = ?",
        (trip_id,),
    ).fetchone()
    paid = round(row["paid"] or 0, 2)
    return {"paid": paid, "balance": round((total_price or 0) - paid, 2)}


def refresh_trip_total(db, trip_id):
    row = db.execute(
        "SELECT COALESCE(SUM(price), 0) AS total FROM itinerary_items WHERE trip_id = ?",
        (trip_id,),
    ).fetchone()
    db.execute("UPDATE trips SET total_price = ? WHERE id = ?", (round(row["total"] or 0, 2), trip_id))
    db.commit()


def log_action(db, user_id, action, entity, entity_id):
    db.execute(
        "INSERT INTO audit_log (user_id, action, entity, entity_id, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (user_id, action, entity, entity_id, date.today().isoformat()),
    )
    db.commit()


def blank_client_values(user):
    return {
        "full_name": "",
        "email": "",
        "phone": "",
        "address": "",
        "passport_number": "",
        "passport_expiry": "",
        "branch_id": "" if user["role"] == ROLE_OWNER else str(user["branch_id"] or ""),
    }


def read_client_form(form, db, user):
    values = {
        "full_name": form.get("full_name", "").strip(),
        "email": form.get("email", "").strip(),
        "phone": form.get("phone", "").strip(),
        "address": form.get("address", "").strip(),
        "passport_number": form.get("passport_number", "").strip(),
        "passport_expiry": form.get("passport_expiry", "").strip(),
        "branch_id": form.get("branch_id", ""),
    }
    errors = {}
    if not values["full_name"]:
        errors["full_name"] = "Enter the client name."
    if "@" not in values["email"]:
        errors["email"] = "Enter a valid email."
    if not values["phone"]:
        errors["phone"] = "Enter a phone number."
    elif not PHONE_PATTERN.match(values["phone"]):
        errors["phone"] = PHONE_HINT
    if not values["address"]:
        errors["address"] = "Enter an address."
    if values["passport_expiry"] and parse_iso_day(values["passport_expiry"]) is None:
        errors["passport_expiry"] = "Use YYYY-MM-DD."
    if user["role"] == ROLE_OWNER:
        branch = db.execute("SELECT id FROM branches WHERE id = ?", (values["branch_id"],)).fetchone()
        if branch is None:
            errors["branch_id"] = "Choose a branch."
    else:
        values["branch_id"] = user["branch_id"]
        if not values["branch_id"]:
            errors["branch_id"] = "Your account has no branch."
    return values, errors


def scoped_client(db, user, client_id):
    branch_ids = visible_branch_ids(user)
    extra, params = branch_filter(branch_ids, column="c.branch_id")
    return db.execute(
        "SELECT c.*, b.name AS branch_name FROM clients c"
        " JOIN branches b ON b.id = c.branch_id"
        f" WHERE c.id = ?{extra}",
        [client_id, *params],
    ).fetchone()


def scoped_trip(db, user, trip_id):
    branch_ids = visible_branch_ids(user)
    extra, params = branch_filter(branch_ids)
    return db.execute(
        "SELECT t.*, c.full_name AS client_name, c.email AS client_email,"
        " c.phone AS client_phone, c.passport_number, c.passport_expiry,"
        " b.name AS branch_name, u.name AS agent_name FROM trips t"
        " JOIN clients c ON c.id = t.client_id"
        " JOIN branches b ON b.id = t.branch_id"
        " LEFT JOIN users u ON u.id = t.agent_id"
        f" WHERE t.id = ?{extra}",
        [trip_id, *params],
    ).fetchone()


def scoped_trip_clients(db, branch_ids):
    extra, params = branch_filter(branch_ids, column="c.branch_id")
    return db.execute(
        "SELECT c.*, b.name AS branch_name FROM clients c"
        " JOIN branches b ON b.id = c.branch_id"
        f" WHERE 1 = 1{extra} ORDER BY c.full_name",
        params,
    ).fetchall()


def scoped_trip_staff(db, branch_ids):
    extra, params = branch_filter(branch_ids, column="u.branch_id")
    return db.execute(
        "SELECT u.*, b.name AS branch_name FROM users u"
        " LEFT JOIN branches b ON b.id = u.branch_id"
        f" WHERE u.role IN ('agent', 'branch_manager'){extra} ORDER BY u.name",
        params,
    ).fetchall()


def validate_trip_dates(departure, return_day):
    errors = {}
    dep = parse_iso_day(departure)
    ret = parse_iso_day(return_day)
    if dep is None:
        errors["departure_date"] = "Enter a valid departure date."
    if ret is None:
        errors["return_date"] = "Enter a valid return date."
    if dep and ret and ret < dep:
        errors["return_date"] = "Return must be on or after departure."
    return errors


def validate_trip_form(values, clients, staff, user, branch_ids):
    errors = {}
    if not values["title"]:
        errors["title"] = "Enter a trip title."
    client_ids = {str(c["id"]): c for c in clients}
    client = client_ids.get(values["client_id"])
    if client is None:
        errors["client_id"] = "Choose a client."
    if user["role"] == ROLE_OWNER:
        if not values["branch_id"].isdigit():
            errors["branch_id"] = "Choose a branch."
    else:
        values["branch_id"] = str(user["branch_id"] or "")
        if branch_ids is not None and values["branch_id"] not in [str(b) for b in branch_ids]:
            errors["branch_id"] = "Choose your branch."
    if client is not None and not errors.get("branch_id"):
        if str(client["branch_id"]) != str(values["branch_id"]):
            errors["client_id"] = "Client belongs to another branch."
    if user["role"] == ROLE_AGENT:
        values["agent_id"] = str(user["id"])
    else:
        staff_ids = {str(s["id"]): s for s in staff}
        agent = staff_ids.get(values["agent_id"])
        if agent is None:
            errors["agent_id"] = "Choose an agent."
        elif user["role"] != ROLE_OWNER and str(agent["branch_id"]) != str(values["branch_id"]):
            errors["agent_id"] = "Agent belongs to another branch."
    errors.update(validate_trip_dates(values["departure_date"], values["return_date"]))
    return errors


def validate_item_form(values, suppliers):
    errors = {}
    if values["kind"] not in ITEM_KINDS:
        errors["kind"] = "Choose a type."
    if not values["title"]:
        errors["title"] = "Enter a title."
    if parse_iso_day(values["item_date"]) is None:
        errors["item_date"] = "Enter a valid date."
    try:
        price = float(values["price"])
        if price < 0:
            errors["price"] = "Price cannot be negative."
    except (TypeError, ValueError):
        errors["price"] = "Enter a price."
    if values["supplier_id"]:
        ids = {str(s["id"]) for s in suppliers}
        if values["supplier_id"] not in ids:
            errors["supplier_id"] = "Choose a supplier."
    return errors


def create_app(test_db=None):
    app = Flask(__name__)
    secret = os.environ.get("SECRET_KEY")
    if secret:
        app.secret_key = secret
    else:
        app.secret_key = secrets.token_hex(32)
        app.logger.warning("SECRET_KEY is not set; using a random key for this run.")
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
            nav=build_nav(g.user["role"], "dashboard"),
            today=today,
            departures=departures,
            unpaid=unpaid,
            stats=stats,
            totals=totals,
        )

    @app.route("/clients")
    @require_page("clients")
    def client_list():
        db = get_db()
        query = request.args.get("q", "").strip()
        branch_param = request.args.get("branch", "")
        branch_ids = visible_branch_ids(g.user)
        clauses = []
        params = []
        active_branch = ""
        if branch_ids is None:
            if branch_param.isdigit():
                clauses.append("c.branch_id = ?")
                params.append(int(branch_param))
                active_branch = branch_param
        else:
            extra, branch_params = branch_filter(branch_ids, column="c.branch_id")
            clauses.append("1 = 1" + extra)
            params.extend(branch_params)
        if query:
            clauses.append("(c.full_name LIKE ? OR c.email LIKE ? OR c.phone LIKE ?)")
            params.extend(["%" + query + "%"] * 3)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = db.execute(
            "SELECT c.*, b.name AS branch_name,"
            " (SELECT COUNT(*) FROM trips t WHERE t.client_id = c.id) AS trip_count"
            " FROM clients c JOIN branches b ON b.id = c.branch_id "
            + where
            + " ORDER BY c.full_name",
            params,
        ).fetchall()
        clients = []
        for row in rows:
            item = dict(row)
            item["passport"] = passport_info(row["passport_expiry"])
            clients.append(item)
        return render_template(
            "clients/list.html",
            user=g.user,
            nav=build_nav(g.user["role"], "clients"),
            clients=clients,
            q=query,
            branches=g.branches,
            active_branch=active_branch,
            can_filter=g.user["role"] == ROLE_OWNER,
        )

    @app.route("/clients/new", methods=["GET", "POST"])
    @require_page("clients")
    def client_new():
        db = get_db()
        values = blank_client_values(g.user)
        errors = {}
        if request.method == "POST":
            values, errors = read_client_form(request.form, db, g.user)
            if not errors:
                cur = db.execute(
                    "INSERT INTO clients (full_name, email, phone, address,"
                    " passport_number, passport_expiry, branch_id, agent_id)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        values["full_name"],
                        values["email"],
                        values["phone"],
                        values["address"],
                        values["passport_number"] or None,
                        values["passport_expiry"] or None,
                        values["branch_id"],
                        g.user["id"],
                    ),
                )
                db.commit()
                return redirect(url_for("client_detail", client_id=cur.lastrowid))
        return render_template(
            "clients/form.html",
            user=g.user,
            nav=build_nav(g.user["role"], "clients"),
            values=values,
            errors=errors,
            branches=g.branches,
            can_pick_branch=g.user["role"] == ROLE_OWNER,
            heading="New client",
            action=url_for("client_new"),
        )

    @app.route("/clients/<int:client_id>")
    @require_page("clients")
    def client_detail(client_id):
        db = get_db()
        client = scoped_client(db, g.user, client_id)
        if client is None:
            abort(404)
        trips = db.execute(
            "SELECT t.*, b.name AS branch_name, u.name AS agent_name FROM trips t"
            " JOIN branches b ON b.id = t.branch_id"
            " LEFT JOIN users u ON u.id = t.agent_id"
            " WHERE t.client_id = ? ORDER BY t.departure_date",
            (client_id,),
        ).fetchall()
        history = []
        for row in trips:
            item = dict(row)
            item.update(trip_money(db, row["id"], row["total_price"]))
            history.append(item)
        profile = dict(client)
        profile["passport"] = passport_info(client["passport_expiry"])
        return render_template(
            "clients/detail.html",
            user=g.user,
            nav=build_nav(g.user["role"], "clients"),
            client=profile,
            trips=history,
        )

    @app.route("/clients/<int:client_id>/edit", methods=["GET", "POST"])
    @require_page("clients")
    def client_edit(client_id):
        db = get_db()
        client = scoped_client(db, g.user, client_id)
        if client is None:
            abort(404)
        values = {
            "full_name": client["full_name"],
            "email": client["email"],
            "phone": client["phone"],
            "address": client["address"],
            "passport_number": client["passport_number"] or "",
            "passport_expiry": client["passport_expiry"] or "",
            "branch_id": str(client["branch_id"]),
        }
        errors = {}
        if request.method == "POST":
            values, errors = read_client_form(request.form, db, g.user)
            if not errors:
                db.execute(
                    "UPDATE clients SET full_name = ?, email = ?, phone = ?, address = ?,"
                    " passport_number = ?, passport_expiry = ?, branch_id = ? WHERE id = ?",
                    (
                        values["full_name"],
                        values["email"],
                        values["phone"],
                        values["address"],
                        values["passport_number"] or None,
                        values["passport_expiry"] or None,
                        values["branch_id"],
                        client_id,
                    ),
                )
                db.commit()
                return redirect(url_for("client_detail", client_id=client_id))
        return render_template(
            "clients/form.html",
            user=g.user,
            nav=build_nav(g.user["role"], "clients"),
            values=values,
            errors=errors,
            branches=g.branches,
            can_pick_branch=g.user["role"] == ROLE_OWNER,
            heading="Edit client",
            action=url_for("client_edit", client_id=client_id),
        )

    @app.route("/trips")
    @require_page("trips")
    def trip_list():
        db = get_db()
        query = request.args.get("q", "").strip()
        status = request.args.get("status", "")
        branch_param = request.args.get("branch", "")
        branch_ids = visible_branch_ids(g.user)
        clauses = []
        params = []
        active_branch = ""
        if branch_ids is None:
            if branch_param.isdigit():
                clauses.append("t.branch_id = ?")
                params.append(int(branch_param))
                active_branch = branch_param
        else:
            extra, branch_params = branch_filter(branch_ids)
            clauses.append("1 = 1" + extra)
            params.extend(branch_params)
        if status in TRIP_STATUSES:
            clauses.append("t.status = ?")
            params.append(status)
        if query:
            clauses.append("(t.title LIKE ? OR c.full_name LIKE ?)")
            params.extend(["%" + query + "%"] * 2)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = db.execute(
            "SELECT t.*, c.full_name AS client_name, b.name AS branch_name,"
            " u.name AS agent_name FROM trips t"
            " JOIN clients c ON c.id = t.client_id"
            " JOIN branches b ON b.id = t.branch_id"
            " LEFT JOIN users u ON u.id = t.agent_id " + where + " ORDER BY t.departure_date",
            params,
        ).fetchall()
        trips = []
        for row in rows:
            item = dict(row)
            item.update(trip_money(db, row["id"], row["total_price"]))
            trips.append(item)
        return render_template(
            "trips/list.html",
            user=g.user,
            nav=build_nav(g.user["role"], "trips"),
            trips=trips,
            q=query,
            status=status,
            statuses=TRIP_STATUSES,
            branches=g.branches,
            active_branch=active_branch,
            can_filter=g.user["role"] == ROLE_OWNER,
        )

    @app.route("/trips/new", methods=["GET", "POST"])
    @require_page("trips")
    def trip_new():
        db = get_db()
        branch_ids = visible_branch_ids(g.user)
        clients = scoped_trip_clients(db, branch_ids)
        staff = scoped_trip_staff(db, branch_ids)
        preset_client = request.args.get("client_id", "")
        week = (date.today() + timedelta(days=7)).isoformat()
        values = {
            "title": "",
            "client_id": preset_client,
            "branch_id": str(branch_ids[0]) if branch_ids else "",
            "agent_id": str(g.user["id"]),
            "departure_date": date.today().isoformat(),
            "return_date": week,
        }
        errors = {}
        if request.method == "POST":
            values = {
                "title": request.form.get("title", "").strip(),
                "client_id": request.form.get("client_id", ""),
                "branch_id": request.form.get("branch_id", ""),
                "agent_id": request.form.get("agent_id", ""),
                "departure_date": request.form.get("departure_date", "").strip(),
                "return_date": request.form.get("return_date", "").strip(),
            }
            errors = validate_trip_form(values, clients, staff, g.user, branch_ids)
            if not errors:
                cur = db.execute(
                    "INSERT INTO trips (client_id, branch_id, agent_id, title,"
                    " departure_date, return_date, total_price, status)"
                    " VALUES (?, ?, ?, ?, ?, ?, 0, 'pending')",
                    (
                        int(values["client_id"]),
                        int(values["branch_id"]),
                        int(values["agent_id"]),
                        values["title"],
                        values["departure_date"],
                        values["return_date"],
                    ),
                )
                db.commit()
                return redirect(url_for("trip_detail", trip_id=cur.lastrowid))
        branch_names = {str(b["id"]): b["name"] for b in g.branches}
        context = {
            "branch": branch_names.get(values["branch_id"], "All branches"),
            "clients": len(clients),
            "staff": len(staff),
        }
        return render_template(
            "trips/form.html",
            user=g.user,
            nav=build_nav(g.user["role"], "trips"),
            values=values,
            errors=errors,
            clients=clients,
            staff=staff,
            branches=g.branches,
            can_pick_branch=g.user["role"] == ROLE_OWNER,
            can_pick_agent=g.user["role"] in (ROLE_OWNER, ROLE_MANAGER),
            context=context,
        )

    @app.route("/trips/<int:trip_id>")
    @require_page("trips")
    def trip_detail(trip_id):
        db = get_db()
        trip = scoped_trip(db, g.user, trip_id)
        if trip is None:
            abort(404)
        items = db.execute(
            "SELECT i.*, s.name AS supplier_name, s.kind AS supplier_kind,"
            " s.phone AS supplier_phone FROM itinerary_items i"
            " LEFT JOIN suppliers s ON s.id = i.supplier_id"
            " WHERE i.trip_id = ? ORDER BY i.item_date",
            (trip_id,),
        ).fetchall()
        suppliers = db.execute("SELECT * FROM suppliers ORDER BY name").fetchall()
        record = dict(trip)
        record.update(trip_money(db, trip["id"], trip["total_price"]))
        record["items_total"] = round(sum(i["price"] or 0 for i in items), 2)
        record["passport"] = passport_info(trip["passport_expiry"])
        payments = db.execute(
            "SELECT p.*, u.name AS received_name FROM payments p"
            " LEFT JOIN users u ON u.id = p.received_by"
            " WHERE p.trip_id = ? ORDER BY p.created_on DESC, p.id DESC",
            (trip_id,),
        ).fetchall()
        return render_template(
            "trips/detail.html",
            user=g.user,
            nav=build_nav(g.user["role"], "trips"),
            trip=record,
            items=items,
            suppliers=suppliers,
            statuses=TRIP_STATUSES,
            kinds=ITEM_KINDS,
            can_change_status=g.user["role"] in (ROLE_OWNER, ROLE_MANAGER),
            payments=payments,
            can_record=page_allowed(g.user["role"], "payments"),
        )

    @app.route("/trips/<int:trip_id>/edit", methods=["POST"])
    @require_page("trips")
    def trip_edit(trip_id):
        db = get_db()
        trip = scoped_trip(db, g.user, trip_id)
        if trip is None:
            abort(404)
        title = request.form.get("title", "").strip()
        departure = request.form.get("departure_date", "").strip()
        return_day = request.form.get("return_date", "").strip()
        errors = validate_trip_dates(departure, return_day)
        if not title:
            errors["title"] = "Enter a trip title."
        if errors:
            items = db.execute(
                "SELECT i.*, s.name AS supplier_name, s.kind AS supplier_kind,"
                " s.phone AS supplier_phone FROM itinerary_items i"
                " LEFT JOIN suppliers s ON s.id = i.supplier_id"
                " WHERE i.trip_id = ? ORDER BY i.item_date",
                (trip_id,),
            ).fetchall()
            suppliers = db.execute("SELECT * FROM suppliers ORDER BY name").fetchall()
            record = dict(trip)
            record.update(trip_money(db, trip["id"], trip["total_price"]))
            record["items_total"] = round(sum(i["price"] or 0 for i in items), 2)
            record["passport"] = passport_info(trip["passport_expiry"])
            payments = db.execute(
                "SELECT p.*, u.name AS received_name FROM payments p"
                " LEFT JOIN users u ON u.id = p.received_by"
                " WHERE p.trip_id = ? ORDER BY p.created_on DESC, p.id DESC",
                (trip_id,),
            ).fetchall()
            return (
                render_template(
                    "trips/detail.html",
                    user=g.user,
                    nav=build_nav(g.user["role"], "trips"),
                    trip=record,
                    items=items,
                    suppliers=suppliers,
                    statuses=TRIP_STATUSES,
                    kinds=ITEM_KINDS,
                    can_change_status=g.user["role"] in (ROLE_OWNER, ROLE_MANAGER),
                    payments=payments,
                    can_record=page_allowed(g.user["role"], "payments"),
                    edit_errors=errors,
                    edit_values={"title": title or trip["title"], "departure_date": departure, "return_date": return_day},
                ),
                200,
            )
        db.execute(
            "UPDATE trips SET title = ?, departure_date = ?, return_date = ? WHERE id = ?",
            (title, departure, return_day, trip_id),
        )
        db.commit()
        return redirect(url_for("trip_detail", trip_id=trip_id))

    @app.route("/trips/<int:trip_id>/status", methods=["POST"])
    @require_page("trips")
    def trip_status(trip_id):
        if g.user["role"] not in (ROLE_OWNER, ROLE_MANAGER):
            return render_template("denied.html"), 403
        db = get_db()
        trip = scoped_trip(db, g.user, trip_id)
        if trip is None:
            abort(404)
        status = request.form.get("status", "")
        if status not in TRIP_STATUSES:
            return redirect(url_for("trip_detail", trip_id=trip_id))
        db.execute("UPDATE trips SET status = ? WHERE id = ?", (status, trip_id))
        log_action(db, g.user["id"], "status:" + status, "trip", trip_id)
        return redirect(url_for("trip_detail", trip_id=trip_id))

    @app.route("/trips/<int:trip_id>/items", methods=["POST"])
    @require_page("trips")
    def trip_item_add(trip_id):
        db = get_db()
        trip = scoped_trip(db, g.user, trip_id)
        if trip is None:
            abort(404)
        values = {
            "kind": request.form.get("kind", ""),
            "title": request.form.get("title", "").strip(),
            "item_date": request.form.get("item_date", "").strip(),
            "price": request.form.get("price", "").strip(),
            "supplier_id": request.form.get("supplier_id", ""),
            "voucher": request.form.get("voucher", "").strip(),
        }
        suppliers = db.execute("SELECT * FROM suppliers ORDER BY name").fetchall()
        errors = validate_item_form(values, suppliers)
        if errors:
            items = db.execute(
                "SELECT i.*, s.name AS supplier_name, s.kind AS supplier_kind,"
                " s.phone AS supplier_phone FROM itinerary_items i"
                " LEFT JOIN suppliers s ON s.id = i.supplier_id"
                " WHERE i.trip_id = ? ORDER BY i.item_date",
                (trip_id,),
            ).fetchall()
            record = dict(trip)
            record.update(trip_money(db, trip["id"], trip["total_price"]))
            record["items_total"] = round(sum(i["price"] or 0 for i in items), 2)
            record["passport"] = passport_info(trip["passport_expiry"])
            payments = db.execute(
                "SELECT p.*, u.name AS received_name FROM payments p"
                " LEFT JOIN users u ON u.id = p.received_by"
                " WHERE p.trip_id = ? ORDER BY p.created_on DESC, p.id DESC",
                (trip_id,),
            ).fetchall()
            return (
                render_template(
                    "trips/detail.html",
                    user=g.user,
                    nav=build_nav(g.user["role"], "trips"),
                    trip=record,
                    items=items,
                    suppliers=suppliers,
                    statuses=TRIP_STATUSES,
                    kinds=ITEM_KINDS,
                    can_change_status=g.user["role"] in (ROLE_OWNER, ROLE_MANAGER),
                    payments=payments,
                    can_record=page_allowed(g.user["role"], "payments"),
                    item_errors=errors,
                    item_values=values,
                ),
                200,
            )
        db.execute(
            "INSERT INTO itinerary_items (trip_id, kind, title, item_date, price, supplier_id, voucher)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                trip_id,
                values["kind"],
                values["title"],
                values["item_date"],
                float(values["price"]),
                int(values["supplier_id"]) if values["supplier_id"] else None,
                values["voucher"] or None,
            ),
        )
        db.commit()
        refresh_trip_total(db, trip_id)
        return redirect(url_for("trip_detail", trip_id=trip_id))

    @app.route("/trips/<int:trip_id>/items/<int:item_id>/delete", methods=["POST"])
    @require_page("trips")
    def trip_item_delete(trip_id, item_id):
        db = get_db()
        trip = scoped_trip(db, g.user, trip_id)
        if trip is None:
            abort(404)
        db.execute("DELETE FROM itinerary_items WHERE id = ? AND trip_id = ?", (item_id, trip_id))
        db.commit()
        refresh_trip_total(db, trip_id)
        return redirect(url_for("trip_detail", trip_id=trip_id))

    @app.route("/payments")
    @require_page("payments")
    def payment_list():
        db = get_db()
        query = request.args.get("q", "").strip()
        kind = request.args.get("kind", "")
        branch_param = request.args.get("branch", "")
        branch_ids = visible_branch_ids(g.user)
        clauses = []
        params = []
        active_branch = ""
        if branch_ids is None:
            if branch_param.isdigit():
                clauses.append("t.branch_id = ?")
                params.append(int(branch_param))
                active_branch = branch_param
        else:
            extra, branch_params = branch_filter(branch_ids)
            clauses.append("1 = 1" + extra)
            params.extend(branch_params)
        if kind in PAYMENT_KINDS:
            clauses.append("p.kind = ?")
            params.append(kind)
        if query:
            clauses.append("(t.title LIKE ? OR c.full_name LIKE ? OR p.receipt_no LIKE ?)")
            params.extend(["%" + query + "%"] * 3)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = db.execute(
            "SELECT p.*, t.title AS trip_title, t.total_price, t.departure_date,"
            " c.full_name AS client_name, b.name AS branch_name, u.name AS received_name"
            " FROM payments p JOIN trips t ON t.id = p.trip_id"
            " JOIN clients c ON c.id = t.client_id"
            " JOIN branches b ON b.id = t.branch_id"
            " LEFT JOIN users u ON u.id = p.received_by " + where + " ORDER BY p.created_on DESC, p.id DESC",
            params,
        ).fetchall()
        running = running_balances(db)
        payments = []
        for row in rows:
            item = dict(row)
            item["balance_after"] = round(running.get(row["id"], row["total_price"]), 2)
            payments.append(item)
        collected = round(sum(p["amount"] for p in payments if p["kind"] != "refund"), 2)
        refunded = round(sum(p["amount"] for p in payments if p["kind"] == "refund"), 2)
        return render_template(
            "payments/list.html",
            user=g.user,
            nav=build_nav(g.user["role"], "payments"),
            payments=payments,
            q=query,
            kind=kind,
            kinds=PAYMENT_KINDS,
            branches=g.branches,
            active_branch=active_branch,
            can_filter=g.user["role"] == ROLE_OWNER,
            collected=collected,
            refunded=refunded,
        )

    @app.route("/payments/new", methods=["GET", "POST"])
    @require_page("payments")
    def payment_new():
        db = get_db()
        branch_ids = visible_branch_ids(g.user)
        extra, params = branch_filter(branch_ids)
        trips = db.execute(
            "SELECT t.id, t.title, t.total_price, t.branch_id, c.full_name AS client_name,"
            " b.name AS branch_name,"
            " COALESCE((SELECT SUM(CASE WHEN p.kind = 'refund' THEN -p.amount ELSE p.amount END)"
            " FROM payments p WHERE p.trip_id = t.id), 0) AS paid"
            " FROM trips t JOIN clients c ON c.id = t.client_id"
            " JOIN branches b ON b.id = t.branch_id"
            f" WHERE t.status != 'cancelled'{extra} ORDER BY t.departure_date",
            params,
        ).fetchall()
        preset = request.args.get("trip_id", "")
        values = {
            "trip_id": preset,
            "amount": "",
            "kind": "deposit",
            "method": "card",
            "created_on": date.today().isoformat(),
            "receipt_no": next_receipt_no(db),
        }
        errors = {}
        if request.method == "POST":
            values = {
                "trip_id": request.form.get("trip_id", ""),
                "amount": request.form.get("amount", "").strip(),
                "kind": request.form.get("kind", ""),
                "method": request.form.get("method", ""),
                "created_on": request.form.get("created_on", "").strip(),
                "receipt_no": request.form.get("receipt_no", "").strip() or next_receipt_no(db),
            }
            errors = validate_payment_form(db, values, trips)
            if not errors:
                cur = db.execute(
                    "INSERT INTO payments (trip_id, amount, kind, method, receipt_no, created_on, received_by)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        int(values["trip_id"]),
                        round(float(values["amount"]), 2),
                        values["kind"],
                        values["method"],
                        values["receipt_no"],
                        values["created_on"],
                        g.user["id"],
                    ),
                )
                db.commit()
                log_action(db, g.user["id"], "record:" + values["kind"], "payment", cur.lastrowid)
                return redirect(url_for("payment_receipt", payment_id=cur.lastrowid))
        options = []
        for trip in trips:
            balance = round(trip["total_price"] - (trip["paid"] or 0), 2)
            option = dict(trip)
            option["balance"] = balance
            options.append(option)
        return render_template(
            "payments/form.html",
            user=g.user,
            nav=build_nav(g.user["role"], "payments"),
            values=values,
            errors=errors,
            trips=options,
            kinds=PAYMENT_KINDS,
            methods=PAYMENT_METHODS,
        )

    @app.route("/payments/<int:payment_id>/receipt")
    @require_page("payments")
    def payment_receipt(payment_id):
        db = get_db()
        payment = scoped_payment(db, g.user, payment_id)
        if payment is None:
            abort(404)
        record = dict(payment)
        record.update(trip_money(db, payment["trip_id"], payment["total_price"]))
        return render_template(
            "payments/receipt.html",
            user=g.user,
            nav=build_nav(g.user["role"], "payments"),
            payment=record,
        )

    @app.route("/reminders")
    @require_page("reminders")
    def reminder_list():
        db = get_db()
        kind = request.args.get("kind", "")
        channel = request.args.get("channel", "")
        status = request.args.get("status", "")
        branch_ids = visible_branch_ids(g.user)
        extra, params = branch_filter(branch_ids, column="COALESCE(t.branch_id, c.branch_id)")
        clauses = ["1 = 1" + extra]
        filter_params = list(params)
        if kind in REMINDER_KINDS:
            clauses.append("r.kind = ?")
            filter_params.append(kind)
        if channel in REMINDER_CHANNELS:
            clauses.append("r.channel = ?")
            filter_params.append(channel)
        if status in REMINDER_STATUSES:
            clauses.append("r.status = ?")
            filter_params.append(status)
        where = "WHERE " + " AND ".join(clauses)
        rows = db.execute(
            "SELECT r.*, c.full_name AS client_name, c.phone AS client_phone, c.email AS client_email,"
            " t.title AS trip_title, t.departure_date,"
            " COALESCE(t.branch_id, c.branch_id) AS branch_id,"
            " (SELECT b.name FROM branches b WHERE b.id = COALESCE(t.branch_id, c.branch_id)) AS branch_name"
            " FROM reminders r"
            " LEFT JOIN trips t ON t.id = r.trip_id"
            " LEFT JOIN clients c ON c.id = r.client_id " + where + " ORDER BY r.due_date, r.id",
            filter_params,
        ).fetchall()
        counts = db.execute(
            "SELECT r.status AS status, COUNT(*) AS n FROM reminders r"
            " LEFT JOIN trips t ON t.id = r.trip_id"
            " LEFT JOIN clients c ON c.id = r.client_id"
            f" WHERE 1 = 1{extra} GROUP BY r.status",
            params,
        ).fetchall()
        tally = {row["status"]: row["n"] for row in counts}
        return render_template(
            "reminders/list.html",
            user=g.user,
            nav=build_nav(g.user["role"], "reminders"),
            reminders=rows,
            kind=kind,
            kinds=REMINDER_KINDS,
            channel=channel,
            channels=REMINDER_CHANNELS,
            status=status,
            statuses=REMINDER_STATUSES,
            ready_count=tally.get("ready", 0),
            sent_count=tally.get("sent", 0),
        )

    @app.route("/reminders/generate", methods=["POST"])
    @require_page("reminders")
    def reminder_generate():
        db = get_db()
        selected = request.form.get("channel", "sms")
        if selected == "both":
            wanted = ("sms", "email")
        elif selected in REMINDER_CHANNELS:
            wanted = (selected,)
        else:
            wanted = ("sms",)
        created = build_reminder_drafts(db, visible_branch_ids(g.user), wanted)
        log_action(db, g.user["id"], "generate", "reminders", created)
        if created == 1:
            flash("Composed 1 reminder")
        else:
            flash("Composed " + str(created) + " reminders")
        return redirect(url_for("reminder_list"))

    @app.route("/reminders/<int:reminder_id>/sent", methods=["POST"])
    @require_page("reminders")
    def reminder_sent(reminder_id):
        db = get_db()
        reminder = scoped_reminder(db, g.user, reminder_id)
        if reminder is None:
            abort(404)
        db.execute("UPDATE reminders SET status = 'sent' WHERE id = ?", (reminder_id,))
        db.commit()
        return redirect(url_for("reminder_list"))

    @app.route("/reminders/<int:reminder_id>/delete", methods=["POST"])
    @require_page("reminders")
    def reminder_delete(reminder_id):
        db = get_db()
        reminder = scoped_reminder(db, g.user, reminder_id)
        if reminder is None:
            abort(404)
        db.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        db.commit()
        return redirect(url_for("reminder_list"))

    @app.route("/reports")
    @require_page("reports")
    def report():
        import calendar

        db = get_db()
        month = request.args.get("month", "").strip() or date.today().strftime("%Y-%m")
        try:
            year_num, month_num = (int(part) for part in month.split("-"))
            calendar.monthrange(year_num, month_num)
        except (ValueError, TypeError):
            month = date.today().strftime("%Y-%m")
            year_num, month_num = (int(part) for part in month.split("-"))
        label = calendar.month_name[month_num] + " " + str(year_num)
        branch_param = request.args.get("branch", "")
        branch_ids = visible_branch_ids(g.user)
        active_branch = ""
        report_branch_ids = branch_ids
        if branch_ids is None and branch_param.isdigit():
            report_branch_ids = [int(branch_param)]
            active_branch = branch_param
        revenue = query_revenue_by_branch(db, month, report_branch_ids)
        commissions = query_commissions(db, month, report_branch_ids)
        outstanding = []
        for branch in g.branches:
            if report_branch_ids is not None and branch["id"] not in report_branch_ids:
                continue
            outstanding.append(
                {"name": branch["name"], "amount": outstanding_for_branch(db, branch["id"])}
            )
        entity = month if not active_branch else month + ":branch-" + active_branch
        reviewed = (
            db.execute(
                "SELECT id FROM audit_log WHERE action = 'month-reviewed' AND entity = ?",
                (entity,),
            ).fetchone()
            is not None
        )
        totals = {
            "revenue": round(sum(r["revenue"] for r in revenue), 2),
            "refunds": round(sum(r["refunds"] for r in revenue), 2),
            "commission": round(sum(c["commission"] for c in commissions), 2),
            "outstanding": round(sum(o["amount"] for o in outstanding), 2),
        }
        return render_template(
            "reports/report.html",
            user=g.user,
            nav=build_nav(g.user["role"], "reports"),
            month=month,
            label=label,
            revenue=revenue,
            commissions=commissions,
            outstanding=outstanding,
            totals=totals,
            reviewed=reviewed,
            branches=g.branches,
            active_branch=active_branch,
            can_filter=g.user["role"] == ROLE_OWNER,
            rate=int(COMMISSION_RATE * 100),
        )

    @app.route("/reports/review", methods=["POST"])
    @require_page("reports")
    def report_review():
        if g.user["role"] != ROLE_OWNER:
            return render_template("denied.html"), 403
        db = get_db()
        month = request.form.get("month", "").strip() or date.today().strftime("%Y-%m")
        branch_param = request.form.get("branch", "")
        entity = month if not branch_param.isdigit() else month + ":branch-" + branch_param
        exists = db.execute(
            "SELECT id FROM audit_log WHERE action = 'month-reviewed' AND entity = ?",
            (entity,),
        ).fetchone()
        if exists is None:
            db.execute(
                "INSERT INTO audit_log (user_id, action, entity, entity_id, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (g.user["id"], "month-reviewed", entity, None, date.today().isoformat()),
            )
            db.commit()
            flash("Month locked as reviewed")
        else:
            flash("Month already locked as reviewed")
        target = url_for("report", month=month)
        if branch_param.isdigit():
            target += "&branch=" + branch_param
        return redirect(target)

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
        "COUNT(DISTINCT CASE WHEN t.status IN ('pending','confirmed') THEN t.id END) AS active_trips, "
        "COUNT(DISTINCT CASE WHEN t.departure_date = ? AND t.status != 'cancelled' THEN t.id END) AS departures_today, "
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


def next_receipt_no(db):
    numbers = []
    for row in db.execute("SELECT receipt_no FROM payments").fetchall():
        text = (row["receipt_no"] or "").strip()
        if text[:2].upper() == "R-" and text[2:].isdigit():
            numbers.append(int(text[2:]))
    return "R-" + str(max(numbers, default=5199) + 1)


def scoped_payment(db, user, payment_id):
    branch_ids = visible_branch_ids(user)
    extra, params = branch_filter(branch_ids)
    return db.execute(
        "SELECT p.*, t.title AS trip_title, t.total_price, t.departure_date, t.return_date,"
        " t.status AS trip_status, t.client_id, c.full_name AS client_name, c.email AS client_email,"
        " c.phone AS client_phone, b.name AS branch_name, b.phone AS branch_phone,"
        " u.name AS agent_name, r.name AS received_name FROM payments p"
        " JOIN trips t ON t.id = p.trip_id"
        " JOIN clients c ON c.id = t.client_id"
        " JOIN branches b ON b.id = t.branch_id"
        " LEFT JOIN users u ON u.id = t.agent_id"
        " LEFT JOIN users r ON r.id = p.received_by"
        f" WHERE p.id = ?{extra}",
        [payment_id, *params],
    ).fetchone()


def scoped_reminder(db, user, reminder_id):
    branch_ids = visible_branch_ids(user)
    extra, params = branch_filter(branch_ids, column="COALESCE(t.branch_id, c.branch_id)")
    return db.execute(
        "SELECT r.* FROM reminders r"
        " LEFT JOIN trips t ON t.id = r.trip_id"
        " LEFT JOIN clients c ON c.id = r.client_id"
        f" WHERE r.id = ?{extra}",
        [reminder_id, *params],
    ).fetchone()


def running_balances(db):
    paid = {}
    for row in db.execute(
        "SELECT p.id, p.trip_id, p.amount, p.kind, t.total_price FROM payments p"
        " JOIN trips t ON t.id = p.trip_id ORDER BY p.created_on, p.id"
    ).fetchall():
        signed = -row["amount"] if row["kind"] == "refund" else row["amount"]
        paid[row["trip_id"]] = paid.get(row["trip_id"], 0) + signed
        paid["after:" + str(row["id"])] = round(row["total_price"] - paid[row["trip_id"]], 2)
    return {
        int(key.split(":")[1]): value
        for key, value in paid.items()
        if isinstance(key, str) and key.startswith("after:")
    }


def validate_payment_form(db, values, trips):
    errors = {}
    options = {str(t["id"]): t for t in trips}
    trip = options.get(values["trip_id"])
    if trip is None:
        errors["trip_id"] = "Choose a trip."
    try:
        amount = float(values["amount"])
        if amount <= 0:
            errors["amount"] = "Enter an amount above zero."
    except (TypeError, ValueError):
        errors["amount"] = "Enter an amount."
    if values["kind"] not in PAYMENT_KINDS:
        errors["kind"] = "Choose a type."
    if values["method"] not in PAYMENT_METHODS:
        errors["method"] = "Choose a method."
    if parse_iso_day(values["created_on"]) is None:
        errors["created_on"] = "Enter a valid date."
    if not values["receipt_no"]:
        errors["receipt_no"] = "Enter a receipt number."
    elif db.execute("SELECT 1 FROM payments WHERE receipt_no = ?", (values["receipt_no"],)).fetchone():
        errors["receipt_no"] = "Receipt number already exists."
    if trip is not None and not errors.get("amount"):
        paid = trip["paid"] or 0
        if values["kind"] == "refund" and float(values["amount"]) - paid > 0.005:
            errors["amount"] = "Refund cannot exceed $" + "{:,.2f}".format(paid) + " paid."
    return errors


def deposit_message(client_name, trip_title, balance, departure_date, branch_name):
    return (
        "Hello "
        + client_name
        + ", this is "
        + branch_name
        + ". A balance of $"
        + "{:,.2f}".format(balance)
        + " remains on your trip '"
        + trip_title
        + "' departing "
        + departure_date
        + ". Reply to this message to arrange payment."
    )


def departure_message(client_name, trip_title, departure_date, days_left):
    if days_left <= 0:
        day_word = "today"
    elif days_left == 1:
        day_word = "tomorrow"
    else:
        day_word = "in " + str(days_left) + " days"
    return (
        "Hello "
        + client_name
        + ", your trip '"
        + trip_title
        + "' departs "
        + day_word
        + " on "
        + departure_date
        + ". Please arrive with your passport and voucher copies. Safe travels."
    )


def passport_message(client_name, expiry, days_left):
    timing = "expired on " + expiry if days_left < 0 else "expires on " + expiry
    return (
        "Hello "
        + client_name
        + ", your passport "
        + timing
        + ". Many trips require six months of validity, so please renew before you travel."
    )


def build_reminder_drafts(db, branch_ids, channels=("sms",)):
    today = date.today()
    created = 0
    wanted = [c for c in channels if c in REMINDER_CHANNELS] or ["sms"]
    extra, params = branch_filter(branch_ids, column="t.branch_id")
    unpaid = db.execute(
        "SELECT t.id AS trip_id, t.title, t.departure_date, t.total_price, c.id AS client_id,"
        " c.full_name AS client_name, b.name AS branch_name,"
        " COALESCE((SELECT SUM(CASE WHEN p.kind = 'refund' THEN -p.amount ELSE p.amount END)"
        " FROM payments p WHERE p.trip_id = t.id), 0) AS paid"
        " FROM trips t JOIN clients c ON c.id = t.client_id JOIN branches b ON b.id = t.branch_id"
        f" WHERE t.status != 'cancelled'{extra}",
        params,
    ).fetchall()
    for row in unpaid:
        balance = round(row["total_price"] - (row["paid"] or 0), 2)
        if balance <= 0.50:
            continue
        for channel_name in wanted:
            exists = db.execute(
                "SELECT id FROM reminders WHERE trip_id = ? AND kind = 'deposit'"
                " AND channel = ? AND status = 'ready'",
                (row["trip_id"], channel_name),
            ).fetchone()
            if exists is None:
                db.execute(
                    "INSERT INTO reminders (trip_id, client_id, kind, channel, message, status, due_date)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        row["trip_id"],
                        row["client_id"],
                        "deposit",
                        channel_name,
                        deposit_message(
                            row["client_name"], row["title"], balance, row["departure_date"], row["branch_name"]
                        ),
                        "ready",
                        today.isoformat(),
                    ),
                )
                created += 1
    soon = db.execute(
        "SELECT t.id AS trip_id, t.title, t.departure_date, c.id AS client_id, c.full_name AS client_name"
        " FROM trips t JOIN clients c ON c.id = t.client_id"
        f" WHERE t.status != 'cancelled' AND t.departure_date BETWEEN ? AND ?{extra}",
        [today.isoformat(), (today + timedelta(days=2)).isoformat(), *params],
    ).fetchall()
    for row in soon:
        dep = parse_iso_day(row["departure_date"])
        if dep is None:
            continue
        days_left = (dep - today).days
        for channel_name in wanted:
            exists = db.execute(
                "SELECT id FROM reminders WHERE trip_id = ? AND kind = 'departure'"
                " AND channel = ? AND status = 'ready'",
                (row["trip_id"], channel_name),
            ).fetchone()
            if exists is None:
                db.execute(
                    "INSERT INTO reminders (trip_id, client_id, kind, channel, message, status, due_date)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        row["trip_id"],
                        row["client_id"],
                        "departure",
                        channel_name,
                        departure_message(row["client_name"], row["title"], row["departure_date"], days_left),
                        "ready",
                        row["departure_date"],
                    ),
                )
                created += 1
    cextra, cparams = branch_filter(branch_ids, column="c.branch_id")
    clients = db.execute(
        "SELECT c.id, c.full_name, c.passport_expiry FROM clients c"
        f" WHERE c.passport_expiry IS NOT NULL AND c.passport_expiry != ''{cextra}",
        cparams,
    ).fetchall()
    for row in clients:
        expiry = parse_iso_day(row["passport_expiry"])
        if expiry is None:
            continue
        days_left = (expiry - today).days
        if days_left > PASSPORT_SOON_DAYS:
            continue
        for channel_name in wanted:
            exists = db.execute(
                "SELECT id FROM reminders WHERE trip_id IS NULL AND client_id = ?"
                " AND kind = 'passport' AND channel = ? AND status = 'ready'",
                (row["id"], channel_name),
            ).fetchone()
            if exists is None:
                db.execute(
                    "INSERT INTO reminders (trip_id, client_id, kind, channel, message, status, due_date)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        None,
                        row["id"],
                        "passport",
                        channel_name,
                        passport_message(row["full_name"], row["passport_expiry"], days_left),
                        "ready",
                        today.isoformat(),
                    ),
                )
                created += 1
    db.commit()
    return created


def query_revenue_by_branch(db, month, branch_ids):
    extra, params = branch_filter(branch_ids, column="b.id")
    rows = db.execute(
        "SELECT b.id, b.name,"
        " COALESCE(SUM(CASE WHEN strftime('%Y-%m', p.created_on) = ? AND p.kind != 'refund'"
        " THEN p.amount ELSE 0 END), 0) AS collected,"
        " COALESCE(SUM(CASE WHEN strftime('%Y-%m', p.created_on) = ? AND p.kind = 'refund'"
        " THEN p.amount ELSE 0 END), 0) AS refunds,"
        " COUNT(CASE WHEN strftime('%Y-%m', p.created_on) = ? THEN 1 END) AS payment_count"
        " FROM branches b LEFT JOIN trips t ON t.branch_id = b.id"
        " LEFT JOIN payments p ON p.trip_id = t.id"
        f" WHERE 1 = 1{extra} GROUP BY b.id, b.name ORDER BY b.name",
        [month, month, month, *params],
    ).fetchall()
    result = []
    for row in rows:
        collected = round(row["collected"] or 0, 2)
        refunds = round(row["refunds"] or 0, 2)
        result.append(
            {
                "id": row["id"],
                "name": row["name"],
                "collected": collected,
                "refunds": refunds,
                "revenue": round(collected - refunds, 2),
                "payment_count": row["payment_count"] or 0,
            }
        )
    return result


def query_commissions(db, month, branch_ids):
    extra, params = branch_filter(branch_ids, column="u.branch_id")
    rows = db.execute(
        "SELECT u.id, u.name, COALESCE(b.name, 'No branch') AS branch_name,"
        " COALESCE(SUM(CASE WHEN strftime('%Y-%m', p.created_on) = ? AND p.kind != 'refund'"
        " THEN p.amount WHEN strftime('%Y-%m', p.created_on) = ? AND p.kind = 'refund'"
        " THEN -p.amount ELSE 0 END), 0) AS revenue"
        " FROM users u LEFT JOIN branches b ON b.id = u.branch_id"
        " LEFT JOIN trips t ON t.agent_id = u.id LEFT JOIN payments p ON p.trip_id = t.id"
        f" WHERE u.role = 'agent'{extra} GROUP BY u.id, u.name, b.name ORDER BY u.name",
        [month, month, *params],
    ).fetchall()
    result = []
    for row in rows:
        revenue = round(row["revenue"] or 0, 2)
        result.append(
            {
                "name": row["name"],
                "branch_name": row["branch_name"],
                "revenue": revenue,
                "commission": round(revenue * COMMISSION_RATE, 2),
            }
        )
    return result


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

    deposit_text = deposit_message(
        "Hannah Whitfield", "Family beach getaway", 3650.00, iso(0), branches[0][0]
    )
    departure_text = departure_message("James Carter", "Lakeside retreat", iso(0), 0)
    passport_expiry = (today + timedelta(days=30)).isoformat()
    passport_text = passport_message("Hannah Whitfield", passport_expiry, 30)
    reminders = [
        (1, client_ids[0], "deposit", "sms", deposit_text, 5),
        (3, client_ids[3], "departure", "email", departure_text, 2),
        (None, client_ids[0], "passport", "sms", passport_text, 30),
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
