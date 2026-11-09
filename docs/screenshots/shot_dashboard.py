import sys
from playwright.sync_api import sync_playwright
out=sys.argv[1]; scale=int(sys.argv[2]); focus=sys.argv[3] if len(sys.argv)>3 else ''
with sync_playwright() as p:
    b=p.chromium.launch(executable_path='/usr/bin/google-chrome',args=['--no-sandbox'])
    pg=b.new_page(viewport={'width':1600,'height':1000},device_scale_factor=scale)
    pg.goto('http://127.0.0.1:9195/kestra-oncall'); pg.wait_for_timeout(6000)
    try: pg.get_by_text('No thanks', exact=True).click(); pg.wait_for_timeout(500)
    except Exception: pass
    if focus:
        pg.evaluate("""(id)=>{const c=[...document.querySelectorAll('.ko-card')].find(e=>e.innerText.includes(id)); c.scrollIntoView({block:'start'}); document.scrollingElement.scrollBy(0,0); let n=c.parentElement; while(n){ if(n.scrollHeight>n.clientHeight+5 && getComputedStyle(n).overflowY!='visible'){ n.scrollTop+=58; break;} n=n.parentElement } }""", focus)
        pg.wait_for_timeout(500)
    pg.screenshot(path=out)
    b.close()
