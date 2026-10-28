"""Exercise AccountBrief against a fresh temporary database and capture real views.

Start a server with ACCOUNTBRIEF_DATABASE pointing to a disposable test database.
Run: python tests/browser_check.py --url http://127.0.0.1:5064
Requires Python Playwright and Chrome. No real accounts or messages are used.
"""
import argparse
import csv
import html
from datetime import date, timedelta
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

SCREENSHOTS = Path(__file__).resolve().parents[1] / "docs" / "screenshots"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:5064")
    args = parser.parse_args()
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1, accept_downloads=True)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(args.url)
        page.wait_for_load_state("networkidle")
        expect(page.locator(".account-row")).to_have_count(3)
        page.locator(".account-row", has_text="Cedar & Loom").click()
        expect(page.locator("#detail-name")).to_have_text("Cedar & Loom")
        expect(page.locator("#detail-health")).to_have_text("At risk")
        expect(page.locator("#health-reasons")).to_contain_text("blocked")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert page.evaluate("document.documentElement.scrollHeight <= window.innerHeight")
        print("Initial buttons:", page.get_by_role("button").all_text_contents())
        page.screenshot(path=str(SCREENSHOTS / "desktop.png"))

        # Screenshots are captured before mutations so the sample workspace stays coherent.
        before_import = page.request.get(f"{args.url}/api/accounts/1").json()
        csv_text = f"account_id,seats,active_users,last_active_date\n1,24,21,{date.today().isoformat()}\n"
        page.get_by_role("button", name="Import usage", exact=True).click()
        expect(page.get_by_role("dialog", name="Import usage snapshot")).to_be_visible()
        page.locator("#csv-input").fill(csv_text)
        page.get_by_role("button", name="Validate preview", exact=True).click()
        expect(page.locator("#import-preview")).to_be_visible()
        expect(page.locator("#preview-rows")).to_contain_text("Cedar & Loom")
        assert page.request.get(f"{args.url}/api/accounts/1").json()["active_users"] == before_import["active_users"]
        page.screenshot(path=str(SCREENSHOTS / "import-desktop.png"))
        page.get_by_role("button", name="Close import dialog", exact=True).click()

        mobile = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=1, is_mobile=True)
        mobile.on("pageerror", lambda error: errors.append(str(error)))
        mobile.goto(args.url)
        mobile.wait_for_load_state("networkidle")
        expect(mobile.locator("#queue")).to_be_visible()
        mobile.locator(".account-row", has_text="Cedar & Loom").click()
        expect(mobile.locator("#detail-name")).to_have_text("Cedar & Loom")
        expect(mobile.get_by_role("button", name="Back to accounts")).to_be_visible()
        assert mobile.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Mobile horizontal overflow"
        assert mobile.evaluate("document.documentElement.scrollHeight <= window.innerHeight"), "Mobile page should use internal scrolling"
        mobile.screenshot(path=str(SCREENSHOTS / "mobile.png"))
        mobile.get_by_role("button", name="Back to accounts").click()
        expect(mobile.locator("#queue")).to_be_visible()
        expect(mobile.locator("#workspace")).to_be_hidden()
        mobile.close()

        page.get_by_role("button", name="All accounts", exact=False).click()
        expect(page.locator(".account-row")).to_have_count(8)
        page.get_by_role("textbox", name="Search accounts").fill("no-such-record")
        expect(page.get_by_text("No matching accounts", exact=True)).to_be_visible()
        page.get_by_role("button", name="Clear search", exact=True).click()
        expect(page.locator(".account-row")).to_have_count(8)
        page.locator(".account-row", has_text="Cedar & Loom").click()

        # Keyboard tabs behave as one tab stop and support the arrow keys.
        expect(page.get_by_role("tab", name="Overview", exact=True)).to_be_enabled()
        page.get_by_role("tab", name="Overview", exact=True).focus()
        page.keyboard.press("ArrowRight")
        expect(page.get_by_role("tab", name="Onboarding & issues", exact=False)).to_be_focused()
        expect(page.locator("#panel-work")).to_be_visible()
        blocked = page.get_by_role("combobox", name="Status for Connect the product catalog", exact=True)
        blocked.select_option("done")
        expect(page.locator("#task-status")).to_contain_text("Task status saved")
        expect(page.get_by_role("combobox", name="Status for Connect the product catalog", exact=True)).to_have_value("done")
        page.get_by_role("button", name="+ Add task", exact=True).click()
        page.get_by_label("Task title", exact=True).fill("Confirm the weekly account review")
        page.locator("#task-owner").fill("Practice owner")
        page.locator("#task-date").fill((date.today()+timedelta(days=2)).isoformat())
        page.get_by_role("button", name="Add task", exact=True).click()
        expect(page.locator("#task-status")).to_contain_text("Onboarding task added")
        expect(page.locator("#tasks")).to_contain_text("Confirm the weekly account review")
        page.get_by_role("button", name="+ Log issue", exact=True).click()
        page.get_by_label("Issue title", exact=True).fill("Practice urgent issue for browser validation")
        page.locator("#issue-severity").select_option("urgent")
        page.get_by_role("button", name="Log issue", exact=True).click()
        expect(page.locator("#issue-status")).to_contain_text("Support issue logged")
        expect(page.locator("#detail-health")).to_have_text("At risk")
        page.get_by_role("button", name="Resolve issue: Practice urgent issue for browser validation", exact=True).click()
        expect(page.locator("#issue-status")).to_contain_text("Issue status saved")

        page.get_by_role("tab", name="Notes & activity", exact=True).click()
        page.get_by_role("textbox", name="New account note", exact=True).fill("<strong>Test observation</strong>: keep this as literal text, then confirm the handoff.")
        page.locator("#note-source").fill("")
        page.get_by_role("button", name="Save note", exact=True).click()
        expect(page.locator("#note-status")).to_contain_text("Account note saved")
        expect(page.locator("#notes")).to_contain_text("<strong>Test observation</strong>")
        expect(page.locator("#notes strong")).to_have_count(0)
        expect(page.locator("#activity")).to_contain_text("note")

        page.get_by_role("tab", name="Overview", exact=True).click()
        page.locator("#edit-owner").fill("Review owner")
        page.locator("#edit-action").fill("Confirm the mapping and review the latest usage snapshot.")
        page.get_by_role("button", name="Save next action", exact=True).click()
        expect(page.locator("#action-status")).to_contain_text("Next action saved")
        page.reload(wait_until="networkidle")
        page.locator(".account-row", has_text="Cedar & Loom").click()
        expect(page.locator("#edit-owner")).to_have_value("Review owner")
        expect(page.locator("#edit-action")).to_have_value("Confirm the mapping and review the latest usage snapshot.")

        page.get_by_role("button", name="Import usage", exact=True).click()
        page.locator("#csv-input").fill("account_id,seats,active_users,last_active_date\n1,2,7,2020-01-01")
        page.get_by_role("button", name="Validate preview", exact=True).click()
        expect(page.locator("#import-error")).to_be_visible()
        expect(page.get_by_role("button", name="Apply snapshot", exact=True)).to_be_hidden()
        page.locator("#csv-input").fill(csv_text)
        page.get_by_role("button", name="Validate preview", exact=True).click()
        expect(page.locator("#import-preview")).to_be_visible()
        page.get_by_role("button", name="Apply snapshot", exact=True).click()
        expect(page.get_by_role("dialog", name="Import usage snapshot")).to_be_hidden()
        expect(page.locator("#usage-active")).to_have_text("21")
        assert page.request.get(f"{args.url}/api/accounts/1").json()["active_users"] == 21

        with page.expect_download() as downloaded:
            page.get_by_role("link", name="Export brief", exact=False).click()
        brief = html.unescape(Path(downloaded.value.path()).read_text())
        assert "Cedar & Loom" in brief and "21" in brief and "Confirm the weekly account review" in brief
        page.get_by_role("button", name="All accounts", exact=False).click()
        expect(page.locator(".account-row")).to_have_count(8)
        page.get_by_role("textbox", name="Search accounts").fill("Cedar")
        expect(page.locator(".account-row")).to_have_count(1)
        with page.expect_download() as exported:
            page.get_by_role("link", name="Export accounts", exact=False).click()
        with open(exported.value.path(), newline="") as file:
            rows = list(csv.reader(file))
        assert len(rows) == 2 and "Cedar & Loom" in rows[1]
        page.get_by_role("button", name="Clear search", exact=True).click()

        page.get_by_role("button", name="+ New account", exact=True).click()
        page.get_by_label("Company name", exact=True).fill("Cedar & Loom")
        page.locator("#new-sector").fill("Software")
        page.locator("#new-owner").fill("Practice owner")
        page.get_by_role("button", name="Create account", exact=True).click()
        expect(page.locator("#new-account-error")).to_contain_text("already exists")
        page.get_by_label("Company name", exact=True).fill("Browser QA Account")
        page.get_by_role("button", name="Create account", exact=True).click()
        expect(page.get_by_role("dialog", name="New account", exact=True)).to_be_hidden()
        expect(page.locator("#detail-name")).to_have_text("Browser QA Account")
        expect(page.locator("#detail-health")).to_have_text("No usage data")
        expect(page.locator("#usage-last")).to_have_text("No snapshot")
        expect(page.locator(".account-row")).to_have_count(9)

        page.route("**/api/accounts?**", lambda route: route.abort())
        page.locator("#search").fill("Cedar")
        expect(page.locator("#app-error")).to_be_visible()
        page.unroute("**/api/accounts?**")
        page.get_by_role("button", name="Try again", exact=True).click()
        expect(page.locator("#app-error")).to_be_hidden()
        expect(page.locator(".account-row")).to_have_count(1)
        assert not errors, errors
        browser.close()
    print("PASS: compact desktop/mobile, queue/back navigation, filters, tasks/issues/notes, persistence, CSV preview/apply, brief/export, account creation/duplicate errors, keyboard tabs, error retry, safe note text, no page errors.")


if __name__ == "__main__":
    main()
