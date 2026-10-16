#!/usr/bin/env python3
"""Check pages at iPhone size: horizontal overflow + screenshots in .shots/.

Desktop headless Chrome won't lay out narrower than ~500 px, so plain --window-size screenshots
lie about mobile. This uses Playwright mobile emulation with the locally installed Chrome.

  .venv/bin/python tools/mobile_check.py                 # default pages, dark mode
  .venv/bin/python tools/mobile_check.py /plan --light   # specific pages, light mode
  .venv/bin/python tools/mobile_check.py --desktop       # 1280 px browser window instead of an iPhone
  .venv/bin/python tools/mobile_check.py --desktop --width=1000   # e.g. inside HA's ingress frame
  .venv/bin/python tools/mobile_check.py / --screens=2   # only the first two screens, readable at full size

Needs the app running (default http://localhost:8099, override with TC_URL). Exit code 1 on overflow.
"""
import os
import sqlite3
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("TC_URL", "http://localhost:8099")
SHOTS = Path(__file__).resolve().parent.parent / ".shots"
OVERFLOW_JS = """() => {
  const vw = document.documentElement.clientWidth, out = [];
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.right > vw + 1 && r.width > 0)
      out.push(`${el.tagName.toLowerCase()}.${[...el.classList].join('.')} right=${Math.round(r.right)}`);
  }
  return {vw, scroll: document.documentElement.scrollWidth, offenders: out.slice(0, 10)};
}"""


def default_pages() -> list[str]:
    pages = ["/", "/plan", "/coach", "/analysis", "/profile", "/settings"]
    db = Path(os.environ.get("TC_DATA_DIR", "/tmp/tc-demo")) / "training.db"
    if db.exists():
        row = sqlite3.connect(db).execute("SELECT id FROM activities ORDER BY start_date DESC LIMIT 1").fetchone()
        if row:
            pages.append(f"/activity/{row[0]}")
    return pages


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    scheme = "light" if "--light" in sys.argv else "dark"
    SHOTS.mkdir(exist_ok=True)
    failed = False
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        desktop = "--desktop" in sys.argv
        device = "desktop" if desktop else "iphone"
        if desktop:
            width = next((int(a.split("=")[1]) for a in sys.argv if a.startswith("--width=")), 1280)
            page = browser.new_page(viewport={"width": width, "height": 900}, color_scheme=scheme)
        else:
            page = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2,
                                    is_mobile=True, has_touch=True, color_scheme=scheme)
        for path in args or default_pages():
            page.goto(f"{BASE}{path}")
            page.wait_for_timeout(500)
            name = path.strip("/").replace("/", "-") or "home"
            shot = SHOTS / f"{device}-{name}-{scheme}.png"
            screens = next((int(a.split("=")[1]) for a in sys.argv if a.startswith("--screens=")), 0)
            if screens:  # only the first N screen heights: what you see without scrolling far
                h = min(page.evaluate("document.documentElement.scrollHeight"), screens * page.viewport_size["height"])
                page.screenshot(path=str(shot), full_page=True, clip={"x": 0, "y": 0, "width": page.viewport_size["width"], "height": h})
            else:
                page.screenshot(path=str(shot), full_page=True)
            r = page.evaluate(OVERFLOW_JS)
            ok = r["scroll"] <= r["vw"] and not r["offenders"]
            failed |= not ok
            print(f"{'ok  ' if ok else 'FAIL'} {path:22} {shot.relative_to(SHOTS.parent)}"
                  + ("" if ok else f"  scrollWidth={r['scroll']} offenders={r['offenders']}"))
        browser.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
