"""Boundary and persistence checks, using a fresh SQLite file per test."""

import csv
import io
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from app import create_app, safe_csv_cell


class AccountBriefTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "test.sqlite"
        self.app = create_app(self.database)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self):
        self.directory.cleanup()

    def update(self, **changes):
        data = {"stage": "Qualified", "followup_date": date.today().isoformat(), "next_action": "Review the research brief."}
        data.update(changes)
        return self.client.patch("/api/accounts/1", json=data)

    def test_seed_is_idempotent_and_summary_counts_real_records(self):
        create_app(self.database)
        data = self.client.get("/api/accounts").get_json()
        self.assertEqual(data["summary"], {"total": 8, "active": 7, "overdue": 3, "evidence_notes": 1})
        self.assertEqual(len(data["accounts"]), 8)

    def test_search_stage_and_overdue_filters_combine(self):
        rows = self.client.get("/api/accounts?q=cedar&stage=Qualified&overdue=1").get_json()["accounts"]
        self.assertEqual([row["company"] for row in rows], ["Cedar & Loom"])
        self.assertEqual(self.client.get("/api/accounts?q=%25").get_json()["accounts"], [])
        self.assertEqual(self.client.get("/api/accounts?q=%27%20OR%201%3D1--").get_json()["accounts"], [])
        self.assertEqual(self.client.get("/api/accounts?stage=Unknown").status_code, 400)

    def test_customer_followups_remain_actionable_but_closed_accounts_do_not(self):
        past = (date.today() - timedelta(days=1)).isoformat()
        self.assertEqual(self.update(stage="Customer", followup_date=past).status_code, 200)
        self.assertTrue(self.client.get("/api/accounts/1").get_json()["overdue"])
        self.assertEqual(self.update(stage="Closed", followup_date=past).status_code, 200)
        self.assertFalse(self.client.get("/api/accounts/1").get_json()["overdue"])

    def test_update_persists_across_app_instances(self):
        self.assertEqual(self.update(stage="Proposal", next_action="Prepare a two-slide demo.").status_code, 200)
        other_client = create_app(self.database).test_client()
        row = other_client.get("/api/accounts/1").get_json()
        self.assertEqual((row["stage"], row["next_action"]), ("Proposal", "Prepare a two-slide demo."))

    def test_invalid_updates_do_not_partially_write(self):
        before = self.client.get("/api/accounts/1").get_json()
        for fields in ({"stage": "Pretend"}, {"followup_date": "2026-02-30"}, {"next_action": ""}, {"stage": 12}):
            with self.subTest(fields=fields):
                self.assertEqual(self.update(**fields).status_code, 400)
        after = self.client.get("/api/accounts/1").get_json()
        self.assertEqual(before, after)
        self.assertEqual(self.client.patch("/api/accounts/1", data="stage=Qualified").status_code, 400)

    def test_evidence_requires_safe_source_and_persists_as_text(self):
        for source in ("javascript:alert(1)", "https://user:secret@example.com", "not-a-url", "https://example.com/has space", "https://example.com:badport", "https://example.com:99999"):
            with self.subTest(source=source):
                self.assertEqual(self.client.post("/api/accounts/2/notes", json={"body": "An observation", "source_url": source}).status_code, 400)
        note = '<script>alert("x")</script> is text, not markup.'
        response = self.client.post("/api/accounts/2/notes", json={"body": note, "source_url": "https://example.com/evidence"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(create_app(self.database).test_client().get("/api/accounts/2").get_json()["notes"][0]["body"], note)

    def test_csv_preserves_structure_and_neutralizes_formulas(self):
        for value in ("=SUM(1,2)", "+cmd", "-1+2", "@SUM(A1)", "  =SUM(1,2)", "\ttext"):
            self.assertTrue(safe_csv_cell(value).startswith("'"))
        self.assertEqual(safe_csv_cell('A "quoted", value'), 'A "quoted", value')
        self.update(next_action='=HYPERLINK("https://example.com","click")')
        response = self.client.get("/api/export?q=cedar")
        rows = list(csv.reader(io.StringIO(response.get_data(as_text=True))))
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(rows[1]), len(rows[0]))
        self.assertTrue(rows[1][5].startswith("'="))
        self.assertIn("attachment", response.headers["Content-Disposition"])

    def test_empty_state_not_found_and_security_headers(self):
        self.assertEqual(self.client.get("/api/accounts?q=unmatched-name").get_json()["accounts"], [])
        self.assertEqual(self.client.get("/api/accounts/999").status_code, 404)
        self.assertEqual(self.client.post("/api/accounts/999/notes", json={"body": "note", "source_url": "https://example.com"}).status_code, 404)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")


if __name__ == "__main__":
    unittest.main()
