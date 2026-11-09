"""Screenshot a local HTML file at 1600x1000 CSS px. Usage: shot_html.py in.html out.png [scale]"""
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

src, out = Path(sys.argv[1]).resolve(), sys.argv[2]
scale = int(sys.argv[3]) if len(sys.argv) > 3 else 2
with sync_playwright() as p:
    browser = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    page = browser.new_page(viewport={"width": 1600, "height": 1000}, device_scale_factor=scale)
    page.goto(src.as_uri())
    page.wait_for_timeout(500)
    page.screenshot(path=out)
    browser.close()
