
from pathlib import Path
from playwright.sync_api import sync_playwright

PROFILE_DIR = Path(__file__).resolve().parent / "browser_profile"

with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=False
    )

    page = context.pages[0] if context.pages else context.new_page()

    page.goto("https://app.manatal.com")

    input("Make sure Manatal is logged in, then press ENTER...")

    print("\nURL:", page.url)
    print("TITLE:", page.title())

    print("\n--- VISIBLE LINKS ---")
    for link in page.locator("a:visible").all():
        text = link.inner_text().strip()
        href = link.get_attribute("href")
        if text or href:
            print(text, "=>", href)

    print("\n--- BUTTONS ---")
    for button in page.locator("button:visible").all():
        text = button.inner_text().strip()
        if text:
            print(text)

    page.screenshot(path="manatal_inspection.png", full_page=True)

    print("\nScreenshot saved: manatal_inspection.png")

    input("Press ENTER to close...")
    context.close()
