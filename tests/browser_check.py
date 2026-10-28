"""Optional browser QA. Run against a fresh demo database, not your personal data."""

import csv
from datetime import date, timedelta
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

SCREENSHOTS = Path(__file__).resolve().parents[1] / "docs" / "screenshots"
SCREENSHOTS.mkdir(parents=True, exist_ok=True)


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, channel="chrome")
    page = browser.new_page(viewport={"width": 1440, "height": 1100}, device_scale_factor=1)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto("http://127.0.0.1:5062")
    page.wait_for_load_state("networkidle")
    expect(page.locator(".account-row")).to_have_count(8)
    page.locator(".account-row", has_text="Cedar & Loom").click()
    expect(page.locator("#detail-name")).to_have_text("Cedar & Loom")
    page.screenshot(path=str(SCREENSHOTS / "desktop.png"), full_page=True)

    mobile = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=1)
    mobile.goto("http://127.0.0.1:5062")
    mobile.wait_for_load_state("networkidle")
    mobile.locator(".account-row", has_text="Cedar & Loom").click()
    expect(mobile.locator("#detail-name")).to_have_text("Cedar & Loom")
    assert mobile.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Mobile horizontal overflow"
    mobile.screenshot(path=str(SCREENSHOTS / "mobile.png"), full_page=True)
    mobile.close()

    page.get_by_role("textbox", name="Search accounts").fill("no-such-account")
    expect(page.get_by_text("No matching accounts", exact=True)).to_be_visible()
    page.locator("#search").fill("")
    page.locator("#stage-filter").select_option("Qualified")
    page.locator("#overdue-filter").check()
    expect(page.locator(".account-row")).to_have_count(1)
    expect(page.locator(".account-row")).to_contain_text("Cedar & Loom")
    page.locator("#overdue-filter").uncheck()
    page.locator("#stage-filter").select_option("")
    expect(page.locator(".account-row")).to_have_count(8)
    page.locator(".account-row", has_text="Cedar & Loom").click()
    page.locator("#edit-stage").select_option("Proposal")
    page.locator("#edit-date").fill((date.today() + timedelta(days=2)).isoformat())
    page.locator("#edit-action").fill("Prepare a concise needs summary for the sample demo.")
    page.get_by_role("button", name="Save next action").click()
    expect(page.locator("#action-status")).to_contain_text("Next action saved")
    page.reload(wait_until="networkidle")
    page.locator(".account-row", has_text="Cedar & Loom").click()
    expect(page.locator("#edit-stage")).to_have_value("Proposal")
    expect(page.locator("#edit-action")).to_have_value("Prepare a concise needs summary for the sample demo.")
    page.locator("#note-body").fill("<strong>Sample observation</strong>: validate the need in a discovery conversation.")
    page.locator("#note-source").fill("https://example.com/portfolio-evidence")
    page.get_by_role("button", name="Add evidence note").click()
    expect(page.locator("#note-status")).to_contain_text("Evidence note saved")
    expect(page.locator("#notes .note-card")).to_have_count(2)
    expect(page.locator("#notes strong")).to_have_count(0)
    page.reload(wait_until="networkidle")
    page.locator(".account-row", has_text="Cedar & Loom").click()
    expect(page.locator("#notes .note-card")).to_have_count(2)

    with page.expect_download() as downloaded:
        page.get_by_role("link", name="Export CSV").click()
    with open(downloaded.value.path(), newline="") as file:
        rows = list(csv.reader(file))
    assert len(rows) == 9 and len(rows[0]) == 7

    page.route("**/api/accounts?**", lambda route: route.abort())
    page.locator("#search").fill("Cedar")
    expect(page.locator("#app-error")).to_be_visible()
    page.unroute("**/api/accounts?**")
    page.get_by_role("button", name="Try again").click()
    expect(page.locator("#app-error")).to_be_hidden()
    expect(page.locator(".account-row")).to_have_count(1)
    page.locator("#search").focus()
    page.keyboard.press("Tab")
    expect(page.locator("#stage-filter")).to_be_focused()
    assert not errors, errors
    browser.close()
    print("Browser QA passed: desktop/mobile, filters, empty/error retry, persistence, evidence text safety, CSV, keyboard focus, no page errors.")
