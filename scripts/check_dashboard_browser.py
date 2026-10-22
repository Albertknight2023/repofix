"""Browser smoke check for the local dashboard (requires Python Playwright)."""

from pathlib import Path
from http.server import ThreadingHTTPServer
from threading import Thread

from playwright.sync_api import sync_playwright
from repofix.dashboard.server import make_handler


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "artifacts" / "dashboard"
OUTPUT.mkdir(parents=True, exist_ok=True)

server = ThreadingHTTPServer(
    ("127.0.0.1", 0),
    make_handler(
        (ROOT / "benchmarks" / "manifest.yaml", ROOT / "benchmarks" / "real" / "manifest.yaml"),
        ROOT / "runs",
        ROOT / "runs" / "evaluations",
    ),
)
server_thread = Thread(target=server.serve_forever, daemon=True)
server_thread.start()

try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"http://127.0.0.1:{server.server_port}", wait_until="networkidle")
        assert page.get_by_role("heading", name="自动修复实验台").is_visible()
        assert page.locator(".task-card").count() >= 11
        assert page.locator(".stat-card").nth(1).locator("strong").inner_text() == "6"
        page.locator(".task-card[data-id='bugsinpy-tqdm-1']").click()
        assert page.locator(".selected-issue h3").inner_text() == "bugsinpy-tqdm-1"
        assert page.locator(".eval-class").inner_text() in {"参考补丁自检", "部分任务试点", "模型完整实验"}
        page.locator(".task-card[data-id='pagination-zero-page-size']").click()
        page.wait_for_selector(".diff-line.add")
        assert "page_size" in page.locator(".diff-code").inner_text()
        page.get_by_role("button", name="真实缺陷", exact=True).click()
        assert page.locator(".task-card").count() >= 6
        page.evaluate("window.scrollTo(0, 0)")
        page.screenshot(path=str(OUTPUT / "desktop.png"), full_page=True)

        page.set_viewport_size({"width": 390, "height": 844})
        page.reload(wait_until="networkidle")
        assert page.get_by_role("heading", name="自动修复实验台").is_visible()
        page.screenshot(path=str(OUTPUT / "mobile.png"), full_page=True)
        assert not errors, errors
        browser.close()
finally:
    server.shutdown()
    server.server_close()
    server_thread.join(timeout=5)
print("Dashboard browser smoke check passed.")
