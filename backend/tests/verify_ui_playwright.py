"""Real-browser Playwright E2E verification across all 8 BillApp views & workflows.

Starts a temporary local server (no-proxy localhost), drives Microsoft Edge / Chromium
through every page, imports synthetic statements (CSV, XLSX, encrypted PDF), tests FX manual
entry, dashboard charts, transactions, rules, accounts, backup download, and data reset,
while asserting zero console errors, zero uncaught page errors, and zero HTTP 5xx responses.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

from playwright.sync_api import sync_playwright
import uvicorn

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from billapp.app import create_app
from tests.fixtures.synthetic_alipay import generate_synthetic_alipay_csv
from tests.fixtures.synthetic_wechat import generate_synthetic_wechat_xlsx
from tests.fixtures.synthetic_pdf import generate_synthetic_boc_pdf, get_default_boc_rows


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(port: int) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(100):
        try:
            with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=0.5) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("Backend server did not become ready in time")


def run_browser_e2e(exe_path: Path | None = None) -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db_path = tmp / "e2e_browser.db"
        alipay_file = tmp / "alipay_sample.csv"
        wechat_file = tmp / "wechat_sample.xlsx"
        boc_file = tmp / "boc_encrypted.pdf"

        alipay_file.write_bytes(generate_synthetic_alipay_csv())
        wechat_file.write_bytes(generate_synthetic_wechat_xlsx())
        boc_file.write_bytes(
            generate_synthetic_boc_pdf(
                rows=get_default_boc_rows(),
                password="boc123",
            )
        )

        port = _find_free_port()
        proc: subprocess.Popen[bytes] | None = None
        server: uvicorn.Server | None = None
        thread: threading.Thread | None = None
        if exe_path is not None:
            proc = subprocess.Popen([str(exe_path), "--no-browser", "--port", str(port), "--db", str(db_path)])
        else:
            app = create_app(db_path=str(db_path))
            config = uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="warning", use_colors=False)
            server = uvicorn.Server(config)
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
        _wait_ready(port)

        base_url = f"http://127.0.0.1:{port}"
        console_errors: list[str] = []
        page_errors: list[str] = []
        server_5xx: list[str] = []

        try:
            with sync_playwright() as p:
                launch_kwargs = {"headless": True, "args": ["--no-proxy-server"]}
                try:
                    browser = p.chromium.launch(channel="msedge", **launch_kwargs)
                except Exception:
                    browser = p.chromium.launch(channel="chrome", **launch_kwargs)

                context = browser.new_context(viewport={"width": 1280, "height": 860}, accept_downloads=True)
                page = context.new_page()

                page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
                page.on("pageerror", lambda exc: page_errors.append(str(exc)))
                page.on(
                    "response",
                    lambda resp: server_5xx.append(f"{resp.status} {resp.url}") if resp.status >= 500 else None,
                )

                # 1. Dashboard initial empty state
                page.goto(f"{base_url}/#/dashboard", wait_until="networkidle")
                page.wait_for_selector(".dashboard-view")
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Horizontal overflow on /dashboard"

                # 2. Import Alipay CSV + WeChat XLSX + encrypted BOC PDF
                page.goto(f"{base_url}/#/import", wait_until="networkidle")
                page.wait_for_selector(".import-view")
                file_input = page.locator(".drop-zone input[type='file']")
                file_input.set_input_files([str(alipay_file), str(wechat_file), str(boc_file)])

                # Wait for BOC password prompt modal or button
                page.wait_for_selector("text=需输入密码", timeout=15000)
                pwd_btn = page.get_by_role("button", name="输入密码").first
                if pwd_btn.is_visible():
                    pwd_btn.click()
                page.wait_for_selector(".el-dialog input[type='password']", timeout=10000)
                page.locator(".el-dialog input[type='password']").fill("boc123")
                page.locator(".el-dialog").get_by_role("button", name="解锁并解析").click()

                # Wait until all 3 files are ready and commit button shows (3)
                page.wait_for_selector("button:has-text('全部导入 (3)')", timeout=15000)
                page.locator("button:has-text('全部导入 (3)')").click()
                page.wait_for_selector("text=完成导入", timeout=15000)

                # 3. Dashboard with real data + narrow viewport check (1024x700)
                page.goto(f"{base_url}/#/dashboard", wait_until="networkidle")
                page.wait_for_selector(".kpi-grid")
                kpi_text = page.locator(".kpi-grid").inner_text()
                assert "NaN" not in kpi_text and "undefined" not in kpi_text, f"Invalid KPI text: {kpi_text}"
                page.set_viewport_size({"width": 1024, "height": 700})
                page.wait_for_timeout(300)
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Horizontal overflow at 1024x700"
                page.set_viewport_size({"width": 1280, "height": 860})

                # 4. Transactions list + detail drawer
                page.goto(f"{base_url}/#/transactions", wait_until="networkidle")
                page.wait_for_selector(".el-table__row", timeout=10000)
                assert page.locator(".el-table__row").count() > 0
                page.locator(".el-table__row").first.get_by_role("button", name="查看").click()
                page.locator(".el-drawer").wait_for(state="visible", timeout=5000)
                page.keyboard.press("Escape")
                page.wait_for_timeout(200)

                # 5. Review view
                page.goto(f"{base_url}/#/review", wait_until="networkidle")
                page.wait_for_selector(".review-view", timeout=10000)

                # 6. Rules & categories view
                page.goto(f"{base_url}/#/rules", wait_until="networkidle")
                page.wait_for_selector(".rules-view", timeout=10000)
                assert len(page.locator(".rules-view").inner_text().strip()) > 20

                # 7. Accounts view
                page.goto(f"{base_url}/#/accounts", wait_until="networkidle")
                page.wait_for_selector(".accounts-view", timeout=10000)
                assert "中国银行" in page.locator(".accounts-view").inner_text() or "支付宝" in page.locator(".accounts-view").inner_text()

                # 8. Settings view: all tabs + manual FX rate + backup + reset
                page.goto(f"{base_url}/#/settings", wait_until="networkidle")
                page.wait_for_selector(".settings-view", timeout=10000)

                page.get_by_role("tab", name="去重与对账规则").click()
                page.get_by_role("tab", name="退款与币种").click()
                page.get_by_role("button", name="手动补录汇率").click()
                page.wait_for_selector(".el-dialog:has-text('手动补录汇率')", timeout=5000)
                dialog = page.locator(".el-dialog:has-text('手动补录汇率')")
                dialog.locator(".el-input-number input").fill("7.2500")
                dialog.get_by_role("button", name="保存汇率").click()
                page.wait_for_selector("text=7.25", timeout=10000)

                page.get_by_role("tab", name="大语言模型 (AI 辅助)").click()
                page.get_by_role("tab", name="关于与数据").click()
                about_text = page.locator(".settings-view").inner_text()
                assert str(db_path) in about_text, f"Expected active db_path {db_path} in SettingsView, got: {about_text}"

                # Backup download check
                with page.expect_download() as dl_info:
                    page.get_by_role("button", name="导出数据库备份 (.db)").click()
                dl_path = dl_info.value.path()
                assert dl_path and Path(dl_path).read_bytes().startswith(b"SQLite format 3\x00")

                # Reset data flow check
                page.get_by_role("button", name="清空账单数据").click()
                reset_dialog = page.locator(".el-dialog:has-text('清空账单数据')")
                reset_dialog.locator("input[placeholder='确认清空']").fill("确认清空")
                reset_dialog.get_by_role("button", name="确认清空数据").click()
                page.wait_for_selector("text=账单数据已清空", timeout=10000)

                # Verify transactions emptied
                page.goto(f"{base_url}/#/transactions", wait_until="networkidle")
                page.wait_for_timeout(400)
                assert page.locator(".el-table__row").count() == 0

                # 9. Mapping wizard view loads cleanly
                page.goto(f"{base_url}/#/import/mapping", wait_until="networkidle")
                page.wait_for_selector(".mapping-wizard-layout", timeout=10000)

                context.close()
                browser.close()
        finally:
            if proc is not None:
                proc.terminate()
                proc.wait(timeout=5)
            if server is not None and thread is not None:
                server.should_exit = True
                thread.join(timeout=3)

        assert not console_errors, f"Console errors observed: {console_errors}"
        assert not page_errors, f"Uncaught page errors observed: {page_errors}"
        assert not server_5xx, f"HTTP 5xx responses observed: {server_5xx}"
        target = str(exe_path) if exe_path is not None else "source app"
        print(f"Playwright real-browser E2E across all 8 views ({target}) PASSED cleanly (0 console errors, 0 page errors, 0 HTTP 5xx).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Playwright real-browser E2E for BillApp")
    parser.add_argument("--exe", type=Path, default=None, help="Optional path to packaged BillApp.exe")
    cli_args = parser.parse_args()
    run_browser_e2e(exe_path=cli_args.exe)
