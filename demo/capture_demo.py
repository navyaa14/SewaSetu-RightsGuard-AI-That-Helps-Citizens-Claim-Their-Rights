from playwright.sync_api import sync_playwright
OUT="/home/claude/video/build/img/"
with sync_playwright() as p:
    b=p.chromium.launch(); pg=b.new_page(viewport={"width":1440,"height":900})
    pg.goto("http://localhost:8000/demo"); pg.wait_for_timeout(1500)
    pg.screenshot(path=OUT+"demo_hero.png")
    pg.click("text=Kamala Das, by her grandson on the bus"); pg.click(".sp button[data-s='4']")
    pg.wait_for_timeout(5200); pg.click("#play")   # pause mid-session
    pg.evaluate("window.scrollTo({top:document.querySelector('#watch').offsetTop+70,behavior:'instant'})"); pg.wait_for_timeout(300)
    pg.screenshot(path=OUT+"demo_player.png")
    pg.click("[data-replay='rina-duplicate']"); pg.click(".sp button[data-s='4']"); pg.wait_for_timeout(9000)
    pg.evaluate("window.scrollTo({top:document.querySelector('#watch').offsetTop+70,behavior:'instant'})"); pg.wait_for_timeout(300)
    pg.screenshot(path=OUT+"demo_officer.png")
    for sec,name in (("#live","demo_live"),("#tools","demo_tools"),("#guardrails","demo_guards")):
        pg.evaluate(f"window.scrollTo({{top:document.querySelector('{sec}').offsetTop-10,behavior:'instant'}})"); pg.wait_for_timeout(400)
        pg.screenshot(path=OUT+name+".png")
    b.close()
print("ok")
