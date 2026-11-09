"""Render docs/screenshots/banner.html to assets/banner.png (1200x600 CSS px, 2x = 2400x1200, 2:1)."""
from pathlib import Path
from playwright.sync_api import sync_playwright

here = Path(__file__).resolve().parent
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 1200, "height": 600}, device_scale_factor=2)
    pg.goto((here / "banner.html").as_uri())
    pg.wait_for_timeout(300)
    pg.screenshot(path=str(here.parent.parent / "assets" / "banner.png"))
    b.close()
