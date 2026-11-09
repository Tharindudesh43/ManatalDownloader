from __future__ import annotations

import utils.session
import json
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urljoin

from playwright.sync_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "browser_profile"
DOWNLOAD_DIR = BASE_DIR / "downloads"
CONTROL_URL = "http://127.0.0.1:8765"


class AutomationConfig:
    def __init__(self) -> None:
        self.open_files = False
        self.start = threading.Event()
        self.stop = threading.Event()
        self.status = {
            "state": "waiting",
            "message": "Start the download to process every client one by one.",
            "total_clients": 0,
            "completed_clients": 0,
            "current_client": "",
            "current_file": "",
            "downloaded_files": 0,
            "errors": [],
            "download_failures": [],
        }

    def update_status(self, **values: object) -> None:
        self.status.update(values)


def safe_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]', "_", value).strip(" .")
    return cleaned[:120] or "unnamed-client"


def control_page(config: AutomationConfig) -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Manatal attachment downloader</title>
<style>
body {{ font: 16px Segoe UI, sans-serif; max-width: 820px; margin: 36px auto; padding: 0 24px; }}
fieldset {{ border: 1px solid #bbb; padding: 18px; }} label {{ display: inline-block; width: 110px; margin: 8px 0; }}
button {{ margin-top: 20px; padding: 10px 18px; cursor: pointer; }} .notice {{ color: #176b2c; }}
#progress {{ background: #f4f6f8; padding: 16px; margin-top: 24px; border-radius: 6px; }}
#bar {{ height: 14px; background: #2574a9; width: 0; transition: width .3s; }}
#bar-wrap {{ background: #dce2e7; margin: 12px 0; }}
li {{ margin: 5px 0; color: #a33; }}
</style></head><body>
<h1>Manatal attachment downloader</h1>
<p>Every available attachment format will be downloaded from every client, one client at a time.</p>
<form method="post">
<p><label style="width:auto"><input type="checkbox" name="open_files"{" checked" if config.open_files else ""}>
Open each downloaded file automatically</label></p>
<button type="submit">Start download</button>
</form>
<section id="progress"><strong id="state">Waiting</strong><p id="message"></p>
<div id="bar-wrap"><div id="bar"></div></div>
<p id="counts"></p><p id="current"></p><h3>Errors</h3><ul id="errors"></ul>
<h3>Attachment download failures</h3><ul id="failures"></ul></section>
<script>
async function refresh() {{
  const response = await fetch('/api/status');
  const s = await response.json();
  document.getElementById('state').textContent = s.state;
  document.getElementById('message').textContent = s.message;
  const percent = s.total_clients ? (s.completed_clients / s.total_clients * 100) : 0;
  document.getElementById('bar').style.width = percent + '%';
  document.getElementById('counts').textContent =
    `Clients: ${{s.completed_clients}} / ${{s.total_clients}} | Files: ${{s.downloaded_files}}`;
  document.getElementById('current').textContent =
    (s.current_client ? `Current: ${{s.current_client}}` : '')
    + (s.current_file ? ` | File: ${{s.current_file}}` : '');
  document.getElementById('errors').innerHTML = s.errors.map(e => `<li>${{e}}</li>`).join('');
  document.getElementById('failures').innerHTML = s.download_failures
    .map(e => `<li>${{e.client}} — ${{e.file}}: ${{e.reason}}</li>`).join('');
}}
refresh(); setInterval(refresh, 1000);
</script></body></html>"""


def start_control_server(config: AutomationConfig) -> ThreadingHTTPServer:
    class ControlHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/api/status":
                body = json.dumps(config.status).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            else:
                body = control_page(config).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            values = parse_qs(self.rfile.read(length).decode("utf-8"))
            config.open_files = "open_files" in values
            config.update_status(state="starting", message="Waiting for Manatal login...")
            config.start.set()
            body = control_page(config).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 8765), ControlHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def collect_client_urls(page: Page, config: AutomationConfig) -> list[str]:
    page.goto("https://app.manatal.com/clients", wait_until="domcontentloaded", timeout=60000)
    wait_for_client_list(page, config)
    while True:
        previous = pagination_button(page, "keyboard_arrow_left")
        if previous is None or previous.is_disabled():
            break
        old_range = page_range(page)
        click_pagination(page, previous, old_range)

    urls: list[str] = []
    seen: set[str] = set()
    while True:
        page.wait_for_timeout(800)
        page_urls = {
            urljoin(page.url, href)
            for link in page.locator('a[href^="/clients/"]').all()
            for href in [link.get_attribute("href")]
            if href and re.fullmatch(r"/clients/\d+", href)
        }
        new_urls = page_urls - seen
        urls.extend(sorted(new_urls))
        seen.update(new_urls)

        next_button = pagination_button(page, "keyboard_arrow_right")
        if next_button is None or next_button.is_disabled():
            break
        old_range = page_range(page)
        click_pagination(page, next_button, old_range)
    return urls


def wait_for_client_list(page: Page, config: AutomationConfig) -> None:
    for _ in range(180):
        if "/login" in page.url:
            config.update_status(
                state="waiting",
                message="Log in to Manatal; the client list will load automatically.",
            )
        else:
            body = page.locator("body").inner_text()
            if "Results per page:" in body and pagination_button(page, "keyboard_arrow_right"):
                return
            config.update_status(state="loading", message="Waiting for the Manatal client table to load...")
        page.wait_for_timeout(1000)
    raise RuntimeError(
        f"Manatal client list did not load. Current URL: {page.url}. "
        "Check that the browser is logged in and the Clients page is accessible."
    )


def page_range(page: Page) -> str:
    match = re.search(r"\d+\s*-\s*\d+\s+of\s+\d+", page.locator("body").inner_text())
    return match.group(0) if match else ""


def wait_for_page_change(page: Page, old_range: str) -> None:
    page.wait_for_function(
        """(oldRange) => {
            const text = document.body.innerText;
            const match = text.match(/\\d+\\s*-\\s*\\d+\\s+of\\s+\\d+/);
            return match && match[0] !== oldRange;
        }""",
        arg=old_range,
        timeout=10000,
    )


def click_pagination(page: Page, button, old_range: str) -> None:
    for attempt in range(3):
        button.click(force=True)
        try:
            wait_for_page_change(page, old_range)
            return
        except PlaywrightTimeoutError:
            if attempt == 2:
                raise RuntimeError(f"Client list did not leave page {old_range}.")
            page.wait_for_timeout(1000)


def pagination_button(page: Page, icon: str):
    for button in page.locator("button:visible").all():
        if button.inner_text().strip() == icon:
            return button
    return None


def client_name(page: Page) -> str:
    heading = page.locator("h1:visible").first
    if heading.count():
        return heading.inner_text().strip()
    return page.title().strip() or "unnamed-client"


def download_client_attachments(
    context: BrowserContext, page: Page, client_url: str, config: AutomationConfig
) -> int:
    last_load_error = ""
    for attempt in range(3):
        try:
            attachment_url = f"{client_url}?tab=attachments"
            page.goto(attachment_url, wait_until="domcontentloaded", timeout=60000)
            if "challenge.rupt.dev" in page.url:
                raise RuntimeError(
                    "Manatal security challenge is open. Complete it in Chromium, then restart."
                )
            wait_for_attachment_page(page, timeout=45000)
            break
        except (PlaywrightTimeoutError, RuntimeError) as error:
            last_load_error = str(error)
            if attempt == 2:
                raise RuntimeError(
                    f"Attachment page did not finish loading after 3 attempts: {last_load_error}"
                ) from error
            page.wait_for_timeout(3000 * (attempt + 1))
    folder = DOWNLOAD_DIR / safe_name(client_name(page))
    downloaded = 0

    file_names = [
        name.strip()
        for name in page.locator('a.item:visible[href="javascript:void(0)"]').all_inner_texts()
        if name.strip()
    ]
    for file_name in file_names:
        last_error = ""
        for attempt in range(3):
            try:
                config.update_status(
                    current_file=file_name,
                    message=f"Downloading {file_name}",
                )
                file_link = page.get_by_text(file_name, exact=True).last
                row = file_link.locator(
                    'xpath=ancestor::*[.//button[.//i[normalize-space()="more_vert"]]][1]'
                )
                if not row.count():
                    raise RuntimeError("Attachment row is no longer available")
                row.locator("button").first.click(force=True)
                download_action = None
                for _ in range(25):
                    for action in page.locator('[data-testid="dropdown-item"]').all():
                        if action.is_visible() and "Download" in action.inner_text():
                            download_action = action
                            break
                    if download_action is not None:
                        break
                    page.wait_for_timeout(200)
                if download_action is None:
                    raise RuntimeError("Visible Download action was not shown")
                with page.expect_download(timeout=15000) as download_info:
                    download_action.evaluate("(element) => element.click()")
                download = download_info.value
                folder.mkdir(parents=True, exist_ok=True)
                target = folder / safe_name(download.suggested_filename or file_name)
                if target.exists():
                    target = folder / f"{target.stem}_{downloaded + 1}{target.suffix}"
                download.save_as(str(target))
                downloaded += 1
                if config.open_files:
                    webbrowser.open(target.as_uri())
                break
            except Exception as error:
                last_error = str(error)
                page.keyboard.press("Escape")
                page.wait_for_timeout(1000)
                if attempt == 2:
                    config.status["download_failures"].append(
                        {
                            "client": client_name(page),
                            "client_url": client_url,
                            "file": file_name or "(unknown filename)",
                            "reason": f"Failed after 3 attempts: {last_error}",
                        }
                    )
                    print(f"Attachment failed: {client_name(page)} / {file_name}: {last_error}")
    return downloaded


def wait_for_attachment_page(page: Page, timeout: int) -> None:
    page.wait_for_function(
        """() => {
            const text = document.body.innerText;
            const hasClientShell = text.includes("Jobs")
                && text.includes("Summary")
                && text.includes("Attachments");
            const hasAttachmentState = text.includes("Uploaded Files")
                || text.includes("No attachments uploaded yet");
            return hasClientShell && hasAttachmentState;
        }""",
        timeout=timeout,
    )


def run_download(context: BrowserContext, config: AutomationConfig) -> None:
    page = context.pages[0] if context.pages else context.new_page()
    while "/login" in page.url:
        config.update_status(state="waiting", message="Log in to Manatal, then the download will start automatically.")
        page.wait_for_timeout(1000)
    config.update_status(state="loading", message="Loading the client list...")
    try:
        clients = collect_client_urls(page, config)
    except Exception as error:
        message = f"Could not load clients: {error}"
        config.status["errors"].append(message)
        config.update_status(state="failed", message=message)
        print(message)
        return
    if not clients:
        message = "The client list loaded, but no client records were found."
        config.status["errors"].append(message)
        config.update_status(state="failed", message=message)
        print(message)
        return
    config.update_status(
        state="downloading",
        message="Downloading attachments one client at a time.",
        total_clients=len(clients),
    )
    print(f"Found {len(clients)} clients.")
    total = 0
    for number, client_url in enumerate(clients, start=1):
        if config.stop.is_set():
            break
        config.update_status(
            current_client=f"{number}/{len(clients)}",
            message=f"Opening client {number} of {len(clients)}",
        )
        try:
            count = download_client_attachments(context, page, client_url, config)
            total += count
            config.update_status(
                completed_clients=number,
                downloaded_files=total,
                message=f"Finished client {number} of {len(clients)}; downloaded {count} attachment(s)",
            )
            print(f"[{number}/{len(clients)}] {client_url} -> {count} attachment(s)")
        except Exception as error:
            config.status["errors"].append(f"{client_url}: {error}")
            config.update_status(completed_clients=number, message=f"Error on client {number}; continuing")
            print(f"[{number}/{len(clients)}] Failed {client_url}: {error}")
            if "security challenge" in str(error).lower():
                config.update_status(
                    state="paused",
                    message="Complete the Manatal security challenge in Chromium, then restart.",
                )
                break
        page.wait_for_timeout(2000)
        if number % 10 == 0:
            config.update_status(message=f"Cooling down after client {number} to avoid rate limits")
            page.wait_for_timeout(10000)
    config.update_status(
        state="finished",
        current_client="",
        current_file="",
        downloaded_files=total,
        message=f"Finished. Files saved in {DOWNLOAD_DIR}.",
    )
    print(f"Finished. Downloaded {total} attachment(s) to {DOWNLOAD_DIR}.")


def main() -> None:
    PROFILE_DIR.mkdir(exist_ok=True)
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    config = AutomationConfig()
    server = start_control_server(config)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), headless=False, accept_downloads=True
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://app.manatal.com/login", wait_until="domcontentloaded", timeout=60000)
        print("Manatal opened in Chromium. Log in manually if necessary.")
        webbrowser.open(CONTROL_URL)
        print(f"Choose file types at {CONTROL_URL}; progress is shown there.")
        config.start.wait()
        run_download(context, config)
        context.close()
    server.shutdown()


if __name__ == "__main__":
    main()
