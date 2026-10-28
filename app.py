"""Local onboarding workspace: browser → Flask → SQLite.

Health follows visible rules. Imports validate all rows before one transaction
writes anything. No external services are called.
"""

import csv
import html
import io
import os
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, Response, abort, g, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

STAGES = ("Research", "Qualified", "Demo scheduled", "Proposal", "Onboarding", "Customer", "Closed")
TASK_STATUSES = ("todo", "done", "blocked")
USAGE_HEADERS = ("account_id", "seats", "active_users", "last_active_date")
MAX_USAGE_ROWS, MAX_USAGE_CSV, MAX_SEATS = 200, 50_000, 100_000


def create_app(database=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config["DATABASE"] = str(database or os.getenv("ACCOUNTBRIEF_DATABASE") or Path(app.instance_path) / "accountbrief.sqlite")
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)

    def db():
        if "db" not in g:
            g.db = sqlite3.connect(app.config["DATABASE"], timeout=10)
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")
        return g.db

    @app.teardown_appcontext
    def close_db(_error):
        connection = g.pop("db", None)
        if connection is not None:
            connection.close()

    with app.app_context():
        initialize(db())

    def require_row(table, item_id):
        # Table names come from routes, never request data.
        row = db().execute(f"SELECT * FROM {table} WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            abort(404, description=f"{table.rstrip('s').capitalize()} not found.")
        return dict(row)

    def detail(account_id):
        row = require_row("accounts", account_id)
        tasks, issues = rows_for(db(), "tasks", account_id), rows_for(db(), "issues", account_id)
        result = account_summary(row, tasks, issues)
        result.update(tasks=tasks, issues=issues, notes=rows_for(db(), "notes", account_id), activity=rows_for(db(), "activity", account_id))
        return result

    def summaries():
        tasks = group_by_account(db().execute("SELECT * FROM tasks ORDER BY id DESC"))
        issues = group_by_account(db().execute("SELECT * FROM issues ORDER BY id DESC"))
        return [account_summary(dict(row), tasks.get(row["id"], []), issues.get(row["id"], []))
                for row in db().execute("SELECT * FROM accounts ORDER BY company COLLATE NOCASE")]

    def filter_rows(rows):
        query = request.args.get("q", "").strip().casefold()
        view = request.args.get("view", "all")
        if len(query) > 100:
            raise ValueError("Keep the search to 100 characters.")
        if view not in ("all", "attention", "onboarding", "overdue"):
            raise ValueError("Choose all, attention, onboarding, or overdue.")
        result = []
        for row in rows:
            searchable = " ".join(str(row.get(key) or "") for key in ("company", "sector", "need", "owner")).casefold()
            if query not in searchable:
                continue
            if view == "attention" and row["health"]["state"] not in ("risk", "watch"):
                continue
            if view == "onboarding" and row["stage"] != "Onboarding":
                continue
            if view == "overdue" and not row["overdue"]:
                continue
            result.append(row)
        return result

    @app.get("/")
    def index():
        return render_template("index.html", stages=STAGES)

    @app.get("/api/accounts")
    def accounts():
        all_rows = summaries()
        return jsonify(accounts=filter_rows(all_rows), stages=STAGES, today=date.today().isoformat(), summary={
            "total": len(all_rows),
            "attention": sum(row["health"]["state"] in ("risk", "watch") for row in all_rows),
            "onboarding": sum(row["stage"] == "Onboarding" for row in all_rows),
            "overdue": sum(row["overdue"] for row in all_rows),
        })

    @app.get("/api/accounts/<int:account_id>")
    def account(account_id):
        return jsonify(detail(account_id))

    @app.post("/api/accounts")
    def add_account():
        body = json_body({"company", "sector", "owner", "plan", "stage", "next_action", "followup_date"})
        company = text_field(body, "company", 100)
        sector = text_field(body, "sector", 80)
        owner = text_field(body, "owner", 80)
        plan = text_field(body, "plan", 40)
        stage = text_field({"stage": body.get("stage", "Onboarding")}, "stage", 40)
        if stage not in STAGES:
            raise ValueError("Choose a stage from the list.")
        action = text_field(body, "next_action", 240, required=False)
        followup = date_field(body.get("followup_date"), "follow-up date", allow_empty=True)
        with db():
            # Lock before the duplicate check so concurrent creates cannot race.
            db().execute("BEGIN IMMEDIATE")
            names = db().execute("SELECT company FROM accounts").fetchall()
            if any(row["company"].casefold() == company.casefold() for row in names):
                abort(409, description="An account with this company name already exists.")
            cursor = db().execute("""INSERT INTO accounts
                (company, sector, size, stage, need, followup_date, next_action, owner, plan)
                VALUES (?, ?, 'Not specified', ?, '', ?, ?, ?, ?)""",
                (company, sector, stage, followup, action, owner, plan))
            account_id = cursor.lastrowid
            log_activity(db(), account_id, "account", "Created an account. No usage snapshot has been added.")
        return jsonify(id=account_id), 201

    @app.patch("/api/accounts/<int:account_id>")
    def update_account(account_id):
        require_row("accounts", account_id)
        body = json_body({"stage", "owner", "followup_date", "next_action"})
        values = {}
        for field in ("stage", "owner", "next_action"):
            if field in body:
                values[field] = text_field(body, field, 240 if field == "next_action" else 80)
        if "stage" in values and values["stage"] not in STAGES:
            raise ValueError("Choose a stage from the list.")
        if "followup_date" in body:
            values["followup_date"] = date_field(body["followup_date"], "follow-up date", allow_empty=True)
        with db():
            update_fields(db(), "accounts", account_id, values)
            log_activity(db(), account_id, "account", "Updated " + ", ".join(key.replace("_", " ") for key in values) + ".")
        return jsonify(message="Account saved.", account=detail(account_id))

    @app.post("/api/accounts/<int:account_id>/tasks")
    def add_task(account_id):
        require_row("accounts", account_id)
        body = json_body({"title", "owner", "due_date"})
        title, owner = text_field(body, "title", 180), text_field(body, "owner", 80)
        due = date_field(body.get("due_date"), "due date", allow_empty=True)
        with db():
            cursor = db().execute("INSERT INTO tasks (account_id, title, owner, due_date, status) VALUES (?, ?, ?, ?, 'todo')", (account_id, title, owner, due))
            log_activity(db(), account_id, "task", f"Added onboarding task: {title}")
        return jsonify(message="Task added.", id=cursor.lastrowid, account=detail(account_id)), 201

    @app.patch("/api/tasks/<int:task_id>")
    def update_task(task_id):
        task = require_row("tasks", task_id)
        body = json_body({"title", "owner", "due_date", "status"})
        values = {}
        for field in ("title", "owner", "status"):
            if field in body:
                values[field] = text_field(body, field, 180 if field == "title" else 80)
        if "status" in values and values["status"] not in TASK_STATUSES:
            raise ValueError("Choose todo, done, or blocked for task status.")
        if "due_date" in body:
            values["due_date"] = date_field(body["due_date"], "due date", allow_empty=True)
        with db():
            update_fields(db(), "tasks", task_id, values)
            log_activity(db(), task["account_id"], "task", f"Updated task: {values.get('title', task['title'])} ({values.get('status', task['status'])}).")
        return jsonify(message="Task saved.", account=detail(task["account_id"]))

    @app.post("/api/accounts/<int:account_id>/issues")
    def add_issue(account_id):
        require_row("accounts", account_id)
        body = json_body({"title", "severity"})
        title, severity = text_field(body, "title", 180), text_field(body, "severity", 10)
        if severity not in ("normal", "urgent"):
            raise ValueError("Choose normal or urgent for issue severity.")
        with db():
            cursor = db().execute("INSERT INTO issues (account_id, title, severity, status, created_at) VALUES (?, ?, ?, 'open', ?)", (account_id, title, severity, now()))
            log_activity(db(), account_id, "issue", f"Opened {severity} issue: {title}")
        return jsonify(message="Issue added.", id=cursor.lastrowid, account=detail(account_id)), 201

    @app.patch("/api/issues/<int:issue_id>")
    def update_issue(issue_id):
        issue = require_row("issues", issue_id)
        body = json_body({"status"})
        status = text_field(body, "status", 10)
        if status not in ("open", "resolved"):
            raise ValueError("Choose open or resolved for issue status.")
        with db():
            db().execute("UPDATE issues SET status = ? WHERE id = ?", (status, issue_id))
            log_activity(db(), issue["account_id"], "issue", f"Issue {status}: {issue['title']}")
        return jsonify(message="Issue saved.", account=detail(issue["account_id"]))

    @app.post("/api/accounts/<int:account_id>/notes")
    def add_note(account_id):
        require_row("accounts", account_id)
        body = json_body({"body", "source_url"})
        note = text_field(body, "body", 1200)
        if len(note) < 3:
            raise ValueError("Write at least 3 characters for the note.")
        source = text_field(body, "source_url", 500, required=False)
        if source:
            validate_source(source)
        with db():
            cursor = db().execute("INSERT INTO notes (account_id, body, source_url, created_at) VALUES (?, ?, ?, ?)", (account_id, note, source, now()))
            log_activity(db(), account_id, "note", "Added an account note" + (" with a source." if source else "."))
        return jsonify(message="Note saved.", id=cursor.lastrowid, account=detail(account_id)), 201

    @app.get("/api/accounts/<int:account_id>/brief")
    def brief(account_id):
        return download(render_brief(detail(account_id)), "text/markdown", f"accountbrief-{account_id}.md")

    @app.get("/api/export")
    def export():
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        keys = ("id", "company", "sector", "stage", "owner", "plan", "seats", "active_users", "adoption_pct", "last_active_date", "followup_date", "next_action")
        writer.writerow([*keys, "health", "health_reasons", "open_tasks", "overdue_tasks", "blocked_tasks", "open_issues"])
        for row in filter_rows(summaries()):
            values = [row[key] if row[key] is not None else "" for key in keys]
            values.extend([row["health"]["label"], "; ".join(row["health"]["reasons"]), row["open_tasks"], row["overdue_tasks"], row["blockers"], row["open_issues"]])
            writer.writerow([safe_csv_cell(value) for value in values])
        return download(output.getvalue(), "text/csv", "accountbrief-accounts.csv")

    @app.get("/api/usage/template")
    def template():
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(USAGE_HEADERS)
        for row in db().execute("SELECT id, seats, active_users, last_active_date FROM accounts ORDER BY id LIMIT 3"):
            writer.writerow([row["id"], row["seats"] if row["seats"] is not None else 10, row["active_users"] if row["active_users"] is not None else 0, row["last_active_date"] or date.today().isoformat()])
        return download(output.getvalue(), "text/csv", "accountbrief-usage-template.csv")

    def usage_preview():
        body = json_body({"csv"})
        csv_text = body.get("csv")
        if not isinstance(csv_text, str) or not csv_text.strip():
            raise ValueError("Choose a nonempty usage CSV file.")
        if len(csv_text) > MAX_USAGE_CSV:
            raise ValueError("Keep the CSV to 50,000 characters or fewer.")
        return validate_usage_csv(csv_text, db())

    @app.post("/api/usage/preview")
    def preview_usage():
        return jsonify(rows=usage_preview())

    @app.post("/api/usage/import")
    def import_usage():
        # Acquire a write lock before revalidation; the complete batch commits once.
        with db():
            db().execute("BEGIN IMMEDIATE")
            rows = usage_preview()
            for row in rows:
                update_fields(db(), "accounts", row["account_id"], row["after"])
                log_activity(db(), row["account_id"], "usage", f"Imported usage snapshot: {row['after']['active_users']}/{row['after']['seats']} active users; last active {row['after']['last_active_date']}.")
        return jsonify(updated=len(rows))

    @app.errorhandler(ValueError)
    def validation_error(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(HTTPException)
    def http_error(error):
        if request.path.startswith("/api/"):
            return jsonify(error=error.description), error.code
        return error

    @app.errorhandler(Exception)
    def unexpected_error(error):
        app.logger.exception("AccountBrief request failed", exc_info=error)
        if request.path.startswith("/api/"):
            return jsonify(error="The change could not be completed. No partial transaction was saved. Try again."), 500
        return "The workspace could not be loaded. Try again.", 500

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        response.headers["Cache-Control"] = "no-store"
        return response

    return app


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def json_body(allowed):
    if not request.is_json:
        raise ValueError("Send a JSON request.")
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not body or set(body) - allowed:
        raise ValueError("Send only the fields shown in the form.")
    return body


def text_field(body, name, maximum, required=True):
    value = body.get(name, "")
    if not isinstance(value, str):
        raise ValueError(f"{name.replace('_', ' ').capitalize()} must be text.")
    value = value.strip()
    if required and not value:
        raise ValueError(f"Add {name.replace('_', ' ')}.")
    if len(value) > maximum or "\x00" in value:
        raise ValueError(f"Keep {name.replace('_', ' ')} to {maximum} characters without null characters.")
    return value


def date_field(value, name, allow_empty=False, not_future=False):
    if allow_empty and value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError(f"Use YYYY-MM-DD for {name}.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"Use a valid YYYY-MM-DD date for {name}.") from None
    if parsed.isoformat() != value:
        raise ValueError(f"Use YYYY-MM-DD for {name}.")
    if not_future and parsed > date.today():
        raise ValueError(f"{name.capitalize()} cannot be in the future.")
    return value


def validate_source(source):
    try:
        url = urlsplit(source)
        _ = url.port
    except ValueError:
        raise ValueError("Add a valid HTTP(S) source URL.") from None
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or any(char.isspace() or ord(char) < 32 for char in source):
        raise ValueError("Add a complete HTTP(S) source URL without spaces or credentials.")


def update_fields(connection, table, item_id, values):
    # Callers construct allowed keys before this helper sees them.
    columns = ", ".join(f"{key} = ?" for key in values)
    connection.execute(f"UPDATE {table} SET {columns} WHERE id = ?", [*values.values(), item_id])


def log_activity(connection, account_id, kind, body):
    connection.execute("INSERT INTO activity (account_id, kind, body, created_at) VALUES (?, ?, ?, ?)", (account_id, kind, body, now()))


def rows_for(connection, table, account_id):
    return [dict(row) for row in connection.execute(f"SELECT * FROM {table} WHERE account_id = ? ORDER BY id DESC", (account_id,))]


def group_by_account(rows):
    grouped = {}
    for row in rows:
        grouped.setdefault(row["account_id"], []).append(dict(row))
    return grouped


def calculate_health(account, tasks, issues, today=None):
    today = today or date.today()
    if account["stage"] == "Closed":
        return {"state": "inactive", "label": "Inactive", "reasons": ["Account is closed."]}
    if account["stage"] not in ("Onboarding", "Customer"):
        return {"state": "unknown", "label": "Not onboarded", "reasons": ["Health rules apply after onboarding starts."]}
    risk, watch = [], []
    urgent = sum(issue["status"] == "open" and issue["severity"] == "urgent" for issue in issues)
    blocked = sum(task["status"] == "blocked" for task in tasks)
    overdue = sum(task["status"] != "done" and bool(task["due_date"]) and task["due_date"] < today.isoformat() for task in tasks)
    if urgent:
        risk.append(f"{urgent} urgent open issue(s).")
    if blocked:
        risk.append(f"{blocked} blocked onboarding task(s).")
    if overdue:
        watch.append(f"{overdue} overdue onboarding task(s).")
    missing = any(account.get(key) is None for key in ("seats", "active_users", "last_active_date"))
    if not missing:
        inactive_days = (today - date.fromisoformat(account["last_active_date"])).days
        if inactive_days >= 7:
            risk.append(f"No product activity for {inactive_days} days (7-day threshold).")
        elif inactive_days >= 3:
            watch.append(f"No product activity for {inactive_days} days (3–6-day review window).")
        if account["seats"] > 0 and account["active_users"] / account["seats"] < 0.5:
            watch.append(f"{account['active_users']} of {account['seats']} seats active (below 50%).")
    if risk:
        return {"state": "risk", "label": "At risk", "reasons": risk + watch + (["No complete usage snapshot."] if missing else [])}
    if missing:
        return {"state": "unknown", "label": "No usage data", "reasons": ["No complete usage snapshot."] + watch}
    if watch:
        return {"state": "watch", "label": "Needs review", "reasons": watch}
    reasons = ["No attention flags under the current rules."]
    if account["seats"] == 0:
        reasons.append("Zero seats: adoption percentage is not calculated.")
    return {"state": "healthy", "label": "On track", "reasons": reasons}


def account_summary(account, tasks, issues):
    result = dict(account)
    result["health"] = calculate_health(account, tasks, issues)
    seats, active = account.get("seats"), account.get("active_users")
    result["adoption_pct"] = round(active / seats * 100, 1) if seats and active is not None else None
    result["open_tasks"] = sum(task["status"] != "done" for task in tasks)
    result["overdue_tasks"] = sum(task["status"] != "done" and bool(task["due_date"]) and task["due_date"] < date.today().isoformat() for task in tasks)
    result["blockers"] = sum(task["status"] == "blocked" for task in tasks)
    result["open_issues"] = sum(issue["status"] == "open" for issue in issues)
    result["overdue"] = account["stage"] != "Closed" and (result["overdue_tasks"] > 0 or bool(account["followup_date"] and account["followup_date"] < date.today().isoformat()))
    return result


def validate_usage_csv(csv_text, connection):
    try:
        reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff"), newline=""), strict=True)
        if reader.fieldnames is None or len(reader.fieldnames) != len(USAGE_HEADERS) or set(reader.fieldnames) != set(USAGE_HEADERS):
            raise ValueError("CSV headers must be exactly: " + ",".join(USAGE_HEADERS))
        result, seen = [], set()
        for line, fields in enumerate(reader, start=2):
            if len(result) >= MAX_USAGE_ROWS:
                raise ValueError(f"Import at most {MAX_USAGE_ROWS} usage rows at a time.")
            if None in fields or any(value is None for value in fields.values()):
                raise ValueError(f"Row {line}: use exactly four values.")
            numbers = {}
            for name in ("account_id", "seats", "active_users"):
                value = fields[name].strip()
                if not re.fullmatch(r"0|[1-9][0-9]*", value) or len(value) > 10:
                    raise ValueError(f"Row {line}: {name} must be a nonnegative whole number.")
                numbers[name] = int(value)
            account_id = numbers.pop("account_id")
            if account_id < 1 or account_id in seen:
                raise ValueError(f"Row {line}: account IDs must be positive and appear only once.")
            if numbers["seats"] > MAX_SEATS or numbers["active_users"] > MAX_SEATS or numbers["active_users"] > numbers["seats"]:
                raise ValueError(f"Row {line}: active_users must not exceed seats; both must be between 0 and {MAX_SEATS}.")
            account = connection.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
            if account is None:
                raise ValueError(f"Row {line}: account {account_id} does not exist.")
            after = {**numbers, "last_active_date": date_field(fields["last_active_date"].strip(), f"row {line} last active date", not_future=True)}
            account = dict(account)
            tasks, issues = rows_for(connection, "tasks", account_id), rows_for(connection, "issues", account_id)
            before = {key: account.get(key) for key in ("seats", "active_users", "last_active_date")}
            result.append({"account_id": account_id, "company": account["company"], "before": before, "after": after,
                           "health_before": calculate_health(account, tasks, issues), "health_after": calculate_health({**account, **after}, tasks, issues)})
            seen.add(account_id)
    except csv.Error:
        raise ValueError("The CSV could not be parsed. Check quotes and row separators.") from None
    if not result:
        raise ValueError("Add at least one account usage row below the headers.")
    return result


def safe_csv_cell(value):
    text = str(value)
    candidate = re.sub(r"^[\s\ufeff]+", "", text)
    return "'" + text if candidate.startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")) else text


def download(content, mimetype, filename):
    return Response(content, mimetype=mimetype, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def markdown_text(value):
    text = html.escape(str(value or ""))
    return re.sub(r"([\\`*_{}\[\]()#+.!|>-])", r"\\\1", text).replace("\n", "  \n")


def render_brief(account):
    m = markdown_text
    lines = [f"# {m(account['company'])} — account review", "", "Local workspace: initial sample records are fictional; added records contain user-provided information. No commercial outcomes are claimed.", "",
             f"Generated: {now()}", "", "## Current context", "",
             f"- Stage: {m(account['stage'])}", f"- Owner: {m(account['owner'])}", f"- Plan: {m(account['plan'])}",
             f"- Health: {account['health']['label']}", *[f"  - {m(reason)}" for reason in account["health"]["reasons"]],
             f"- Seats / active users: {account['seats'] if account['seats'] is not None else 'Unknown'} / {account['active_users'] if account['active_users'] is not None else 'Unknown'}",
             f"- Adoption: {str(account['adoption_pct']) + '%' if account['adoption_pct'] is not None else 'Not calculated'}",
             f"- Last active: {account['last_active_date'] or 'No usage data'}", f"- Follow-up: {account['followup_date'] or 'Not set'}",
             f"- Next action: {m(account['next_action'])}", "", "## Open onboarding work", ""]
    tasks = [task for task in account["tasks"] if task["status"] != "done"]
    lines.extend([f"- [{task['status']}] {m(task['title'])} — {m(task['owner'])}; due {task['due_date'] or 'not set'}" for task in tasks] or ["No open onboarding tasks."])
    lines.extend(["", "## Open issues", ""])
    issues = [issue for issue in account["issues"] if issue["status"] == "open"]
    lines.extend([f"- [{issue['severity']}] {m(issue['title'])}" for issue in issues] or ["No open issues."])
    lines.extend(["", "## Account notes", ""])
    for note in account["notes"]:
        lines.extend([f"### {note['created_at']}", "", m(note["body"]), f"Source: {m(note['source_url']) if note['source_url'] else 'Not provided'}", ""])
    if not account["notes"]:
        lines.append("No notes recorded.")
    return "\n".join(lines) + "\n"


def initialize(connection):
    """Additive migrations: never replace existing account rows or notes."""
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY, company TEXT NOT NULL, sector TEXT NOT NULL,
            size TEXT NOT NULL, stage TEXT NOT NULL, need TEXT NOT NULL,
            followup_date TEXT, next_action TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id),
            body TEXT NOT NULL, source_url TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id),
            title TEXT NOT NULL, owner TEXT NOT NULL, due_date TEXT,
            status TEXT NOT NULL CHECK(status IN ('todo', 'done', 'blocked'))
        );
        CREATE TABLE IF NOT EXISTS issues (
            id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id),
            title TEXT NOT NULL, severity TEXT NOT NULL CHECK(severity IN ('normal', 'urgent')),
            status TEXT NOT NULL CHECK(status IN ('open', 'resolved')), created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS activity (
            id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id),
            kind TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_account ON tasks(account_id);
        CREATE INDEX IF NOT EXISTS idx_issues_account ON issues(account_id);
        CREATE INDEX IF NOT EXISTS idx_notes_account ON notes(account_id);
        CREATE INDEX IF NOT EXISTS idx_activity_account ON activity(account_id);
    """)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(accounts)")}
    additions = {"owner": "TEXT NOT NULL DEFAULT 'Unassigned'", "plan": "TEXT NOT NULL DEFAULT 'Trial'", "seats": "INTEGER", "active_users": "INTEGER", "last_active_date": "TEXT"}
    with connection:
        for name, declaration in additions.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE accounts ADD COLUMN {name} {declaration}")
        connection.execute("PRAGMA user_version = 2")
    if not connection.execute("SELECT count(*) FROM accounts").fetchone()[0]:
        seed(connection)


def seed(connection):
    today = date.today()
    day = lambda offset: (today + timedelta(days=offset)).isoformat() if offset is not None else None
    samples = [
        ("Cedar & Loom", "Home & living", "11–50", "Onboarding", "Make order-status answers easier to find.", -2, "Unblock catalog sync and confirm the first working workflow.", "Alex", "Growth", 24, 8, -4),
        ("Morrow Skincare", "Beauty", "11–50", "Customer", "Recover abandoned carts with relevant follow-ups.", 1, "Review recent usage and confirm the open support question.", "Sam", "Growth", 18, 15, -1),
        ("Trailkind", "Outdoor retail", "51–200", "Customer", "Compare repeat-purchase journeys across channels.", -1, "Resolve the connection issue before the account review.", "Alex", "Scale", 60, 12, -9),
        ("Paperplane Studio", "Stationery", "1–10", "Onboarding", "Keep customer requests and next steps together.", 3, "Run the first team walkthrough after workspace setup.", "Taylor", "Starter", 8, 6, 0),
        ("Northline Coffee", "Food & beverage", "11–50", "Customer", "Track onboarding progress for a small operations team.", 0, "Follow up on incomplete staff training.", "Sam", "Growth", 20, 7, -3),
        ("Serein Apparel", "Fashion", "51–200", "Onboarding", "Understand size-exchange questions before a product demo.", 2, "Request the first usage snapshot from the workspace owner.", "Taylor", "Scale", None, None, None),
        ("Fieldnote Goods", "Lifestyle", "1–10", "Qualified", "Group product feedback into useful themes.", 4, "Confirm the onboarding scope and a responsible owner.", "Alex", "Trial", None, None, None),
        ("Brightnest", "Home & living", "11–50", "Closed", "No active evaluation in this fictional scenario.", None, "Keep the context for a future review.", "Sam", "Starter", 10, 0, -20),
    ]
    with connection:
        for company, sector, size, stage, need, followup, action, owner, plan, seats, active, last_active in samples:
            connection.execute("INSERT INTO accounts (company, sector, size, stage, need, followup_date, next_action, owner, plan, seats, active_users, last_active_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (company, sector, size, stage, need, day(followup), action, owner, plan, seats, active, day(last_active)))
        tasks = [
            (1, "Connect the product catalog", "Alex", -2, "blocked"), (1, "Invite the operations team", "Account owner", 1, "todo"), (1, "Confirm success criteria", "Alex", -4, "done"),
            (2, "Share the account review summary", "Sam", 2, "todo"), (2, "Complete the first team walkthrough", "Sam", -5, "done"),
            (3, "Recheck the workspace connection", "Alex", -1, "todo"),
            (4, "Invite two teammates", "Account owner", 1, "todo"), (4, "Set up the first workflow", "Taylor", -1, "done"),
            (5, "Complete staff training", "Sam", -2, "todo"), (5, "Agree a weekly review owner", "Account owner", 1, "todo"),
            (6, "Confirm the workspace owner", "Taylor", 1, "todo"), (6, "Import the first usage snapshot", "Account owner", 2, "todo"),
        ]
        for account_id, title, owner, due, status in tasks:
            connection.execute("INSERT INTO tasks (account_id, title, owner, due_date, status) VALUES (?, ?, ?, ?, ?)", (account_id, title, owner, day(due), status))
        for account_id, title, severity, status in [(1, "Catalog sync is waiting for a field mapping", "normal", "open"), (2, "Clarify the saved segment filter", "normal", "open"), (3, "Workspace connection is unavailable", "urgent", "open"), (4, "Invitation email question answered", "normal", "resolved")]:
            connection.execute("INSERT INTO issues (account_id, title, severity, status, created_at) VALUES (?, ?, ?, ?, ?)", (account_id, title, severity, status, now()))
        connection.execute("INSERT INTO notes (account_id, body, source_url, created_at) VALUES (1, ?, ?, ?)", ("Sample handoff: agree the catalog field mapping before the first team walkthrough. This scenario and its usage figures are fictional.", "https://example.com/fictional-onboarding-brief", now()))
        for account_id in range(1, 9):
            log_activity(connection, account_id, "sample", "Created a fictional account scenario for this local workspace.")


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=int(os.getenv("PORT", "5062")), debug=False)
