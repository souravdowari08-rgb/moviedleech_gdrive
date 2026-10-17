#!/usr/bin/env python3
"""
Open a link, click the "Download File" button (#ddl) and return the Google Drive link.

Handles both outcomes after the click:
  1. a token page appears and the gdrive link is inside it
  2. the site directly gives you (redirects to / shows) a gdrive link

The button only exists for logged-in users, so the session is loaded from
state.json (Playwright storage_state: cookies + localStorage) and
meta.json (browser fingerprint info such as user agent, viewport, headers).

Setup:
    pip install playwright
    playwright install chromium

Usage:
    python get_gdrive_link.py "https://example.com/file/abc123"
    python get_gdrive_link.py URL --state state.json --meta meta.json --timeout 20
    python get_gdrive_link.py URL --slow 500        # slow down each action by 500ms
    python get_gdrive_link.py URL --headed          # show the browser (default is headless = faster)
    python get_gdrive_link.py URL1 URL2 URL3        # many links, one browser, parallel tabs
    python get_gdrive_link.py URL --block-ads       # also block ad networks (faster, fewer popups)
    python get_gdrive_link.py URL --load-all        # disable all request blocking (debugging)
    python get_gdrive_link.py URL --keep-popups     # don't auto-close extra tabs (if the token page opens cross-site)
    python get_gdrive_link.py URL --retries 8 --retry-delay 3   # quota/limit errors: wait, press again (no reload)
    python get_gdrive_link.py URL --reload-on-retry              # reload the page before pressing again
    python get_gdrive_link.py URL --methods call                # only call the button's handler, never click
    python get_gdrive_link.py URL --discover                    # record what the button sends -> discover.json
    python get_gdrive_link.py URL --error-regex "server busy|try later"   # extra error text to react to

From your own code:
    from getlink import get_gdrive_link_sync
    link = get_gdrive_link_sync(url)

    # many links, one browser launch:
    from getlink import get_gdrive_links_sync
    results = get_gdrive_links_sync([url1, url2, url3], concurrency=3)
"""
import argparse
import asyncio
import contextlib
import html
import json
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlparse

from playwright.async_api import async_playwright

BUTTON_SELECTOR = "#ddl"

GDRIVE_RE = re.compile(
    r"https?://(?:drive\.google\.com|docs\.google\.com|drive\.usercontent\.google\.com)"
    r"/[^\s\"'<>\\)\]]+",
    re.I,
)

# Which drive URLs are real file/folder links (higher = better)
GOOD_PATTERNS = [
    (re.compile(r"/file/d/"), 5),
    (re.compile(r"/uc\?"), 5),
    (re.compile(r"/download\?"), 5),
    (re.compile(r"/open\?id="), 4),
    (re.compile(r"/drive/folders/|/folders/"), 4),
    (re.compile(r"/document/d/|/spreadsheets/d/|/presentation/d/"), 3),
]

# --- request blocking -------------------------------------------------------
# Matched by URL inside the browser layer (no Python round-trip for requests that
# don't match), so allowed requests pay zero overhead.
BLOCKED_EXT_RE = re.compile(
    r"\.(?:png|jpe?g|gif|webp|avif|svg|ico|bmp|woff2?|ttf|otf|eot|mp4|webm|mp3|ogg|m4a|wav)(?:[?#].*)?$",
    re.I,
)


def _host_re(hosts):
    return re.compile(
        r"^https?://(?:[^/]*\.)?(?:" + "|".join(re.escape(h) for h in hosts) + r")(?:[:/?#]|$)", re.I
    )


FONT_HOST_RE = _host_re([
    "fonts.googleapis.com", "fonts.gstatic.com", "use.typekit.net",
    "use.fontawesome.com", "kit.fontawesome.com",
])
TRACKER_HOST_RE = _host_re([
    "google-analytics.com", "googletagmanager.com", "analytics.google.com",
    "connect.facebook.net", "hotjar.com", "clarity.ms", "histats.com",
    "mc.yandex.ru", "cloudflareinsights.com", "newrelic.com",
])
AD_HOST_RE = _host_re([  # opt-in (--block-ads): the site might rely on its ad scripts
    "doubleclick.net", "googlesyndication.com", "googleadservices.com",
    "adservice.google.com", "popads.net", "popcash.net", "propellerads.com",
    "adsterra.com", "exoclick.com", "juicyads.com", "adnxs.com", "taboola.com",
    "outbrain.com", "monetag.com", "onclickads.com", "clickadu.com", "hilltopads.net",
])
GDRIVE_HOST_RE = re.compile(
    r"^https?://(?:drive\.google\.com|docs\.google\.com|drive\.usercontent\.google\.com)/", re.I
)

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-sync",
    "--disable-default-apps",
    "--disable-features=Translate,OptimizationHints,MediaRouter",
    "--no-first-run",
    "--mute-audio",
]

TOKEN_URL_PATTERN = r"[?&]token="   # the site's token page, e.g. https://site/file/ID?token=...
TOKEN_WAIT = 25  # seconds the token page gets to produce the link (countdowns etc.)

POLL_INTERVAL = 0.1  # seconds; a network hit wakes the poll loop immediately

RETRY_MIN_GAP = 0.3    # seconds between an error and the next press (0 delay = press as soon as the button is enabled)
RETRY_MAX_WAIT = 30    # longest we wait for a disabled / counting-down button before pressing anyway
SITE_REPLY_GRACE = 0.7  # after the site's reply, an unchanged old error this long on screen = the press failed again
ERROR_SETTLE = 2.0  # seconds an old error may stay on screen after a re-press before it counts again

MAX_REPRESS = 4  # presses in a row that an ad popup may swallow before giving up on a click style

# Ways to trigger the button, tried in order within one round:
#   call - run the button's own click handler directly (no click event, so ad scripts never see it)
#   js   - element.click() on the button
#   real - a real mouse click
METHODS = ["call", "js", "real"]
METHOD_LABELS = {
    "call": "call the button's handler directly",
    "js": "direct element click",
    "real": "mouse click",
}

# Error texts that make the script wait, reload the page and press the button again.
# Only text that appears AFTER the click counts (text already on the page is ignored).
DEFAULT_ERROR_PATTERN = (
    r"quota[^.\n]{0,60}(?:exceed|reach|limit)"
    r"|(?:exceed|reach)\w*[^.\n]{0,40}quota"
    r"|(?:download|daily|rate|request|usage)s? limit[^.\n]{0,30}"
    r"|limit (?:has been )?(?:reached|exceeded)"
    r"|too many (?:requests|downloads|attempts|users)"
    r"|rate.?limit"
    r"|try again (?:later|after|in)"
    r"|something went wrong"
    r"|an error (?:has )?occurred"
)


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


class NotLoggedInError(RuntimeError):
    pass


class LinkNotFoundError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _score(url: str) -> int:
    for pat, score in GOOD_PATTERNS:
        if pat.search(url):
            return score
    return 0


def find_gdrive_links(text: str, loose: bool = False) -> list[str]:
    """Return gdrive links found in text, best match first. loose=True also keeps drive links that match no known pattern."""
    if not text:
        return []
    text = html.unescape(text).replace("\\/", "/").replace("\\u0026", "&")
    if "%" in text:  # link passed URL-encoded, e.g. ?url=https%3A%2F%2Fdrive.google.com%2F...
        text += "\n" + unquote(text)
    links, seen = [], set()
    for m in GDRIVE_RE.finditer(text):
        link = m.group(0).rstrip(".,;")
        if link not in seen and (loose or _score(link) > 0):
            seen.add(link)
            links.append(link)
    links.sort(key=_score, reverse=True)
    return links


def _load_json(path):
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _normalize_cookies(cookies: list[dict]) -> list[dict]:
    """Make browser-extension cookie exports Playwright-compatible."""
    same_site_map = {
        "no_restriction": "None", "none": "None",
        "lax": "Lax", "strict": "Strict", "unspecified": "Lax",
    }
    out = []
    for c in cookies:
        c = dict(c)
        if "sameSite" in c:
            c["sameSite"] = same_site_map.get(str(c["sameSite"]).lower(), "Lax")
        if "expirationDate" in c and "expires" not in c:
            c["expires"] = c.pop("expirationDate")
        for k in ("hostOnly", "session", "storeId", "id"):
            c.pop(k, None)
        out.append(c)
    return out


def build_context_options(state, meta) -> dict:
    """state.json -> storage_state, meta.json -> fingerprint options."""
    opts = {}

    if isinstance(state, list):  # plain cookie list export
        state = {"cookies": _normalize_cookies(state), "origins": []}
    elif isinstance(state, dict):
        state = dict(state)
        state["cookies"] = _normalize_cookies(state.get("cookies", []))
        state.setdefault("origins", [])
    if state:
        opts["storage_state"] = state

    meta = meta or {}
    ua = meta.get("user_agent") or meta.get("userAgent")
    if ua:
        opts["user_agent"] = ua
    if meta.get("viewport"):
        opts["viewport"] = meta["viewport"]
    if meta.get("locale"):
        opts["locale"] = meta["locale"]
    tz = meta.get("timezone_id") or meta.get("timezone")
    if tz:
        opts["timezone_id"] = tz
    headers = meta.get("extra_http_headers") or meta.get("headers")
    if isinstance(headers, dict) and headers:
        opts["extra_http_headers"] = headers
    if meta.get("geolocation"):
        opts["geolocation"] = meta["geolocation"]
    if meta.get("proxy"):
        opts["proxy"] = meta["proxy"]
    return opts


# One round-trip per page. The regex pre-filter runs INSIDE the page, so only small
# snippets around "drive.google.com" etc. come back instead of the whole HTML.
SCAN_JS = """
(wantText) => {
  const raw = [
    document.documentElement.outerHTML,
    ...[...document.querySelectorAll('input,textarea')].map(e => e.value)
  ].join('\\n');
  const re = /drive\\.google\\.com|docs\\.google\\.com|drive\\.usercontent\\.google\\.com/gi;
  const snippets = [];
  let m;
  while ((m = re.exec(raw)) && snippets.length < 50)
    snippets.push(raw.slice(Math.max(0, m.index - 40), m.index + 700));
  return {
    links: snippets.join('\\n'),
    text: wantText && document.body ? document.body.innerText : '',
  };
}
"""


async def _probe_page(page, want_text: bool, loose: bool = False):
    """(gdrive links, visible text) of one page, including all of its iframes."""
    links = find_gdrive_links(page.url, loose)
    try:
        frames = list(page.frames)
    except Exception:
        frames = []

    async def one(fr, wt):
        try:
            res = await fr.evaluate(SCAN_JS, wt)
            return find_gdrive_links(fr.url, loose) + find_gdrive_links(res.get("links", ""), loose), res.get("text") or ""
        except Exception:
            return [], ""  # frame is navigating, try again on the next poll

    results = await asyncio.gather(*(one(fr, want_text and fr == page.main_frame) for fr in frames))
    text = ""
    for fl, ft in results:
        links += fl
        text += ft
    return links, text


async def _probe(context, origin_host: str, error_re, token_re=None):
    """Scan all tabs in parallel: gdrive links everywhere, error lines on the site's own tabs."""
    pages = [p for p in context.pages if not p.is_closed()]
    results = await asyncio.gather(
        *(
            _probe_page(
                p,
                _same_site(_host(p.url), origin_host),
                bool(token_re and token_re.search(p.url) and _same_site(_host(p.url), origin_host)),
            )
            for p in pages
        )
    )
    links, errors = [], set()
    for page_links, text in results:
        links += page_links
        for line in text.splitlines():
            line = line.strip()
            if line and error_re.search(line):
                errors.add(line[:120])
    return links, errors


# --------------------------------------------------------------------------
# overlay / ad handling
# --------------------------------------------------------------------------
# Finds whatever sits on top of the button (transparent ad overlays, iframes,
# link layers), makes it click-through, and outlines the real button in red.
# (Not hidden: error messages must stay visible so they can still be detected.)
NEUTRALIZE_JS = """
(sel) => {
  const btn = document.querySelector(sel);
  if (!btn) return { ok: false, removed: [] };
  btn.scrollIntoView({ block: 'center', inline: 'center' });
  const describe = (el) => {
    let d = el.tagName.toLowerCase();
    if (el.id) d += '#' + el.id;
    if (typeof el.className === 'string' && el.className.trim())
      d += '.' + el.className.trim().split(/\\s+/).join('.');
    const href = el.href || (el.closest && el.closest('a') && el.closest('a').href);
    if (href) d += ' -> ' + href;
    return d.slice(0, 140);
  };
  const removed = [];
  for (let i = 0; i < 20; i++) {
    const r = btn.getBoundingClientRect();
    const pts = [
      [r.left + r.width / 2, r.top + r.height / 2],
      [r.left + 4, r.top + 4],
      [r.right - 4, r.bottom - 4],
    ];
    let blocker = null;
    for (const [x, y] of pts) {
      const el = document.elementFromPoint(x, y);
      if (el && el !== btn && !btn.contains(el) && !el.contains(btn)) { blocker = el; break; }
    }
    if (!blocker) break;
    removed.push(describe(blocker));
    blocker.style.setProperty('pointer-events', 'none', 'important');
  }
  btn.style.outline = '3px solid red';
  return {
    ok: true, removed,
    text: (btn.innerText || btn.value || '').trim().slice(0, 40),
    disabled: !!(btn.disabled || btn.classList.contains('disabled') || btn.getAttribute('aria-disabled') === 'true'),
  };
}
"""


# Runs the button's own click handler(s) without dispatching a click event.
# Works for jQuery handlers (direct or delegated), onclick="..." and form submit buttons.
CALL_HANDLER_JS = """
(sel) => {
  const btn = document.querySelector(sel);
  if (!btn) return 'no-button';
  const $ = window.jQuery || window.$;
  let called = 0;
  const errors = [];
  if ($ && $._data && $.Event) {
    const chain = [];
    for (let n = btn; n; n = n.parentNode) chain.push(n);
    for (const n of chain) {
      const ev = $._data(n, 'events');
      if (!ev || !ev.click) continue;
      for (const h of ev.click.slice()) {
        try {
          let ctx = null;
          if (h.selector) ctx = btn.closest(h.selector);
          else if (n === btn) ctx = btn;
          if (!ctx) continue;
          h.handler.call(ctx, $.Event('click', { target: btn, currentTarget: ctx }));
          called++;
        } catch (e) { errors.push(String(e)); }
      }
    }
  }
  if (!called && typeof btn.onclick === 'function') {
    try { btn.onclick.call(btn, new MouseEvent('click')); called++; } catch (e) { errors.push(String(e)); }
  }
  if (!called && btn.form && btn.type === 'submit') {
    try { btn.form.requestSubmit ? btn.form.requestSubmit(btn) : btn.form.submit(); called++; }
    catch (e) { errors.push(String(e)); }
  }
  return (called ? 'called:' + called : 'none') + (errors.length ? ' errors:' + errors.join(' | ').slice(0, 200) : '');
}
"""


def _host(u: str) -> str:
    try:
        return (urlparse(u).hostname or "").lower()
    except Exception:
        return ""


def _same_site(a: str, b: str) -> bool:
    """Rough same-registrable-domain check (last two labels)."""
    if not a or not b:
        return False
    return a.split(".")[-2:] == b.split(".")[-2:]


async def _click_attempt(page, method: str) -> bool:
    """Make overlays click-through, then trigger the button. Returns False if nothing could be triggered.

    method "call": run the button's own handler directly - no click event at all.
    method "js":   element.click() on the button itself - no coordinates, so an
                   overlay on top of it can't receive the click.
    method "real": actual mouse click at the button's position (after the
                   overlays were made click-through).
    """
    # The page may be re-rendering / reloading after an error: the button can be briefly gone.
    info = None
    for _ in range(2):
        try:
            info = await page.evaluate(NEUTRALIZE_JS, BUTTON_SELECTOR)
        except Exception:
            info = None
        if info and info.get("ok"):
            break
        log("Button not on the page right now - waiting for it")
        try:
            await page.locator(BUTTON_SELECTOR).wait_for(state="visible", timeout=6_000)
        except Exception:
            return False
    if not (info and info.get("ok")):
        return False
    for d in info.get("removed", []):
        log(f"Made overlay click-through: {d}")
    log(f"Button state: text={info.get('text')!r} disabled={info.get('disabled')}")
    try:
        if method == "call":
            res = await page.evaluate(CALL_HANDLER_JS, BUTTON_SELECTOR)
            log(f"Direct handler call: {res}")
            return str(res).startswith("called")
        if method == "js":
            clicked = await page.evaluate(
                "sel => { const b = document.querySelector(sel); if (!b) return false; b.click(); return true; }",
                BUTTON_SELECTOR,
            )
            return bool(clicked)
        try:
            await page.locator(BUTTON_SELECTOR).click(timeout=5_000)
        except Exception as e:
            log(f"Mouse click failed: {str(e).splitlines()[0]}")
        return True
    except Exception as e:  # page navigated while we were pressing
        log(f"Press interrupted: {str(e).splitlines()[0]}")
        return False


async def _prune_popups(context, main_page, origin_host: str, first_seen: dict):
    """Close extra tabs that opened on another site (ad popups)."""
    for p in list(context.pages):
        if p is main_page or p.is_closed():
            continue
        u = p.url
        if u in ("", "about:blank"):
            continue  # still loading
        host = _host(u)
        if _same_site(host, origin_host) or host.endswith("google.com"):
            continue
        t = first_seen.setdefault(id(p), time.monotonic())
        if time.monotonic() - t >= 1.5:
            log(f"Closing popup/ad tab: {u[:110]}")
            try:
                await p.close()
            except Exception:
                pass


async def _button_state(page):
    """Text / disabled / class of the button - changes when the site starts working."""
    try:
        return await page.evaluate(
            """(sel) => { const b = document.querySelector(sel); if (!b) return 'missing';
                return [(b.innerText || b.value || '').trim(), b.disabled, b.className].join('|'); }""",
            BUTTON_SELECTOR,
        )
    except Exception:
        return None


async def _action_started(run) -> bool:
    """Did the press actually reach the site's own button handler?"""
    if run.site_activity:                       # ajax / navigation request to the site itself
        return True
    if run.page.url != run.click_url:           # main tab navigated
        return True
    state = await _button_state(run.page)
    if run.click_state is not None and state is not None and state != run.click_state:
        return True                             # e.g. "Download File" -> "Please wait..."
    for p in run.context.pages:                 # token page opened in a new tab
        if p is not run.page and not p.is_closed():
            u = p.url
            if u not in ("", "about:blank") and _same_site(_host(u), run.origin_host):
                return True
    return False


DESCRIBE_JS = """
() => {
  const t = (document.body ? document.body.innerText : '').replace(/\\s+/g, ' ').trim().slice(0, 160);
  const a = [...document.querySelectorAll('a[href],button')].slice(0, 8).map(e =>
    ((e.innerText || e.value || '').trim().slice(0, 25) + (e.href ? ' -> ' + e.href.slice(0, 70) : ''))
  ).join(' | ');
  return t + ' || ' + a;
}
"""


async def _token_watch(run) -> bool:
    """Is the site's token page open? Logs what it shows, optionally clicks a button on it."""
    page = next(
        (p for p in run.context.pages
         if not p.is_closed() and run.token_re.search(p.url) and _same_site(_host(p.url), run.origin_host)),
        None,
    )
    if page is None:
        return run.token_seen
    now = time.monotonic()
    if not run.token_seen:
        run.token_seen = True
        run.deadline = max(run.deadline, now + TOKEN_WAIT)
        log(f"Token page reached: {page.url[:140]}")
    if run.token_click and not run.token_clicked:
        try:
            loc = page.locator(run.token_click).first
            if await loc.is_visible():
                log(f"Clicking {run.token_click!r} on the token page")
                await loc.click(timeout=3_000)
                run.token_clicked = True
        except Exception:
            pass
    if now - run.token_last_log >= 4:
        run.token_last_log = now
        try:
            log("Token page shows: " + await page.evaluate(DESCRIBE_JS))
        except Exception:
            pass
    return True


async def _poll(run, seconds, count_persist=True):
    """Wait up to `seconds`. Returns ("link", url), ("error", text), ("swallowed", url) or None."""
    end = time.monotonic() + max(seconds, 0)
    last_urls = None
    while time.monotonic() < end:
        urls = [p.url for p in run.context.pages]
        if urls != last_urls:
            log("Tabs: " + " | ".join(u[:90] for u in urls))
            last_urls = urls
        if run.network_hit:
            link = sorted(run.network_hit, key=_score, reverse=True)[0]
            log(f"Direct redirect to gdrive: {link}")
            return ("link", link)
        if run.token_re is not None and await _token_watch(run):
            end = max(end, run.deadline)  # the token page may take a while - don't give up / re-press early
        links, current = await _probe(run.context, run.origin_host, run.error_re, run.token_re)
        for link in sorted(links, key=_score, reverse=True):
            if link not in run.baseline:
                log(f"Found gdrive link on page: {link}")
                return ("link", link)
        if run.error_hit:
            return ("error", run.error_hit[0])
        fresh = current - run.baseline_errors
        if fresh:
            return ("error", sorted(fresh)[0])
        if run.watch:
            # Pressing again without a reload: the old error text may still be on screen.
            # If it vanished and came back, or never went away, the press failed again.
            still = current & run.watch
            if not still:
                run.stale_cleared = True
            elif run.stale_cleared:
                return ("error", sorted(still)[0])  # it went away and came back
            elif count_persist and (
                time.monotonic() - run.click_time >= ERROR_SETTLE
                or (run.site_resp and run.site_resp[-1] > run.click_time
                    and time.monotonic() - run.site_resp[-1] >= SITE_REPLY_GRACE
                    and not await _action_started(run))
            ):
                log("Old error is still on screen after the press")
                return ("error", sorted(still)[0])
        # An ad popup opened right after the press? Some ad scripts use the click to open
        # their popup and swallow it, so the button's own handler never runs.
        if run.method == "call":
            pass  # no click event was sent, so a new tab is the site's own token page - never an ad
        elif run.ad_popup_at is None:
            for pg in run.context.pages:
                if pg is run.page or pg.is_closed():
                    continue
                u = pg.url
                host = _host(u)
                if u in ("", "about:blank") or host.endswith("google.com") or _same_site(host, run.origin_host):
                    continue
                run.ad_popup_at, run.ad_popup_url = time.monotonic(), u
                log(f"Popup opened after the press: {u[:100]}")
                break
        elif time.monotonic() - run.ad_popup_at >= 1.0 and not await _action_started(run):
            return ("swallowed", run.ad_popup_url or "")
        if run.close_popups and run.method != "call":
            await _prune_popups(run.context, run.page, run.origin_host, run.first_seen)
        # sleep, but wake instantly on a network event (redirect / dialog / 429)
        try:
            await asyncio.wait_for(run.wake.wait(), POLL_INTERVAL)
        except asyncio.TimeoutError:
            pass
        run.wake.clear()
    return None


async def _fresh_start(context, page, start_url: str) -> bool:
    """Close extra tabs, reload the original page and wait for the button."""
    for p in list(context.pages):
        if p is not page and not p.is_closed():
            try:
                await p.close()
            except Exception:
                pass
    try:
        await page.goto(start_url, wait_until="domcontentloaded", timeout=20_000)
        await page.locator(BUTTON_SELECTOR).wait_for(state="visible", timeout=10_000)
        return True
    except Exception as e:
        log(f"Could not get the button back: {str(e).splitlines()[0]}")
        return False


BUTTON_ENABLED_JS = """
(sel) => {
  const b = document.querySelector(sel);
  if (!b) return false;
  const r = b.getBoundingClientRect();
  const txt = (b.innerText || b.value || '').trim();
  const off = b.disabled || b.classList.contains('disabled') || b.getAttribute('aria-disabled') === 'true'
    || getComputedStyle(b).pointerEvents === 'none';
  const busy = /please wait|wait\\s*\\d|\\d+\\s*s\\b|loading|processing|generating/i.test(txt);
  return r.width > 0 && r.height > 0 && !off && !busy;
}
"""


async def _wait_button_enabled(page, max_wait: float = RETRY_MAX_WAIT) -> bool:
    """Return as soon as the button is enabled again (it may be disabled / counting down after an error)."""
    end = time.monotonic() + max_wait
    first = True
    while time.monotonic() < end:
        try:
            if await page.evaluate(BUTTON_ENABLED_JS, BUTTON_SELECTOR):
                return True
        except Exception:
            pass  # page is navigating
        if first:
            log("Waiting for the button to be enabled again")
            first = False
        await asyncio.sleep(0.1)
    log(f"Button still not enabled after {max_wait:g}s - pressing anyway")
    return False


async def _ready_to_press(context, page) -> bool:
    """Close extra tabs and check the button is still there, so we can press again without reloading."""
    for p in list(context.pages):
        if p is not page and not p.is_closed():
            try:
                await p.close()
            except Exception:
                pass
    try:
        return not page.is_closed() and await page.locator(BUTTON_SELECTOR).is_visible()
    except Exception:
        return False


async def _round(run, timeout: float):
    """One press of the button, trying each method in turn until something happens."""
    run.network_hit.clear()
    run.error_hit.clear()
    links, run.baseline_errors = await _probe(run.context, run.origin_host, run.error_re)
    run.baseline = set(links)
    run.token_seen = False
    run.token_clicked = False
    run.watch = run.baseline_errors & run.prev_error_lines  # old error still showing from last press
    run.stale_cleared = False
    run.deadline = time.monotonic() + timeout

    order = run.method_order
    for i, method in enumerate(order):
        last = i == len(order) - 1
        label = METHOD_LABELS[method]
        presses = 0
        while True:
            try:
                await run.page.bring_to_front()
            except Exception:
                pass
            log(f"Attempt {i + 1}/{len(order)}: {label}" + (f" - press again #{presses}" if presses else ""))

            # remember what things looked like, so we can tell if the button's handler ran
            run.site_activity.clear()
            run.ad_popup_at = run.ad_popup_url = None
            run.click_url = run.page.url
            run.method = method
            run.click_state = await _button_state(run.page)
            if run.discovery is not None:
                run.discovery.append({"event": "press", "method": method, "time": round(time.time(), 2)})
            performed = await _click_attempt(run.page, method)
            run.click_time = time.monotonic()
            if not performed:
                log("Could not press with this method - trying the next one")
                result = None
                try:
                    if await run.page.locator(BUTTON_SELECTOR).count() == 0:
                        log("Button is gone - reloading the page")
                        await _fresh_start(run.context, run.page, run.start_url)
                except Exception:
                    pass
                break

            log("Waiting for a token page or a direct gdrive link")
            remaining = run.deadline - time.monotonic()
            wait = remaining if last else min(3 if method == "call" else 6, remaining)
            # An unchanged old error only counts as "press failed" for the method that
            # is known to register (or the last one) - otherwise try the next method first.
            persist = last or method == run.working_method
            result = await _poll(run, wait, count_persist=persist)
            if result is None and run.token_seen:
                return None  # token page is open: never press again, just report what happened

            if result and result[0] == "swallowed":
                if presses < MAX_REPRESS and time.monotonic() < run.deadline:
                    presses += 1
                    log("An ad popup took the click, the button did nothing - pressing again")
                    await _ready_to_press(run.context, run.page)  # closes the popup tabs
                    if not _same_site(_host(run.page.url), run.origin_host):
                        await _recover(run.page, run.start_url)
                    continue
                log("Ad popups keep swallowing this method")
                result = None
            break

        if result:
            if result[0] == "error":
                run.working_method = method  # this method reached the site
                run.method_order = [method] + [m for m in run.methods if m != method]
            return result
        if not last and not _same_site(_host(run.page.url), run.origin_host):
            await _recover(run.page, run.start_url)
    return None


async def _dump_tabs(context, debug_dir="."):
    """On failure: save every open tab's HTML so you can see what the token page really contains.
    debug_dir=None disables it."""
    if not debug_dir:
        return
    Path(debug_dir).mkdir(parents=True, exist_ok=True)
    for i, p in enumerate(list(context.pages)):
        try:
            target = Path(debug_dir) / f"debug_tab{i}.html"
            target.write_text(await p.content(), encoding="utf-8")
            log(f"Saved tab {i} ({p.url[:100]}) to {target}")
        except Exception:
            pass


async def _recover(page, start_url: str):
    """If an overlay link sent the main tab to another site, go back."""
    log("Main tab was sent to another site - going back")
    try:
        await page.go_back(wait_until="domcontentloaded", timeout=8_000)
    except Exception:
        pass
    try:
        if not _same_site(_host(page.url), _host(start_url)):
            await page.goto(start_url, wait_until="domcontentloaded", timeout=15_000)
        await page.locator(BUTTON_SELECTOR).wait_for(state="visible", timeout=8_000)
    except Exception as e:
        log(f"Could not restore the page: {str(e).splitlines()[0]}")


# --------------------------------------------------------------------------
# main logic
# --------------------------------------------------------------------------
@contextlib.asynccontextmanager
async def _browser_scope(shared_browser, headless: bool, slow_mo: int, block_resources: bool):
    """Yield the shared browser if given, otherwise launch (and later close) our own."""
    if shared_browser is not None:
        yield shared_browser
        return
    args = list(LAUNCH_ARGS)
    if block_resources:
        args.append("--blink-settings=imagesEnabled=false")  # blocks images with zero overhead
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless, slow_mo=slow_mo, args=args)
        try:
            yield browser
        finally:
            await browser.close()


async def get_gdrive_link(
    url: str,
    state_path: str = "state.json",
    meta_path: str = "meta.json",
    timeout: float = 30,
    headless: bool = True,
    save_state: bool = False,
    slow_mo: int = 0,
    hold: float = 3,
    close_popups: bool = True,
    max_retries: int = 30,
    retry_delay: float = 0,
    error_pattern: str = "",
    reload_on_retry: bool = False,
    methods: list = None,
    discover: bool = False,
    discover_path: str = "discover.json",
    block_resources: bool = True,   # images / fonts / media (any mode, not just headless)
    block_trackers: bool = True,    # analytics scripts
    block_ads: bool = False,        # ad networks (opt-in)
    shared_browser=None,            # reuse a running browser (saves the launch time)
    token_pattern: str = TOKEN_URL_PATTERN,  # URL regex that marks the site's token page ('' disables)
    token_click: str = "",          # optional CSS selector to click on the token page
    debug_dir: str = ".",           # where to dump tab HTML when no link is found (None/"" = off)
) -> str:
    methods = list(methods or METHODS)
    bad = [m for m in methods if m not in METHOD_LABELS]
    if bad:
        raise ValueError(f"unknown method(s) {bad}; choose from {list(METHOD_LABELS)}")
    state = _load_json(state_path)
    meta = _load_json(meta_path)
    if state is None:
        raise FileNotFoundError(f"{state_path} not found - the button needs a logged-in session")

    async with _browser_scope(shared_browser, headless, slow_mo, block_resources) as browser:
        ctx_opts = build_context_options(state, meta)
        ctx_opts.setdefault("service_workers", "block")  # SWs can bypass routing and add overhead
        context = await browser.new_context(**ctx_opts)

        # speed: abort useless requests inside the browser layer
        async def _abort(route):
            try:
                await route.abort()
            except Exception:
                pass

        if block_resources:
            await context.route(BLOCKED_EXT_RE, _abort)
            await context.route(FONT_HOST_RE, _abort)
        if block_trackers:
            await context.route(TRACKER_HOST_RE, _abort)
        if block_ads:
            await context.route(AD_HOST_RE, _abort)

        # catch a direct redirect / navigation to gdrive (covers popups too)
        network_hit: list[str] = []
        error_hit: list[str] = []  # quota/limit errors seen via alert() or HTTP 429
        site_activity: list[str] = []  # requests to the site itself (ajax/navigation) since the last press
        site_resp: list = []  # monotonic times of the site's own ajax replies
        base_host = _host(url)
        site_host = [base_host]  # updated once the page has loaded (in case of redirects)
        error_re = re.compile(
            DEFAULT_ERROR_PATTERN + (f"|{error_pattern}" if error_pattern else ""), re.I
        )

        wake = asyncio.Event()  # set by network events so the poll loop reacts instantly

        # Once the gdrive link is known, don't waste time loading the Drive page itself.
        async def _route_gdrive(route):
            req = route.request
            hit = find_gdrive_links(req.url) if req.is_navigation_request() else []
            try:
                if hit:
                    network_hit.extend(hit)
                    wake.set()
                    await route.abort()
                else:
                    await route.continue_()
            except Exception:
                pass

        await context.route(GDRIVE_HOST_RE, _route_gdrive)

        def _on_request(req):
            if req.is_navigation_request():
                hit = find_gdrive_links(req.url)
                if hit:
                    network_hit.extend(hit)
                    wake.set()
            if req.resource_type in ("xhr", "fetch", "document") and _same_site(_host(req.url), site_host[0]):
                site_activity.append(req.url)

        async def _scan_body(resp):
            """Token pages often get the link from an ajax/json response, never writing it into the DOM."""
            try:
                ct = resp.headers.get("content-type", "")
                if not any(k in ct for k in ("json", "text", "javascript", "html", "xml")):
                    return
                if int(resp.headers.get("content-length") or 0) > 500_000:
                    return
                hit = find_gdrive_links(await resp.text())
            except Exception:
                return
            if hit:
                log(f"Gdrive link found in a response from {resp.url[:80]}")
                network_hit.extend(hit)
                wake.set()

        def _on_response(resp):
            if resp.request.resource_type in ("xhr", "fetch") and _same_site(_host(resp.url), base_host):
                site_resp.append(time.monotonic())
            if resp.status == 200 and resp.request.resource_type in ("xhr", "fetch"):
                asyncio.ensure_future(_scan_body(resp))
            loc = resp.headers.get("location")
            if loc:
                hit = find_gdrive_links(loc)
                if hit:
                    network_hit.extend(hit)
                    wake.set()
            if (
                resp.status == 429
                and resp.request.resource_type in ("document", "xhr", "fetch")
                and _same_site(_host(resp.url), base_host)
            ):
                error_hit.append(f"HTTP 429 from {resp.url[:80]}")
                wake.set()

        def _on_dialog(dialog):
            msg = dialog.message
            log(f"Page dialog: {msg[:120]}")
            if error_re.search(msg):
                error_hit.append(msg)
                wake.set()
            asyncio.ensure_future(dialog.accept())

        context.on("request", _on_request)
        context.on("response", _on_response)
        context.on("page", lambda pg: pg.on("dialog", _on_dialog))

        # --discover: record what the site sends, so the button can be replaced by a direct request
        discovery: list = []
        if discover:
            async def _record(resp):
                req = resp.request
                try:
                    preview = (await resp.text())[:400]
                except Exception:
                    preview = "<no body>"
                try:
                    post = (req.post_data or "")[:1000]
                except Exception:
                    post = "<binary>"
                headers = {
                    k: v for k, v in req.headers.items()
                    if k.lower() in ("content-type", "referer", "origin", "accept")
                    or k.lower().startswith("x-") or "csrf" in k.lower() or "token" in k.lower()
                }  # cookies are deliberately left out
                discovery.append({
                    "time": round(time.time(), 2), "method": req.method, "url": req.url,
                    "type": req.resource_type, "status": resp.status,
                    "headers": headers, "post_data": post, "response_preview": preview,
                })

            def _on_record(resp):
                r = resp.request
                if r.resource_type in ("xhr", "fetch", "document") and _same_site(_host(resp.url), site_host[0]):
                    asyncio.ensure_future(_record(resp))

            context.on("response", _on_record)

        try:
            page = await context.new_page()
            log(f"Opening {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))

            # 1) the button (only rendered for logged-in users)
            site_host[0] = _host(page.url)
            log(f"Page loaded: {page.url} - looking for {BUTTON_SELECTOR}")
            btn = page.locator(BUTTON_SELECTOR)
            try:
                await btn.wait_for(state="visible", timeout=10_000)
            except Exception:
                raise NotLoggedInError(
                    f"'{BUTTON_SELECTOR}' button not found on {page.url} - "
                    "session in state.json is probably expired/logged out"
                )

            log("Button found (logged in)")
            run = SimpleNamespace(
                context=context, page=page, start_url=page.url, origin_host=_host(page.url),
                network_hit=network_hit, error_hit=error_hit, error_re=error_re,
                close_popups=close_popups, first_seen={}, baseline=set(), baseline_errors=set(),
                prev_error_lines=set(), watch=set(), stale_cleared=False, click_time=0.0,
                working_method=None, methods=methods, method_order=list(methods),
                discovery=discovery if discover else None,
                site_activity=site_activity, site_resp=site_resp, click_url="", click_state=None,
                ad_popup_at=None, ad_popup_url=None, wake=wake, method=None,
                token_re=re.compile(token_pattern, re.I) if token_pattern else None,
                token_click=token_click, token_seen=False, token_clicked=False,
                token_last_log=0.0, deadline=0.0,
            )

            # 2) press the button; on a quota/limit error wait and press it again
            last_error = None
            for retry in range(max_retries + 1):
                if retry:
                    delay = max(retry_delay * retry, RETRY_MIN_GAP)
                    log(f"Retry {retry}/{max_retries} in {delay:g}s (reason: {last_error})")
                    await asyncio.sleep(delay)
                    if not reload_on_retry:
                        await _wait_button_enabled(page)  # press the moment the button is enabled again
                    if not reload_on_retry and await _ready_to_press(context, page):
                        log("Pressing the button again (no reload)")
                    else:
                        if not reload_on_retry:
                            log("Button is no longer on the page - reloading it")
                        if not await _fresh_start(context, page, run.start_url):
                            last_error = "button did not come back after reload"
                            continue

                result = await _round(run, timeout)
                if result is None:
                    await _dump_tabs(context, debug_dir)
                    raise LinkNotFoundError(
                        f"No gdrive link appeared within {timeout}s (last url: {page.url}). "
                        "If the page shows an error, pass --error-regex to match its text."
                    )
                kind, value = result
                if kind == "link":
                    return value
                last_error = value
                run.prev_error_lines = {value}
                log(f"Error detected on the page: {value}")

            raise LinkNotFoundError(
                f"Gave up after {max_retries} retries - last error: {last_error}"
            )
        finally:
            if discover:
                await asyncio.sleep(0.5)  # let the last responses finish being recorded
                Path(discover_path).write_text(json.dumps(discovery, indent=2), encoding="utf-8")
                log(f"Saved {len(discovery)} recorded request(s) to {discover_path}")
            if not headless and hold > 0:
                log(f"Closing browser in {hold:g}s")
                await asyncio.sleep(hold)
            if save_state and state_path:
                try:
                    await context.storage_state(path=state_path)
                except Exception:
                    pass
            try:
                await context.close()
            except Exception:
                pass


def get_gdrive_link_sync(url: str, **kwargs) -> str:
    return asyncio.run(get_gdrive_link(url, **kwargs))


async def get_gdrive_links(urls: list, concurrency: int = 3, **kwargs) -> dict:
    """Resolve many URLs with ONE browser launch, `concurrency` at a time.
    Returns {url: link_or_Exception}."""
    sem = asyncio.Semaphore(concurrency)
    async with _browser_scope(
        None, kwargs.get("headless", True), kwargs.get("slow_mo", 0), kwargs.get("block_resources", True)
    ) as browser:
        async def one(u):
            async with sem:
                try:
                    return u, await get_gdrive_link(u, shared_browser=browser, **kwargs)
                except Exception as e:
                    return u, e
        return dict(await asyncio.gather(*(one(u) for u in urls)))


def get_gdrive_links_sync(urls: list, **kwargs) -> dict:
    return asyncio.run(get_gdrive_links(urls, **kwargs))


def main():
    ap = argparse.ArgumentParser(description="Click #ddl and return the Google Drive link")
    ap.add_argument("url", nargs="+", help="one or more URLs (several = one shared browser)")
    ap.add_argument("--state", default="state.json")
    ap.add_argument("--meta", default="meta.json")
    ap.add_argument("--timeout", type=float, default=30)
    ap.add_argument("--headed", action="store_true", help="show the browser (default: headless = faster)")
    ap.add_argument("--headless", action="store_true", help=argparse.SUPPRESS)  # kept for old command lines
    ap.add_argument("--load-all", action="store_true", help="don't block images/fonts/media/trackers")
    ap.add_argument("--block-ads", action="store_true", help="also block known ad networks")
    ap.add_argument("--token-regex", default=TOKEN_URL_PATTERN, help="regex for the token page URL (default: token= in the URL)")
    ap.add_argument("--token-click", default="", help="CSS selector of a button to click on the token page")
    ap.add_argument("--concurrency", type=int, default=3, help="parallel tabs when several URLs are given")
    ap.add_argument("--slow", type=int, default=0, help="slow down every browser action by N ms")
    ap.add_argument("--keep-popups", action="store_true", help="don't auto-close extra tabs from other sites")
    ap.add_argument("--retries", type=int, default=30, help="times to press again after a quota/limit error")
    ap.add_argument("--retry-delay", type=float, default=0, help="extra wait before a retry (grows each time); 0 = press as soon as the button is enabled")
    ap.add_argument("--error-regex", default="", help="extra regex for page error text that should trigger a retry")
    ap.add_argument("--reload-on-retry", action="store_true", help="reload the page before pressing again (default: just press again)")
    ap.add_argument("--methods", default=",".join(METHODS), help="how to trigger the button, in order: call,js,real")
    ap.add_argument("--discover", action="store_true", help="record the site's requests around the press into discover.json")
    ap.add_argument("--discover-file", default="discover.json")
    ap.add_argument("--hold", type=float, default=0, help="seconds to keep the browser open at the end")
    ap.add_argument("--save-state", action="store_true", help="write refreshed cookies back to state.json")
    args = ap.parse_args()

    opts = dict(
        state_path=args.state,
        meta_path=args.meta,
        timeout=args.timeout,
        headless=not args.headed,
        save_state=args.save_state,
        slow_mo=args.slow,
        hold=args.hold,
        close_popups=not args.keep_popups,
        max_retries=args.retries,
        retry_delay=args.retry_delay,
        error_pattern=args.error_regex,
        reload_on_retry=args.reload_on_retry,
        methods=[m.strip() for m in args.methods.split(",") if m.strip()],
        discover=args.discover,
        discover_path=args.discover_file,
        block_resources=not args.load_all,
        block_trackers=not args.load_all,
        block_ads=args.block_ads,
        token_pattern=args.token_regex,
        token_click=args.token_click,
    )

    try:
        if len(args.url) == 1:
            print(get_gdrive_link_sync(args.url[0], **opts))
            return
        results = get_gdrive_links_sync(args.url, concurrency=args.concurrency, **opts)
    except (NotLoggedInError, LinkNotFoundError, FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    failed = False
    for u, r in results.items():
        if isinstance(r, Exception):
            failed = True
            print(f"{u}\tERROR: {r}")
        else:
            print(f"{u}\t{r}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()