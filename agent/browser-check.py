#!/usr/bin/env python3
"""browser-check: load a web page in a REAL browser (headless Chromium), try to use it, and report in a few short lines.

    browser-check index.html                          does it load, run without errors, and draw something?
    browser-check index.html --keys ArrowLeft,Space   ... and does pressing those keys change what is on screen?
    browser-check index.html --click "#start" --expect-text "Score"

Why it exists: a check that fakes the browser (jsdom, a stub document) can pass while the real page is broken: a script that runs
before its canvas exists, an event listener on the wrong element, a canvas that never draws, a missing file. This opens the page
the way you will, and saves screenshots (artifacts/screenshot.png, artifacts/after-input.png) for a person to look at.

Output: at most about 15 lines, one per check, ending with `OK` or `FAIL: ...`. Exit code 0 only on OK. Detail: artifacts/browser-check.json.

Options
  PAGE                 an .html file (served from its folder on a local port, so relative files and modules work) or an http(s) URL
  --keys A,B,C         press these keys in order (names as Playwright knows them: ArrowUp, Space, Enter, a, ...) and require the screen to change
  --click SELECTOR     click this element first (a start button)
  --expect-text TEXT   this text must be visible on the page (repeatable)
  --expect-selector S  this element must exist (repeatable)
  --probe EXPR         a JavaScript expression (for example window.game.score); shown before and after the input, and with
                       --probe-change it must differ
  --expect-motion      the screen must change by itself within 2 seconds (an animation or game loop)
  --wait SECONDS       settle time after loading (default 1.0)
  --out DIR            where screenshots and the JSON report go (default ./artifacts)
  --size WxH           window size (default 1000x700)
"""
import argparse, hashlib, http.server, io, json, os, socketserver, sys, threading, warnings
warnings.simplefilter("ignore")
from urllib.parse import urlparse


def serve(root):
    class Q(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k): super().__init__(*a, directory=root, **k)
        def log_message(self, *a): pass
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Q)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def distinct_colours(png_bytes):
    from PIL import Image
    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    im.thumbnail((200, 200))
    return len(set(im.getdata()))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("page")
    ap.add_argument("--keys", default="")
    ap.add_argument("--click", default="")
    ap.add_argument("--expect-text", action="append", default=[])
    ap.add_argument("--expect-selector", action="append", default=[])
    ap.add_argument("--probe", default="")
    ap.add_argument("--probe-change", action="store_true")
    ap.add_argument("--expect-motion", action="store_true")
    ap.add_argument("--wait", type=float, default=1.0)
    ap.add_argument("--out", default="artifacts")
    ap.add_argument("--size", default="1000x700")
    a = ap.parse_args()
    from playwright.sync_api import sync_playwright

    os.makedirs(a.out, exist_ok=True)
    w, h = (int(x) for x in a.size.lower().split("x"))
    lines, problems, warnings = [], [], []
    report = {"page": a.page, "checks": {}}

    def say(ok, name, detail=""):
        lines.append(f"{'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail else ''}")
        report["checks"][name] = {"ok": ok, "detail": detail}
        if not ok:
            problems.append(f"{name}{' (' + detail + ')' if detail else ''}")

    srv = None
    if a.page.startswith(("http://", "https://")):
        url = a.page
    else:
        path = os.path.abspath(a.page)
        if not os.path.isfile(path):
            print(f"FAIL: no such file: {a.page}")
            return 1
        srv = serve(os.path.dirname(path))
        url = f"http://127.0.0.1:{srv.server_address[1]}/{os.path.basename(path)}"
    local = urlparse(url).netloc

    errors, external = [], set()
    failed_by_url = {}                                   # URL -> why (the same file can be reported twice by the browser)
    with sync_playwright() as p:
        br = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
        pg = br.new_page(viewport={"width": w, "height": h})
        pg.on("pageerror", lambda e: errors.append(f"{str(e)[:140]}"))
        pg.on("console", lambda m: errors.append(f"console.error: {m.text[:120]}") if m.type == "error" and "Failed to load resource" not in m.text else None)
        pg.on("requestfailed", lambda r: (external.add(r.url[:80]) if urlparse(r.url).netloc not in ("", local) else failed_by_url.setdefault(r.url[:80], "failed")))
        pg.on("response", lambda r: failed_by_url.__setitem__(r.url[:80], str(r.status)) if r.status >= 400 and urlparse(r.url).netloc == local else None)
        try:
            resp = pg.goto(url, wait_until="load", timeout=20000)
            say(resp is not None and resp.status < 400, "loads", f"HTTP {resp.status if resp else '?'}")
        except Exception as e:  # noqa: BLE001
            say(False, "loads", str(e).split("\n")[0][:100])
            print("\n".join(lines) + "\nFAIL: " + "; ".join(problems))
            return 1
        pg.wait_for_timeout(int(a.wait * 1000))
        shot = os.path.join(a.out, "screenshot.png")
        s0 = pg.screenshot(path=shot)
        say(not errors, "no script errors", "; ".join(dict.fromkeys(errors))[:200] if errors else "")
        failed = [f"{u.split('/', 3)[-1]} ({why})" for u, why in failed_by_url.items()]
        say(not failed, "no missing files", ", ".join(failed)[:200] if failed else "")
        if external:
            warnings.append("needs the network: " + ", ".join(sorted(external))[:120])

        # does the page show anything at all?
        visible_text = pg.evaluate("document.body ? document.body.innerText.trim().length : 0")
        canvases = pg.evaluate("[...document.querySelectorAll('canvas')].map(c => { const r = c.getBoundingClientRect(); return [c.width, c.height, Math.round(r.width), Math.round(r.height), getComputedStyle(c).display, getComputedStyle(c).visibility]; })")
        colours = distinct_colours(s0)
        if canvases:
            tiny = [c for c in canvases if c[2] < 10 or c[3] < 10 or c[4] == "none" or c[5] == "hidden"]
            say(not tiny, "canvas is visible", f"canvas {canvases[0][0]}x{canvases[0][1]}, shown at {canvases[0][2]}x{canvases[0][3]}" if not tiny else f"canvas not visible (shown at {tiny[0][2]}x{tiny[0][3]}, display {tiny[0][4]})")
            try:
                el = pg.query_selector("canvas")
                cc = distinct_colours(el.screenshot()) if el else 0
            except Exception:  # noqa: BLE001
                cc = colours
            say(cc >= 3, "canvas has been drawn on", f"{cc} distinct colours")
        else:
            say(visible_text > 0 or colours >= 3, "page shows something", f"{visible_text} characters of text, {colours} colours")
        for t in a.expect_text:
            say(bool(pg.get_by_text(t, exact=False).first.is_visible()) if pg.get_by_text(t, exact=False).count() else False, f"text '{t}' visible")
        for sel in a.expect_selector:
            say(pg.query_selector(sel) is not None, f"element {sel} exists")

        def snap():
            return hashlib.sha1(pg.screenshot()).hexdigest()

        before = snap()
        if a.expect_motion or not (a.keys or a.click):
            pg.wait_for_timeout(1500)
            moved = snap() != before
            if a.expect_motion:
                say(moved, "moves by itself", "" if moved else "the screen did not change in 2 s")
            else:
                report["checks"]["moves by itself"] = {"ok": None, "detail": "yes" if moved else "no (fine for a page that is not animated)"}
        probe0 = pg.evaluate(a.probe) if a.probe else None
        if a.click:
            try:
                pg.click(a.click, timeout=3000)
                pg.wait_for_timeout(400)
                say(True, f"clicked {a.click}")
            except Exception as e:  # noqa: BLE001
                say(False, f"click {a.click}", str(e).split("\n")[0][:90])
        if a.keys:
            pg.mouse.click(w // 2, h // 2)                    # a person clicks the page before pressing keys; focus matters
            mid = snap()
            for k in a.keys.split(","):
                pg.keyboard.press(k.strip())
                pg.wait_for_timeout(250)
            pg.wait_for_timeout(500)
            after = snap()
            say(after != mid, f"keys change the screen ({a.keys})", "" if after != mid else "nothing changed: are the key handlers attached to the right element?")
        if a.keys or a.click:
            pg.screenshot(path=os.path.join(a.out, "after-input.png"))
        if a.probe:
            probe1 = pg.evaluate(a.probe)
            lines.append(f"info probe {a.probe}: {probe0!r} -> {probe1!r}")
            if a.probe_change:
                say(probe0 != probe1, "probe changed", f"{probe0!r} -> {probe1!r}")
        errors_after = [e for e in errors if e not in "".join(problems)]
        if errors_after:
            say(False, "no script errors while playing", "; ".join(dict.fromkeys(errors_after))[:200])
        br.close()
    for wmsg in warnings:
        lines.append("warn " + wmsg)
    report["problems"], report["warnings"] = problems, warnings
    json.dump(report, open(os.path.join(a.out, "browser-check.json"), "w"), indent=1)
    lines.append(f"screenshots: {a.out}/screenshot.png" + (f", {a.out}/after-input.png" if (a.keys or a.click) else ""))
    print("\n".join(lines))
    print("OK" if not problems else "FAIL: " + "; ".join(problems)[:300])
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
