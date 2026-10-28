"""A small, local account-research ledger: browser → Flask → SQLite."""

import csv
import io
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, Response, g, jsonify, render_template, request

STAGES = ("Research", "Qualified", "Demo scheduled", "Proposal", "Customer", "Closed")
INACTIVE_STAGES = ("Closed",)


def create_app(database=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config["DATABASE"] = str(database or os.getenv("ACCOUNTBRIEF_DATABASE") or Path(app.instance_path) / "accountbrief.sqlite")
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)

    def db():
        if "db" not in g:
            g.db = sqlite3.connect(app.config["DATABASE"])
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

    def account_or_404(account_id):
        row = db().execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return dict(row) if row else None

    def filtered_accounts():
        query = request.args.get("q", "").strip()
        stage = request.args.get("stage", "")
        if len(query) > 100:
            raise ValueError("Keep the search to 100 characters.")
        if stage and stage not in STAGES:
            raise ValueError("Choose a stage from the list.")
        sql = "SELECT a.*, (SELECT count(*) FROM notes n WHERE n.account_id = a.id) AS note_count FROM accounts a WHERE 1 = 1"
        params = []
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            sql += " AND (a.company LIKE ? ESCAPE '\\' OR a.sector LIKE ? ESCAPE '\\' OR a.need LIKE ? ESCAPE '\\')"
            params.extend([f"%{escaped}%"] * 3)
        if stage:
            sql += " AND a.stage = ?"
            params.append(stage)
        if request.args.get("overdue") == "1":
            sql += " AND a.followup_date < ? AND a.stage != ?"
            params.extend([date.today().isoformat(), *INACTIVE_STAGES])
        sql += " ORDER BY a.company COLLATE NOCASE"
        return [decorate(dict(row)) for row in db().execute(sql, params).fetchall()]

    @app.get("/")
    def index():
        return render_template("index.html", stages=STAGES)

    @app.get("/api/accounts")
    def accounts():
        try:
            result = filtered_accounts()
        except ValueError as error:
            return jsonify(error=str(error)), 400
        all_rows = [decorate(dict(row)) for row in db().execute("SELECT * FROM accounts")]
        due = sorted([row for row in all_rows if row["overdue"]], key=lambda row: row["followup_date"])
        return jsonify(accounts=result, stages=STAGES, today=date.today().isoformat(), summary={
            "total": len(all_rows), "active": sum(row["stage"] not in INACTIVE_STAGES for row in all_rows),
            "overdue": len(due), "evidence_notes": db().execute("SELECT count(*) FROM notes").fetchone()[0],
        }, overdue_actions=due[:3])

    @app.get("/api/accounts/<int:account_id>")
    def account(account_id):
        result = account_or_404(account_id)
        if result is None:
            return jsonify(error="Account not found. Choose another record."), 404
        result = decorate(result)
        result["notes"] = [dict(row) for row in db().execute("SELECT * FROM notes WHERE account_id = ? ORDER BY id DESC", (account_id,))]
        return jsonify(result)

    def json_body(allowed):
        if not request.is_json:
            raise ValueError("Send a JSON request.")
        # Requiring JSON prevents ordinary cross-site form submissions to this local demo.
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not body or set(body) - allowed:
            raise ValueError("Send the fields shown in the form.")
        return body

    @app.patch("/api/accounts/<int:account_id>")
    def update_account(account_id):
        if account_or_404(account_id) is None:
            return jsonify(error="Account not found."), 404
        try:
            body = json_body({"stage", "followup_date", "next_action"})
            stage = text_field(body, "stage", 40)
            if stage not in STAGES:
                raise ValueError("Choose a stage from the list.")
            followup = text_field(body, "followup_date", 10, required=False)
            if followup:
                if date.fromisoformat(followup).isoformat() != followup:
                    raise ValueError("Use a valid date in YYYY-MM-DD format.")
            action = text_field(body, "next_action", 240)
        except (ValueError, TypeError) as error:
            return jsonify(error=str(error) if str(error) else "Enter a valid follow-up date."), 400
        with db():
            db().execute("UPDATE accounts SET stage = ?, followup_date = ?, next_action = ? WHERE id = ?", (stage, followup or None, action, account_id))
        return jsonify(message="Next action saved.")

    @app.post("/api/accounts/<int:account_id>/notes")
    def add_note(account_id):
        if account_or_404(account_id) is None:
            return jsonify(error="Account not found."), 404
        try:
            body = json_body({"body", "source_url"})
            note = text_field(body, "body", 1200)
            if len(note) < 3:
                raise ValueError("Write at least 3 characters for the evidence note.")
            source = text_field(body, "source_url", 500)
            try:
                url = urlsplit(source)
                _ = url.port  # Accessing the property validates nonnumeric/out-of-range ports.
            except ValueError:
                raise ValueError("Add a valid http:// or https:// source URL.") from None
            if url.scheme not in ("http", "https") or not url.netloc or not url.hostname or url.username or url.password or any(char.isspace() for char in source):
                raise ValueError("Add a complete http:// or https:// source URL without spaces or credentials.")
        except (ValueError, TypeError) as error:
            return jsonify(error=str(error)), 400
        with db():
            cursor = db().execute("INSERT INTO notes (account_id, body, source_url, created_at) VALUES (?, ?, ?, ?)", (account_id, note, source, datetime.now(timezone.utc).isoformat(timespec="seconds")))
        return jsonify(message="Evidence note saved.", id=cursor.lastrowid), 201

    @app.get("/api/export")
    def export():
        try:
            rows = filtered_accounts()
        except ValueError as error:
            return jsonify(error=str(error)), 400
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["Company", "Sector", "Stage", "Need", "Follow-up date", "Next action", "Evidence notes"])
        for row in rows:
            writer.writerow([safe_csv_cell(row[key] or "") for key in ("company", "sector", "stage", "need", "followup_date", "next_action", "note_count")])
        return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": 'attachment; filename="accountbrief-demo.csv"'})

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        response.headers["Cache-Control"] = "no-store"
        return response

    return app


def text_field(body, name, maximum, required=True):
    value = body.get(name, "")
    if not isinstance(value, str):
        raise ValueError(f"{name.replace('_', ' ').capitalize()} must be text.")
    value = value.strip()
    if required and not value:
        raise ValueError(f"Add {name.replace('_', ' ')}.")
    if len(value) > maximum:
        raise ValueError(f"Keep {name.replace('_', ' ')} to {maximum} characters.")
    return value


def safe_csv_cell(value):
    """Prevent spreadsheet formula execution, including whitespace-prefixed formulas."""
    text = str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")) else text


def decorate(row):
    row["overdue"] = bool(row["followup_date"] and row["followup_date"] < date.today().isoformat() and row["stage"] not in INACTIVE_STAGES)
    return row


def initialize(connection):
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
    """)
    if connection.execute("SELECT count(*) FROM accounts").fetchone()[0]:
        return
    today = date.today()
    samples = [
        ("Cedar & Loom", "Home & living", "11–50", "Qualified", "Make order-status answers easier to find.", -2, "Prepare three discovery questions for order tracking."),
        ("Morrow Skincare", "Beauty", "11–50", "Demo scheduled", "Recover abandoned carts with relevant follow-ups.", 1, "Outline a five-minute cart recovery walkthrough."),
        ("Trailkind", "Outdoor retail", "51–200", "Research", "Compare repeat-purchase journeys across channels.", -1, "Check the sample brief and add a source-backed observation."),
        ("Paperplane Studio", "Stationery", "1–10", "Proposal", "Keep customer requests and next steps together.", 3, "Summarize requirements before the next review."),
        ("Northline Coffee", "Food & beverage", "11–50", "Customer", "Track onboarding progress for a small operations team.", -3, "Review the sample onboarding checklist."),
        ("Serein Apparel", "Fashion", "51–200", "Research", "Understand size-exchange questions before a product demo.", 4, "Draft a short research brief on exchange support."),
        ("Fieldnote Goods", "Lifestyle", "1–10", "Qualified", "Group product feedback into useful themes.", 0, "Record the questions to cover in a discovery call."),
        ("Brightnest", "Home & living", "11–50", "Closed", "No active evaluation in this fictional scenario.", None, "Keep the research for a future review."),
    ]
    with connection:
        for company, sector, size, stage, need, offset, action in samples:
            followup = (today + timedelta(days=offset)).isoformat() if offset is not None else None
            connection.execute("INSERT INTO accounts (company, sector, size, stage, need, followup_date, next_action) VALUES (?, ?, ?, ?, ?, ?, ?)", (company, sector, size, stage, need, followup, action))
        connection.execute("INSERT INTO notes (account_id, body, source_url, created_at) VALUES (1, ?, ?, ?)", ("Synthetic research note: the sample scenario describes repeated order-status questions. Validate this need in discovery; it is not a verified statement about a real brand.", "https://example.com/fictional-cedar-and-loom", datetime.now(timezone.utc).isoformat(timespec="seconds")))


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=int(os.getenv("PORT", "5062")), debug=False)
