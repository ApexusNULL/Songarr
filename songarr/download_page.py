"""The app's download page: <app address>/download, the one web page the app API serves (switch it off
under Settings → App access; then /download answers an empty 404 like any other page).

It offers the newest published Android app (app_updates.py) and shows, with pictures, how to install it
and sign in. It names nothing about the server or the people on it, not even its address: whoever
shares the page gives the sign-in details separately. The app itself is no secret: it does nothing
without a pairing code or password from the admin.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .service import Service

PHONE = "arm64-v8a"  # every phone from the last several years
OLDER = "armeabi-v7a"  # 32-bit phones
NOTE = "M9 18.5A3.5 3.5 0 1 1 7 15.3V4l12-2v13.5a3.5 3.5 0 1 1-2-3.2V6.3L9 7.6z"  # Songarr's logo
# Screenshots of each step (Android 16, Songarr's own name and icon, nothing typed in)
PICTURES = Path(__file__).with_name("assets") / "download"
PICTURE_NAMES = ("1-downloads", "2-not-allowed", "3-allow", "4-install", "5-camera", "6-sign-in")
HEADERS = {
    "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; base-uri 'none'; "
                               "form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",  # for the family, not for search engines
    "Cache-Control": "no-cache",
}


def enabled(svc: Service) -> bool:
    return bool(svc.db.setting("download_page"))


def picture(name: str) -> Path | None:
    """One of the step pictures, by name (nothing else is served from the folder)."""
    path = PICTURES / f"{name}.webp"
    return path if name in PICTURE_NAMES and path.is_file() else None


def apk_name(svc: Service, abi: str) -> str:
    latest = svc.app_updates.latest(abi) or {}
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", svc.branding.name()).strip("-.") or "Songarr"
    return f"{name}-{latest.get('version') or 'app'}.apk"


def _size(n: int | None) -> str:
    return f"{(n or 0) / 1_000_000:.0f} MB"


def _shot(name: str, alt: str) -> str:
    return f'<img class="shot" src="/download/img/{name}.webp" alt="{html.escape(alt)}" loading="lazy">' if picture(name) else ""


def render(svc: Service) -> bytes:
    e = html.escape
    name = svc.branding.name()
    info = svc.branding.info()
    phone, older = svc.app_updates.latest(PHONE), svc.app_updates.latest(OLDER)
    icon = (f'<img class="icon" src="/download/icon.png?v={e(info["icon"])}" alt="">' if info["icon"] else
            f'<div class="icon mark"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="#fff" d="{NOTE}"/></svg></div>')
    if phone or older:
        main = phone or older
        get = (f'<a class="get" href="/download/{e(apk_name(svc, main["abi"]))}?abi={main["abi"]}">Download for Android</a>'
               f'<div class="meta">Version {e(main["version"])} · {_size(main.get("size"))}</div>')
        if phone and older:
            get += (f'<div class="meta small">Phone older than about 2017? <a href="/download/{e(apk_name(svc, OLDER))}?abi={OLDER}">'
                    f'Get the 32-bit version</a> ({_size(older.get("size"))}).</div>')
    else:
        get = '<p class="meta">The app isn\'t ready to download yet. Check back soon.</p>'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Get {e(name)} for Android</title>
{f'<link rel="icon" href="/download/icon.png?v={e(info["icon"])}">' if info["icon"] else ""}
<style>
:root {{ color-scheme: dark; --text: #f4f1ff; --dim: #bdb6da; --glass: rgba(255,255,255,.06); --line: rgba(255,255,255,.12); --pink: #ec4899; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; min-height: 100vh; font: 16px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; color: var(--text);
  background: radial-gradient(60rem 40rem at 15% -10%, rgba(139,92,246,.35), transparent 60%),
              radial-gradient(50rem 35rem at 110% 20%, rgba(236,72,153,.22), transparent 60%),
              radial-gradient(40rem 30rem at 40% 120%, rgba(245,158,11,.16), transparent 60%), #0b0817; background-attachment: fixed; }}
main {{ max-width: 36rem; margin: 0 auto; padding: 40px 16px 48px; }}
.card {{ background: var(--glass); border: 1px solid var(--line); border-radius: 22px; padding: 28px 22px; }}
.hero {{ text-align: center; }}
.icon {{ width: 96px; height: 96px; border-radius: 24px; display: block; margin: 0 auto 16px; box-shadow: 0 10px 40px rgba(139,92,246,.35); }}
.mark {{ display: grid; place-items: center; background: linear-gradient(135deg, #8b5cf6, #ec4899 60%, #f59e0b); }}
.mark svg {{ width: 54px; height: 54px; }}
h1 {{ font-size: 1.75rem; margin: 0 0 4px; letter-spacing: -.01em; }}
.lead {{ color: var(--dim); margin: 0 0 22px; }}
.get {{ display: inline-block; padding: 14px 28px; border-radius: 999px; font-weight: 650; color: #fff; text-decoration: none;
  background: linear-gradient(90deg, #8b5cf6, #ec4899 55%, #f59e0b); box-shadow: 0 8px 28px rgba(236,72,153,.35); }}
.meta {{ color: var(--dim); font-size: .9rem; margin-top: 10px; }}
.small {{ font-size: .82rem; }}
a {{ color: #d8c8ff; }}
h2 {{ font-size: 1.25rem; margin: 36px 0 6px; }}
.intro {{ color: var(--dim); margin: 0 0 18px; }}
.step {{ display: grid; grid-template-columns: 34px 1fr; gap: 0 12px; margin: 0 0 26px; }}
.num {{ width: 34px; height: 34px; border-radius: 50%; display: grid; place-items: center; font-weight: 700; font-size: .95rem;
  background: linear-gradient(135deg, #8b5cf6, #ec4899); }}
.step h3 {{ margin: 4px 0 4px; font-size: 1.05rem; }}
.step p {{ margin: 0 0 10px; color: var(--dim); }}
.step p b {{ color: var(--text); }}
.shot {{ display: block; width: 100%; max-width: 330px; height: auto; border-radius: 16px; border: 1px solid var(--line); margin: 6px 0 12px; }}
.tip {{ border-left: 3px solid var(--pink); background: rgba(236,72,153,.08); border-radius: 0 12px 12px 0; padding: 10px 14px; margin: 4px 0 12px; color: var(--dim); }}
.tip b {{ color: var(--text); }}
.mock {{ max-width: 330px; background: #f6f6f8; color: #111; border-radius: 16px; padding: 16px 18px 18px; margin: 6px 0 6px; font-family: system-ui, sans-serif; }}
.mock .crumbs {{ font-size: .78rem; color: #555; }}
.mock .mtitle {{ font-size: 1.6rem; font-weight: 600; margin: 6px 0 14px; }}
.mock .mrow {{ display: flex; align-items: center; justify-content: space-between; background: #fff; border-radius: 14px; padding: 12px 14px; font-weight: 600; }}
.mock .tog {{ width: 46px; height: 26px; border-radius: 13px; background: #3e7bfa; position: relative; outline: 3px solid var(--pink); outline-offset: 4px; }}
.mock .tog::after {{ content: ""; position: absolute; right: 3px; top: 3px; width: 20px; height: 20px; border-radius: 50%; background: #fff; }}
.mock .mdesc {{ font-size: .8rem; color: #555; margin: 10px 2px 0; }}
.caption {{ font-size: .78rem; color: var(--dim); margin: 0 0 12px; }}
footer {{ text-align: center; color: var(--dim); font-size: .85rem; margin-top: 30px; }}
</style></head>
<body><main>
<div class="card hero">
  {icon}
  <h1>{e(name)}</h1>
  <p class="lead">Your family's music, on your phone.</p>
  {get}
</div>

<h2>Installing it</h2>
<p class="intro">{e(name)} doesn't come from the Play Store, so Android asks a few extra questions the first time. It takes about a minute.</p>

<div class="step"><div class="num">1</div><div>
  <h3>Open the download</h3>
  <p>Tap <b>Download for Android</b> above. When it's done, tap the file in the notification or in your browser's
    <b>Downloads</b> (it's also in the <b>Files</b> app, under Downloads).</p>
  {_shot("1-downloads", "The downloaded app in Chrome's Downloads list")}
  <div class="tip">If the browser warns that the file might be harmful, tap <b>Download anyway</b>. Browsers say that about every app from outside the Play Store.</div>
</div></div>

<div class="step"><div class="num">2</div><div>
  <h3>Allow installing apps from there</h3>
  <p>Android stops at first (it may name your browser, Files or Download Manager). Tap <b>Settings</b>…</p>
  {_shot("2-not-allowed", "Android: your phone currently isn't allowed to install unknown apps from this source")}
  <p>…turn on <b>Allow from this source</b>, then go back. (This is only for apps you open from there; you can turn it off again later.)</p>
  {_shot("3-allow", "Install unknown apps: Allow from this source")}
</div></div>

<div class="step"><div class="num">3</div><div>
  <h3>Samsung phones: Auto Blocker</h3>
  <p>On most Samsung Galaxy phones <b>Auto Blocker</b> is on, and it blocks the install with a message instead.
    Open <b>Settings → Security and privacy → Auto Blocker</b> and turn it <b>off</b> (you'll confirm with your PIN or fingerprint), then open the download again.</p>
  <div class="mock" role="img" aria-label="Samsung Settings, Auto Blocker, with the switch to turn off">
    <div class="crumbs">Settings › Security and privacy</div>
    <div class="mtitle">Auto Blocker</div>
    <div class="mrow"><span>On</span><span class="tog"></span></div>
    <div class="mdesc">Blocks apps from unauthorised sources and other threats.</div>
  </div>
  <p class="caption">What it looks like on a Samsung phone (a drawing): turn the switch off.</p>
  <p>You can turn Auto Blocker back on once {e(name)} is installed, but it may stop the app's automatic updates; if an update doesn't install, turn it off again for a moment.</p>
</div></div>

<div class="step"><div class="num">4</div><div>
  <h3>Install</h3>
  <p>Tap <b>Install</b>, then <b>Open</b>.</p>
  {_shot("4-install", "Do you want to install this app? Install")}
  <div class="tip">If <b>Google Play Protect</b> warns that it doesn't recognize the app, tap <b>More details</b> and then <b>Install anyway</b> (or <b>Install without scanning</b>). It says that about apps that aren't from the Play Store.</div>
</div></div>

<div class="step"><div class="num">5</div><div>
  <h3>Allow the camera</h3>
  <p>{e(name)} asks for the camera so it can scan your sign-in code. Tap <b>While using the app</b>.</p>
  {_shot("5-camera", "Allow the app to take pictures: While using the app")}
</div></div>

<div class="step"><div class="num">6</div><div>
  <h3>Sign in</h3>
  <p>Scan the sign-in code you were given, or tap <b>Sign in with a password</b> and type the server address, name and
    password you were given. (Ask the person who sent you this page for them.)</p>
  {_shot("6-sign-in", "Sign in: server address, sign-in name and password")}
</div></div>

<footer>That's it. Updates install themselves from now on.</footer>
</main></body></html>
""".encode()
