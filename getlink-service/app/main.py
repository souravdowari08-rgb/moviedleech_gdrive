"""GetLink web service: send a site URL, get the Google Drive link back.

Run with ONE worker (the Chromium instance is shared inside the process):
    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
import asyncio
import ipaddress
import json
import os
import secrets
import shutil
import socket
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, Header, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright
from pydantic import BaseModel, Field

from . import getlink as core
from .config import settings as cfg

log = core.log


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


def _map_error(e: Exception) -> ApiError:
    """Turn anything the scraper raises into a stable {error: code} the app can act on."""
    if isinstance(e, ApiError):
        return e
    msg = str(e) or e.__class__.__name__
    if isinstance(e, core.NotLoggedInError):
        return ApiError(503, "session_expired", msg + " -> upload a fresh state.json (PUT /admin/state)")
    if isinstance(e, FileNotFoundError):
        return ApiError(503, "no_session", "state.json has not been uploaded yet (PUT /admin/state)")
    if isinstance(e, core.LinkNotFoundError):
        low = msg.lower()
        if "quota" in low or "limit" in low or "429" in low or "too many" in low:
            return ApiError(429, "rate_limited", msg)
        return ApiError(502, "link_not_found", msg)
    if isinstance(e, ValueError):
        return ApiError(400, "bad_request", msg)
    if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
        return ApiError(504, "timeout", f"no link within {cfg.request_timeout:g}s")
    log(f"Unexpected error: {e!r}")
    return ApiError(500, "internal_error", msg[:300])


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------
def _key_from(x_api_key: Optional[str], authorization: Optional[str], query_key: Optional[str] = None) -> str:
    if cfg.allow_query_key and query_key:
        return query_key.strip()
    if x_api_key:
        return x_api_key.strip()
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def _same(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode(), b.encode())


def require_api(
    x_api_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
    key: Optional[str] = Query(None, include_in_schema=False),
):
    key = _key_from(x_api_key, authorization, key)
    if not key or not any(_same(key, k) for k in cfg.api_keys):
        raise ApiError(401, "unauthorized", "missing or invalid API key (send X-API-Key)")


def require_admin(x_api_key: Optional[str] = Header(None), authorization: Optional[str] = Header(None)):  # admin: header only
    if not cfg.admin_key:
        raise ApiError(403, "admin_disabled", "ADMIN_KEY is not set on the server")
    key = _key_from(x_api_key, authorization)
    if not key or not _same(key, cfg.admin_key):
        raise ApiError(401, "unauthorized", "missing or invalid admin key")


# --------------------------------------------------------------------------
# target URL validation (SSRF protection)
# --------------------------------------------------------------------------
async def validate_target(url: str) -> str:
    url = (url or "").strip()
    p = urlparse(url)
    if len(url) > 2048 or p.scheme not in ("http", "https") or not p.hostname:
        raise ApiError(400, "bad_url", "url must be a valid http(s) URL")
    host = p.hostname.lower()
    if cfg.allowed_hosts:
        if not any(host == h or host.endswith("." + h) for h in cfg.allowed_hosts):
            raise ApiError(403, "host_not_allowed", f"{host} is not in ALLOWED_HOSTS")
        return url
    # No allow-list configured: at least refuse localhost / LAN / cloud-metadata addresses.
    if host == "localhost" or host.endswith((".local", ".internal")):
        raise ApiError(403, "host_not_allowed", "internal hosts are blocked")
    try:
        ips = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
        except socket.gaierror:
            raise ApiError(400, "bad_url", f"{host} does not resolve")
        ips = [ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos]
    if any(not ip.is_global for ip in ips):
        raise ApiError(403, "host_not_allowed", "internal addresses are blocked")
    return url


# --------------------------------------------------------------------------
# shared browser
# --------------------------------------------------------------------------
class BrowserManager:
    def __init__(self):
        self.pw = None
        self.browser = None
        self.lock = asyncio.Lock()
        self.active = 0
        self.uses = 0

    async def start(self):
        self.pw = await async_playwright().start()
        await self._launch()

    async def _launch(self):
        args = list(core.LAUNCH_ARGS) + ["--blink-settings=imagesEnabled=false", "--disable-dev-shm-usage"]
        if cfg.no_sandbox:
            args.append("--no-sandbox")
        self.browser = await self.pw.chromium.launch(headless=True, args=args)
        self.uses = 0
        log("Chromium started")

    async def _restart(self):
        if self.browser is not None:
            try:
                await self.browser.close()
            except Exception:
                pass
        await self._launch()

    async def acquire(self):
        async with self.lock:
            dead = self.browser is None or not self.browser.is_connected()
            worn = self.uses >= cfg.recycle_after and self.active == 0
            if dead or worn:
                log("Restarting Chromium" + (" (crashed)" if dead else " (recycle)"))
                await self._restart()
            self.active += 1
            self.uses += 1
            return self.browser

    def release(self):
        self.active = max(0, self.active - 1)

    async def restart_now(self):
        async with self.lock:
            await self._restart()

    async def stop(self):
        try:
            if self.browser:
                await self.browser.close()
        finally:
            if self.pw:
                await self.pw.stop()


mgr = BrowserManager()
_sem: Optional[asyncio.Semaphore] = None
_waiting = 0
_cache: dict = {}      # url -> (link, expires_at)
_inflight: dict = {}   # url -> running task (identical concurrent requests share one browser run)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _sem
    if not cfg.api_keys:
        raise RuntimeError("API_KEYS is empty - refusing to start an unauthenticated service")
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    _sem = asyncio.Semaphore(max(1, cfg.max_concurrency))
    await mgr.start()
    log(f"Ready. data_dir={cfg.data_dir.resolve()} concurrency={cfg.max_concurrency} "
        f"allowed_hosts={cfg.allowed_hosts or 'ANY(public)'}")
    try:
        yield
    finally:
        await mgr.stop()


app = FastAPI(
    title="GetLink",
    version="1.0",
    lifespan=lifespan,
    docs_url="/docs" if cfg.docs else None,
    redoc_url=None,
    openapi_url="/openapi.json" if cfg.docs else None,
)


@app.exception_handler(ApiError)
async def _api_error_handler(_req: Request, exc: ApiError):
    return JSONResponse(status_code=exc.status, content={"ok": False, "error": exc.code, "message": exc.message})


# --------------------------------------------------------------------------
# core call
# --------------------------------------------------------------------------
async def _run_core(url: str, timeout: float, retries: int, retry_delay: float) -> str:
    global _waiting
    if _waiting >= cfg.max_queue:
        raise ApiError(429, "busy", "too many requests queued, retry shortly")
    _waiting += 1
    try:
        await _sem.acquire()
    finally:
        _waiting -= 1
    try:
        browser = await mgr.acquire()
        try:
            return await asyncio.wait_for(
                core.get_gdrive_link(
                    url,
                    state_path=str(cfg.data_dir / "state.json"),
                    meta_path=str(cfg.data_dir / "meta.json"),
                    timeout=timeout,
                    headless=True,
                    hold=0,
                    save_state=cfg.save_state,
                    max_retries=retries,
                    retry_delay=retry_delay,
                    shared_browser=browser,
                    debug_dir=str(cfg.data_dir / "debug") if cfg.debug_dump else None,
                ),
                cfg.request_timeout,
            )
        finally:
            mgr.release()
    finally:
        _sem.release()


async def resolve_link(url: str, timeout: float, retries: int, retry_delay: float, fresh: bool):
    """Cache + in-flight de-duplication in front of the browser."""
    now = time.monotonic()
    if cfg.cache_ttl and not fresh:
        hit = _cache.get(url)
        if hit and hit[1] > now:
            return hit[0], True
    task = _inflight.get(url)
    if task is None:
        task = asyncio.create_task(_run_core(url, timeout, retries, retry_delay))
        _inflight[url] = task

        def _done(t, url=url):
            _inflight.pop(url, None)
            if t.cancelled() or t.exception() is not None:
                return
            if cfg.cache_ttl:
                _cache[url] = (t.result(), time.monotonic() + cfg.cache_ttl)
                if len(_cache) > 1000:
                    t_now = time.monotonic()
                    for k in [k for k, v in _cache.items() if v[1] <= t_now] or list(_cache)[:200]:
                        _cache.pop(k, None)

        task.add_done_callback(_done)
    return await asyncio.shield(task), False


class LinkRequest(BaseModel):
    url: str
    timeout: Optional[float] = Field(None, gt=0, description="seconds to wait per press")
    retries: Optional[int] = Field(None, ge=0, le=100)
    retry_delay: Optional[float] = Field(None, ge=0, le=120)
    fresh: bool = Field(False, description="skip the cache")


class BatchRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=20)
    timeout: Optional[float] = Field(None, gt=0)
    retries: Optional[int] = Field(None, ge=0, le=100)
    retry_delay: Optional[float] = Field(None, ge=0, le=120)
    fresh: bool = False


async def _resolve_one(url, timeout, retries, retry_delay, fresh) -> dict:
    t0 = time.monotonic()
    try:
        url = await validate_target(url)
        link, cached = await resolve_link(
            url,
            min(timeout or cfg.default_timeout, cfg.max_timeout),
            cfg.retries if retries is None else retries,
            cfg.retry_delay if retry_delay is None else retry_delay,
            fresh,
        )
    except Exception as e:
        err = _map_error(e)
        raise err
    return {"ok": True, "url": url, "link": link, "cached": cached,
            "elapsed_ms": int((time.monotonic() - t0) * 1000)}


# --------------------------------------------------------------------------
# public endpoints
# --------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/link", dependencies=[Depends(require_api)])
async def link_get(url: str, fresh: bool = False):
    return await _resolve_one(url, None, None, None, fresh)


@app.post("/link", dependencies=[Depends(require_api)])
async def link_post(req: LinkRequest):
    return await _resolve_one(req.url, req.timeout, req.retries, req.retry_delay, req.fresh)


@app.post("/links", dependencies=[Depends(require_api)])
async def links_post(req: BatchRequest):
    async def one(u):
        try:
            return await _resolve_one(u, req.timeout, req.retries, req.retry_delay, req.fresh)
        except ApiError as e:
            return {"ok": False, "url": u, "error": e.code, "message": e.message}
        except Exception as e:
            err = _map_error(e)
            return {"ok": False, "url": u, "error": err.code, "message": err.message}

    return {"results": await asyncio.gather(*(one(u) for u in req.urls))}


# --------------------------------------------------------------------------
# session files (state.json / meta.json)
# --------------------------------------------------------------------------
def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _cookies_of(data) -> list:
    return data if isinstance(data, list) else (data.get("cookies") if isinstance(data, dict) else None)


def _validate(name: str, data):
    if name == "state":
        cookies = _cookies_of(data)
        if not isinstance(cookies, list) or not cookies:
            raise ApiError(422, "bad_state", "state.json needs a non-empty cookies list "
                                             "(Playwright storage_state or a cookie export array)")
        if not all(isinstance(c, dict) and "name" in c and "value" in c for c in cookies):
            raise ApiError(422, "bad_state", "every cookie needs at least name and value")
    elif not isinstance(data, dict):
        raise ApiError(422, "bad_meta", "meta.json must be a JSON object")


def _store(name: str, raw: bytes) -> dict:
    if len(raw) > cfg.max_json_bytes:
        raise ApiError(413, "too_large", f"{name}.json is larger than {cfg.max_json_bytes} bytes")
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except Exception:
        raise ApiError(400, "bad_json", f"{name}.json is not valid JSON")
    _validate(name, data)

    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    target = cfg.data_dir / f"{name}.json"
    if target.exists():
        shutil.copy2(target, cfg.data_dir / f"{name}.json.bak")
    fd, tmp = tempfile.mkstemp(dir=cfg.data_dir, suffix=".tmp")  # atomic: readers never see half a file
    with os.fdopen(fd, "wb") as f:
        f.write(json.dumps(data).encode("utf-8"))
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)
    log(f"{name}.json updated ({target.stat().st_size} bytes)")
    return _file_info(name)


def _file_info(name: str) -> dict:
    path = cfg.data_dir / f"{name}.json"
    if not path.exists():
        return {"present": False}
    st = path.stat()
    info = {"present": True, "bytes": st.st_size, "modified": _iso(st.st_mtime)}
    if name == "state":
        try:
            cookies = _cookies_of(json.loads(path.read_text(encoding="utf-8"))) or []
            info["cookies"] = len(cookies)
            exp = [c.get("expires", c.get("expirationDate")) for c in cookies if isinstance(c, dict)]
            exp = [e for e in exp if isinstance(e, (int, float)) and e > 0]
            if exp:
                info["earliest_cookie_expiry"] = _iso(min(exp))
        except Exception:
            info["unreadable"] = True
    return info


async def _json_body(request: Request) -> bytes:
    if int(request.headers.get("content-length") or 0) > cfg.max_json_bytes:
        raise ApiError(413, "too_large", "body too large")
    return await request.body()


@app.put("/admin/state", dependencies=[Depends(require_admin)])
async def put_state(request: Request):
    """Body = the contents of state.json (JSON)."""
    return {"ok": True, "state": _store("state", await _json_body(request))}


@app.put("/admin/meta", dependencies=[Depends(require_admin)])
async def put_meta(request: Request):
    """Body = the contents of meta.json (JSON)."""
    return {"ok": True, "meta": _store("meta", await _json_body(request))}


@app.post("/admin/upload", dependencies=[Depends(require_admin)])
async def upload(state: Optional[UploadFile] = File(None), meta: Optional[UploadFile] = File(None)):
    """Multipart form: fields `state` and/or `meta` as files."""
    if state is None and meta is None:
        raise ApiError(400, "no_files", "send form fields 'state' and/or 'meta'")
    saved = {}
    for name, f in (("state", state), ("meta", meta)):
        if f is not None:
            saved[name] = _store(name, await f.read(cfg.max_json_bytes + 1))
    return {"ok": True, "saved": saved}


@app.get("/admin/status", dependencies=[Depends(require_admin)])
async def admin_status():
    return {"ok": True, "state": _file_info("state"), "meta": _file_info("meta")}


@app.post("/admin/reload-browser", dependencies=[Depends(require_admin)])
async def reload_browser():
    await mgr.restart_now()
    return {"ok": True}


@app.get("/status", dependencies=[Depends(require_api)])
async def status():
    return {
        "ok": True,
        "browser_connected": bool(mgr.browser and mgr.browser.is_connected()),
        "active": mgr.active,
        "waiting": _waiting,
        "max_concurrency": cfg.max_concurrency,
        "cache_entries": len(_cache),
        "session": _file_info("state"),
    }