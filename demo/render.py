import sys, subprocess
from playwright.sync_api import sync_playwright
FPS = 30
start, end, out = float(sys.argv[1]), float(sys.argv[2]), sys.argv[3]
ff = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-y", "-f", "image2pipe", "-framerate", str(FPS), "-c:v", "mjpeg",
                       "-i", "-", "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p",
                       "-r", str(FPS), out], stdin=subprocess.PIPE)
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1920, "height": 1080})
    pg.goto("file:///home/claude/video/build/video.html")
    pg.wait_for_function("window.__ready===true")
    n0, n1 = round(start * FPS), round(end * FPS)
    for i in range(n0, n1):
        pg.evaluate("seek(%f)" % (i / FPS))
        ff.stdin.write(pg.screenshot(type="jpeg", quality=93))
        if i % 300 == 0:
            print(out, i, flush=True)
    b.close()
ff.stdin.close(); ff.wait()
print("done", out)
