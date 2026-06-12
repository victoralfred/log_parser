"""Browser smoke test: full click-through of the dashboard in headless
Chromium. Skips cleanly when playwright or its browser is unavailable."""

import socket
import threading
import time

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")

import uvicorn  # noqa: E402

from logscope.web.app import create_app  # noqa: E402
from tests.test_flare import make_flare  # noqa: E402


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("ui")
    flare = make_flare(tmp / "uiflare")
    port = _free_port()
    app = create_app(db_path=str(tmp / "ui.db"), uploads_root=tmp / "up")
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not srv.started:
        if time.time() > deadline:
            raise TimeoutError("uvicorn did not start")
        time.sleep(0.05)
    yield {"url": f"http://127.0.0.1:{port}", "flare": str(flare)}
    srv.should_exit = True
    thread.join(timeout=5)


def test_dashboard_click_through(server):
    try:
        pw = playwright_sync.sync_playwright().start()
        browser = pw.chromium.launch()
    except Exception as exc:  # browser binary missing
        pytest.skip(f"chromium unavailable: {exc}")
    try:
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(server["url"])

        # scanner sidebar lists the three built-ins
        page.wait_for_selector(".scanner")
        names = page.locator(".scanner .name").all_inner_texts()
        assert {"agent-files", "flare", "journald"} <= set(names)

        # run a scan of the synthetic flare (agent-files + flare only;
        # journald would pull in the host's real journal)
        page.fill("#root-input", server["flare"])
        for box in page.locator(".scanner-pick").all():
            if box.input_value() == "journald":
                box.uncheck()
        page.click("#scan-btn")
        page.wait_for_function(
            "document.querySelector('#scan-status').textContent.includes('scan done')",
            timeout=30000)

        # summary matrix rendered with data
        page.wait_for_selector("#matrix table")
        assert "agent" in page.locator("#matrix").inner_text()

        # record click -> console modal; Esc closes
        page.wait_for_selector(".rec-row")
        page.locator(".rec-row").first.click()
        page.wait_for_selector("#modal-overlay:not([hidden])")
        assert page.locator("#modal-body.modal-console").count() == 1
        page.keyboard.press("Escape")
        page.wait_for_selector("#modal-overlay", state="hidden")

        # flare files -> config document modal with variables table
        page.wait_for_selector("#flare-section:not([hidden])")
        page.locator(".doc-item", has_text="datadog.yaml").first.click()
        page.wait_for_selector("#modal-overlay:not([hidden])")
        assert page.locator(".var-table").count() == 1
        assert "api_key" in page.locator("#modal-body").inner_text()
        page.keyboard.press("Escape")

        # health report section rendered with a verdict
        page.wait_for_selector("#report-section:not([hidden])")
        assert page.locator(".verdict").count() == 1

        assert errors == [], f"JS errors: {errors}"
    finally:
        browser.close()
        pw.stop()
