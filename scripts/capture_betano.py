"""One-off: open betano.pt tennis in a visible browser, wait, dump every JSON response.

python scripts/capture_betano.py [seconds]
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "raw" / "betano_sample.json"
WAIT_S = int(sys.argv[1]) if len(sys.argv) > 1 else 20

captured = []


def on_response(resp):
    if "application/json" not in resp.headers.get("content-type", ""):
        return
    try:
        captured.append({"url": resp.url, "status": resp.status, "body": resp.json()})
    except Exception as e:  # body gone (redirect/navigation) or not valid JSON
        captured.append({"url": resp.url, "status": resp.status, "error": str(e)})


with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        str(ROOT / "data" / "browser_profile"), headless=False, locale="pt-PT",
        viewport={"width": 1280, "height": 900})
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.on("response", on_response)
    page.goto("https://www.betano.pt/sport/tenis/", wait_until="domcontentloaded")
    print(f"Waiting {WAIT_S}s - solve any challenge in the window if one appears.")
    page.wait_for_timeout(WAIT_S * 1000)
    (ROOT / "data" / "raw" / "betano_page.html").write_text(page.content(), encoding="utf-8")
    ctx.close()

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(captured, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"{len(captured)} JSON responses -> {OUT}")
