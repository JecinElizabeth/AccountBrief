"""Behavior and transaction tests; each test has its own temporary SQLite file."""

import csv
import io
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import app as app_module
from app import calculate_health, create_app, safe_csv_cell


class AccountBriefTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "test.sqlite"
        self.app = create_app(self.database)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.today = date.today().isoformat()

    def tearDown(self):
        self.directory.cleanup()

    def detail(self, account_id=1):
        return self.client.get(f"/api/accounts/{account_id}").get_json()

    def usage_csv(self, rows=None):
        rows = rows or [f"1,24,24,{self.today}", f"2,18,17,{self.today}"]
        return "account_id,seats,active_users,last_active_date\n" + "\n".join(rows) + "\n"

    def snapshot(self):
        with closing(sqlite3.connect(self.database)) as connection:
            return {table: connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
                    for table in ("accounts", "notes", "tasks", "issues", "activity")}

    def test_seed_is_idempotent_and_returns_contract(self):
        before = self.snapshot()
        create_app(self.database)
        self.assertEqual(before, self.snapshot())
        data = self.client.get("/api/accounts").get_json()
        self.assertEqual(data["summary"], {"total": 8, "attention": 3, "onboarding": 3, "overdue": 3})
        self.assertEqual(len(data["accounts"]), 8)
        required = {"id", "company", "stage", "owner", "plan", "seats", "active_users", "adoption_pct", "last_active_date", "health", "open_tasks", "overdue_tasks", "blockers", "open_issues"}
        self.assertTrue(required <= set(data["accounts"][0]))
        self.assertTrue({"tasks", "issues", "notes", "activity"} <= set(self.detail()))

    def test_search_and_views_combine_without_wildcard_or_sql_interpretation(self):
        for view, count in (("all", 8), ("attention", 3), ("onboarding", 3), ("overdue", 3)):
            with self.subTest(view=view):
                self.assertEqual(len(self.client.get(f"/api/accounts?view={view}").get_json()["accounts"]), count)
        rows = self.client.get("/api/accounts?q=CEDAR&view=attention").get_json()["accounts"]
        self.assertEqual([row["company"] for row in rows], ["Cedar & Loom"])
        for query in ("%25", "%27%20OR%201%3D1--", "no-such-account"):
            self.assertEqual(self.client.get(f"/api/accounts?q={query}").get_json()["accounts"], [])
        self.assertEqual(self.client.get("/api/accounts?view=invalid").status_code, 400)

    def test_partial_account_updates_persist_and_recalculate_health(self):
        original = self.detail()
        response = self.client.patch("/api/accounts/1", json={"owner": "Review owner"})
        self.assertEqual(response.status_code, 200)
        row = create_app(self.database).test_client().get("/api/accounts/1").get_json()
        self.assertEqual(row["owner"], "Review owner")
        self.assertEqual(row["stage"], original["stage"])
        self.assertEqual(row["notes"], original["notes"])
        self.assertEqual(self.client.patch("/api/accounts/1", json={"stage": "Closed", "followup_date": None}).status_code, 200)
        self.assertEqual(self.detail()["health"]["state"], "inactive")
        self.assertFalse(self.detail()["overdue"])

    def test_new_account_is_persistent_without_fabricated_usage_or_work(self):
        body = {"company": "New user account", "sector": "Software", "owner": "Case owner", "plan": "Trial"}
        response = self.client.post("/api/accounts", json=body)
        self.assertEqual(response.status_code, 201)
        account_id = response.get_json()["id"]
        row = create_app(self.database).test_client().get(f"/api/accounts/{account_id}").get_json()
        self.assertEqual((row["company"], row["stage"]), (body["company"], "Onboarding"))
        self.assertEqual(row["health"]["label"], "No usage data")
        self.assertEqual([row["seats"], row["active_users"], row["last_active_date"], row["adoption_pct"]], [None] * 4)
        self.assertEqual((row["tasks"], row["issues"], row["notes"]), ([], [], []))
        self.assertEqual(row["next_action"], "")
        self.assertEqual(len(row["activity"]), 1)
        result = self.client.post("/api/usage/import", json={"csv": self.usage_csv([f"{account_id},12,9,{self.today}"])})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.detail(account_id)["adoption_pct"], 75.0)

    def test_new_account_duplicate_and_field_validation(self):
        body = {"company": "  cEdAr & lOoM  ", "sector": "Software", "owner": "Case owner", "plan": "Trial"}
        before = self.snapshot()
        response = self.client.post("/api/accounts", json=body)
        self.assertEqual(response.status_code, 409)
        self.assertIn("error", response.get_json())
        for changes in ({"company": ""}, {"company": "x" * 101}, {"company": "New", "stage": "Unsupported"}, {"company": "New", "owner": 12}, {"company": "New", "followup_date": "bad"}):
            self.assertEqual(self.client.post("/api/accounts", json={**body, **changes}).status_code, 400)
        self.assertEqual(before, self.snapshot())

    def test_new_account_accepts_explicit_stage_and_next_action(self):
        body = {"company": "Second account", "sector": "Software", "owner": "Case owner", "plan": "Trial", "stage": "Qualified", "next_action": "Confirm the scope", "followup_date": self.today}
        response = self.client.post("/api/accounts", json=body)
        row = self.detail(response.get_json()["id"])
        self.assertEqual((row["stage"], row["next_action"], row["followup_date"]), ("Qualified", "Confirm the scope", self.today))
        self.assertEqual(row["health"]["label"], "Not onboarded")

    def test_invalid_partial_updates_are_atomic(self):
        before = self.snapshot()
        for body in ({"owner": "New owner", "stage": "Pretend"}, {"owner": 12}, {"next_action": ""}, {"followup_date": "2026-02-30"}, {"unexpected": "value"}, {}):
            with self.subTest(body=body):
                response = self.client.patch("/api/accounts/1", json=body)
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.get_json())
                self.assertEqual(before, self.snapshot())

    def test_task_creation_update_completion_and_health(self):
        response = self.client.post("/api/accounts/4/tasks", json={"title": "Confirm the import owner", "owner": "Taylor", "due_date": self.today})
        self.assertEqual(response.status_code, 201)
        task_id = response.get_json()["id"]
        response = self.client.patch(f"/api/tasks/{task_id}", json={"status": "blocked"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.detail(4)["health"]["state"], "risk")
        self.assertEqual(self.detail(4)["blockers"], 1)
        self.client.patch(f"/api/tasks/{task_id}", json={"status": "done", "owner": "Sam", "due_date": None})
        detail = self.detail(4)
        self.assertEqual(detail["health"]["state"], "healthy")
        saved_task = next(task for task in detail["tasks"] if task["id"] == task_id)
        self.assertEqual((saved_task["owner"], saved_task["due_date"], saved_task["status"]), ("Sam", None, "done"))
        self.assertTrue(any("Confirm the import owner" in event["body"] for event in detail["activity"]))

    def test_task_validation_does_not_write(self):
        before = self.snapshot()
        for body in ({"title": "", "owner": "Alex"}, {"title": "Task", "owner": "Alex", "due_date": "bad"}):
            self.assertEqual(self.client.post("/api/accounts/1/tasks", json=body).status_code, 400)
        self.assertEqual(self.client.patch("/api/tasks/1", json={"status": "maybe", "owner": "Changed"}).status_code, 400)
        self.assertEqual(self.snapshot(), before)

    def test_urgent_issue_resolution_recalculates_and_records_history(self):
        response = self.client.post("/api/accounts/4/issues", json={"title": "Sample access problem", "severity": "urgent"})
        self.assertEqual(response.status_code, 201)
        issue_id = response.get_json()["id"]
        self.assertEqual(self.detail(4)["health"]["state"], "risk")
        self.client.patch(f"/api/issues/{issue_id}", json={"status": "resolved"})
        self.assertEqual(self.detail(4)["health"]["state"], "healthy")
        self.client.patch(f"/api/issues/{issue_id}", json={"status": "open"})
        self.assertEqual(self.detail(4)["health"]["state"], "risk")
        self.assertEqual(self.client.patch(f"/api/issues/{issue_id}", json={"status": "deleted"}).status_code, 400)
        self.assertEqual(self.client.post("/api/accounts/4/issues", json={"title": "Bad severity", "severity": "critical"}).status_code, 400)

    def test_note_source_is_optional_but_nonempty_urls_are_validated(self):
        self.assertEqual(self.client.post("/api/accounts/4/notes", json={"body": "A useful internal note."}).status_code, 201)
        for source in ("javascript:alert(1)", "https://user:secret@example.com", "not-a-url", "https://example.com/has space", "https://example.com:badport", "https://example.com:99999"):
            with self.subTest(source=source):
                self.assertEqual(self.client.post("/api/accounts/4/notes", json={"body": "An observation", "source_url": source}).status_code, 400)
        text = '<script>alert("x")</script> is stored as text.'
        self.client.post("/api/accounts/4/notes", json={"body": text, "source_url": "https://example.com/source"})
        self.assertEqual(create_app(self.database).test_client().get("/api/accounts/4").get_json()["notes"][0]["body"], text)

    def test_preview_shows_current_and_proposed_health_without_any_write(self):
        before = self.snapshot()
        response = self.client.post("/api/usage/preview", json={"csv": self.usage_csv([f"5,20,18,{self.today}"])})
        self.assertEqual(response.status_code, 200)
        row = response.get_json()["rows"][0]
        self.assertEqual(row["before"]["active_users"], 7)
        self.assertEqual(row["after"]["active_users"], 18)
        self.assertEqual(row["health_before"]["state"], "watch")
        # The overdue training task remains a review flag despite better usage.
        self.assertEqual(row["health_after"]["state"], "watch")
        self.assertEqual(self.snapshot(), before)

    def test_valid_import_persists_whole_batch_and_logs_each_account(self):
        before = len(self.detail()["activity"])
        response = self.client.post("/api/usage/import", json={"csv": self.usage_csv()})
        self.assertEqual(response.get_json(), {"updated": 2})
        fresh_client = create_app(self.database).test_client()
        self.assertEqual(fresh_client.get("/api/accounts/1").get_json()["active_users"], 24)
        self.assertEqual(fresh_client.get("/api/accounts/2").get_json()["active_users"], 17)
        self.assertEqual(len(self.detail()["activity"]), before + 1)
        self.assertEqual(self.detail()["health"]["state"], "risk")  # Blocker is not hidden by good usage.

    def test_invalid_import_rows_reject_the_entire_batch_in_preview_and_apply(self):
        future = (date.today() + timedelta(days=1)).isoformat()
        invalid_rows = [f"2,10,11,{self.today}", f"2,-1,0,{self.today}", f"2,1.5,1,{self.today}", f"2,100001,2,{self.today}",
                        f"2,10,2,{future}", "2,10,2,2026-02-30", f"999,10,2,{self.today}", f"1,10,2,{self.today}",
                        f"2,10,2,{self.today},extra", f"2,10,2", f"0,10,2,{self.today}", f"2,True,2,{self.today}"]
        before = self.snapshot()
        for invalid in invalid_rows:
            for route in ("preview", "import"):
                with self.subTest(invalid=invalid, route=route):
                    response = self.client.post(f"/api/usage/{route}", json={"csv": self.usage_csv([f"1,24,24,{self.today}", invalid])})
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(self.snapshot(), before)

    def test_csv_headers_parsing_and_body_limits(self):
        cases = ["", "account_id,seats,active_users,last_active_date\n", "account_id,seats,active_users,extra\n1,2,1,x\n",
                 "account_id,seats,seats,last_active_date\n1,2,1,2026-01-01\n", 'account_id,seats,active_users,last_active_date\n1,2,1,"unclosed\n']
        for value in cases:
            self.assertEqual(self.client.post("/api/usage/preview", json={"csv": value}).status_code, 400)
        self.assertEqual(self.client.post("/api/usage/preview", json={"csv": "x" * 50_001}).status_code, 400)
        response = self.client.post("/api/usage/import", json={"csv": "x" * 70_000})
        self.assertEqual(response.status_code, 413)
        self.assertIn("error", response.get_json())
        with patch.object(app_module, "MAX_USAGE_ROWS", 1):
            self.assertEqual(self.client.post("/api/usage/preview", json={"csv": self.usage_csv()}).status_code, 400)

    def test_import_rolls_back_if_a_write_fails_mid_batch(self):
        before = self.snapshot()
        real_log = app_module.log_activity
        count = 0

        def fail_second(connection, account_id, kind, body):
            nonlocal count
            count += 1
            if count == 2:
                raise sqlite3.IntegrityError("simulated transaction failure")
            return real_log(connection, account_id, kind, body)

        with patch.object(app_module, "log_activity", side_effect=fail_second):
            with self.assertLogs(self.app.logger, level="ERROR"):
                response = self.client.post("/api/usage/import", json={"csv": self.usage_csv()})
        self.assertEqual(response.status_code, 500)
        self.assertIn("error", response.get_json())
        self.assertEqual(self.snapshot(), before)

    def test_zero_seats_and_missing_usage_have_defined_behavior(self):
        response = self.client.post("/api/usage/import", json={"csv": self.usage_csv([f"6,0,0,{self.today}"])})
        self.assertEqual(response.status_code, 200)
        row = self.detail(6)
        self.assertIsNone(row["adoption_pct"])
        self.assertEqual(row["health"]["state"], "healthy")
        self.assertEqual(self.detail(7)["health"]["label"], "Not onboarded")

    def test_csv_export_matches_filters_and_escapes_formulas(self):
        for value in ("=SUM(1,2)", "+cmd", "-1+2", "@SUM(A1)", "  =SUM(1,2)", "\ttext", "\ufeff  =SUM(1,2)"):
            self.assertTrue(safe_csv_cell(value).startswith("'"))
        self.client.patch("/api/accounts/1", json={"next_action": '=HYPERLINK("https://example.com","click")'})
        response = self.client.get("/api/export?q=cedar&view=attention")
        rows = list(csv.DictReader(io.StringIO(response.get_data(as_text=True))))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["company"], "Cedar & Loom")
        self.assertTrue(rows[0]["next_action"].startswith("'="))
        self.assertEqual(rows[0]["health"], "At risk")
        self.assertIn("attachment", response.headers["Content-Disposition"])

    def test_markdown_brief_includes_current_work_metrics_and_safe_notes(self):
        self.client.post("/api/accounts/1/notes", json={"body": "<script>bad</script> [unsafe](javascript:bad)"})
        response = self.client.get("/api/accounts/1/brief")
        text = response.get_data(as_text=True)
        for section in ("Current context", "Open onboarding work", "Open issues", "Account notes", "At risk", "24 / 8", "Connect the product catalog"):
            self.assertIn(section, text)
        self.assertNotIn("<script>", text)
        self.assertNotIn("[unsafe](javascript:bad)", text)
        self.assertIn('filename="accountbrief-1.md"', response.headers["Content-Disposition"])

    def test_template_is_a_valid_previewable_csv(self):
        response = self.client.get("/api/usage/template")
        self.assertEqual(response.status_code, 200)
        preview = self.client.post("/api/usage/preview", json={"csv": response.get_data(as_text=True)})
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(len(preview.get_json()["rows"]), 3)

    def test_consistent_json_errors_and_security_headers(self):
        requests = [("get", "/api/accounts/999", None), ("get", "/api/missing", None),
                    ("patch", "/api/tasks/999", {"status": "done"}), ("patch", "/api/issues/999", {"status": "resolved"})]
        for method, url, body in requests:
            response = getattr(self.client, method)(url, json=body) if body else getattr(self.client, method)(url)
            self.assertEqual(response.status_code, 404)
            self.assertIn("error", response.get_json())
        self.assertEqual(self.client.patch("/api/accounts/1", data="stage=Customer").status_code, 400)
        self.assertEqual(self.client.post("/api/accounts/1/notes", data="{bad", content_type="application/json").status_code, 400)
        response = self.client.get("/api/accounts")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    def test_legacy_database_migration_preserves_notes_and_account_edits(self):
        legacy = Path(self.directory.name) / "legacy.sqlite"
        with closing(sqlite3.connect(legacy)) as connection:
            connection.executescript("""
                CREATE TABLE accounts (id INTEGER PRIMARY KEY, company TEXT NOT NULL, sector TEXT NOT NULL,
                    size TEXT NOT NULL, stage TEXT NOT NULL, need TEXT NOT NULL, followup_date TEXT, next_action TEXT NOT NULL);
                CREATE TABLE notes (id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id),
                    body TEXT NOT NULL, source_url TEXT NOT NULL, created_at TEXT NOT NULL);
                INSERT INTO accounts VALUES (42, 'Existing company', 'Retail', '1–10', 'Customer', 'Existing need', NULL, 'My saved next action');
                INSERT INTO notes VALUES (99, 42, 'Keep this note', 'https://example.com/old', '2026-01-01T00:00:00+00:00');
            """)
        client = create_app(legacy).test_client()
        row = client.get("/api/accounts/42").get_json()
        self.assertEqual(row["next_action"], "My saved next action")
        self.assertEqual((row["notes"][0]["id"], row["notes"][0]["body"]), (99, "Keep this note"))
        self.assertEqual(row["health"]["label"], "No usage data")
        self.assertEqual(row["owner"], "Unassigned")
        self.assertEqual(len(client.get("/api/accounts").get_json()["accounts"]), 1)
        self.assertEqual(row["tasks"], [])
        self.assertEqual(create_app(legacy).test_client().get("/api/accounts/42").get_json(), row)


class HealthRuleTests(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 10, 29)
        self.base = {"stage": "Customer", "seats": 10, "active_users": 5, "last_active_date": self.today.isoformat()}

    def health(self, **changes):
        return calculate_health({**self.base, **changes}, [], [], self.today)

    def test_inactivity_thresholds_are_inclusive(self):
        for days, state in ((2, "healthy"), (3, "watch"), (6, "watch"), (7, "risk")):
            with self.subTest(days=days):
                self.assertEqual(self.health(last_active_date=(self.today - timedelta(days=days)).isoformat())["state"], state)

    def test_adoption_threshold_does_not_round_away_the_rule(self):
        self.assertEqual(self.health(active_users=5)["state"], "healthy")
        self.assertEqual(self.health(active_users=4)["state"], "watch")

    def test_missing_usage_does_not_hide_urgent_issues_or_blocked_tasks(self):
        missing = {**self.base, "seats": None, "active_users": None, "last_active_date": None}
        self.assertEqual(calculate_health(missing, [], [], self.today)["state"], "unknown")
        task = {"status": "blocked", "due_date": self.today.isoformat()}
        issue = {"status": "open", "severity": "urgent"}
        for tasks, issues in (([task], []), ([], [issue])):
            result = calculate_health(missing, tasks, issues, self.today)
            self.assertEqual(result["state"], "risk")
            self.assertIn("No complete usage snapshot.", result["reasons"])

    def test_completed_tasks_and_resolved_issues_do_not_raise_flags(self):
        tasks = [{"status": "done", "due_date": "2026-01-01"}]
        issues = [{"status": "resolved", "severity": "urgent"}]
        self.assertEqual(calculate_health(self.base, tasks, issues, self.today)["state"], "healthy")


if __name__ == "__main__":
    unittest.main()
