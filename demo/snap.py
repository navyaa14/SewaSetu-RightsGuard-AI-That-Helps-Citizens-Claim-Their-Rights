import sys
from playwright.sync_api import sync_playwright
times=[float(x) for x in sys.argv[1:]]
with sync_playwright() as p:
    b=p.chromium.launch(); pg=b.new_page(viewport={"width":1920,"height":1080})
    pg.goto("file:///home/claude/video/build/video.html"); pg.wait_for_function("window.__ready===true")
    for t in times:
        pg.evaluate(f"seek({t})"); pg.screenshot(path=f"check/t{t:06.1f}.jpg", type="jpeg", quality=80)
    b.close()
