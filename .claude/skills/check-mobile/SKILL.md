---
name: check-mobile
description: Check the Training Coach UI at iPhone size (390px) for overflow and layout problems. Use after any change to templates or CSS, before calling UI work done.
---

# Check the UI at iPhone size

The user mostly opens the panel on an iPhone. A page that looks fine on desktop can be broken on a phone.

1. **Server.** Check whether something answers on `http://localhost:8099/healthz`. If not, start it with demo data:
   ```bash
   [ -f /tmp/tc-demo/training.db ] || TC_DATA_DIR=/tmp/tc-demo .venv/bin/python tools/seed_demo.py
   (cd training_coach && TC_DATA_DIR=/tmp/tc-demo ../.venv/bin/python -m uvicorn app.main:app --port 8099 > /tmp/tc-demo/server.log 2>&1 &)
   ```
   If code changed since the server started, restart it (`pkill -f "uvicorn app.main:app"`, then start it again).
2. **Run** `.venv/bin/python tools/mobile_check.py` (all pages, dark mode). Add `--light` for light mode, or pass
   specific paths such as `/plan`. Exit code 1 means horizontal overflow; the output names the offending elements.
3. **Look** at the screenshots for the pages you changed (`.shots/iphone-*.png`, full-page). The script only catches overflow.
   Read the images and check for:
   - squeezed columns
   - overlapping buttons
   - headings wrapping into several lines
   - numbers breaking across lines
   - tap targets that are too small
4. Fix and re-run until clean. Then stop the server if you started it.

Don't use `chrome --headless --window-size=390,...` for this. Desktop Chrome lays out at ~500 px minimum and crops,
so it both hides real problems and invents fake ones.

Known trap: inline `style="grid-template-columns: …"` overrides the responsive media queries in `app.css`. Put layouts in CSS classes.
