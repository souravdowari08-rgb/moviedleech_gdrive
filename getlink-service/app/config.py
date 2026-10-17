"""All settings come from environment variables (see .env.example)."""
import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None or v.strip() == "" else v.strip().lower() in ("1", "true", "yes", "on")


def _num(name: str, default, cast=float):
    v = os.getenv(name)
    return default if v is None or v.strip() == "" else cast(v)


def _list(name: str) -> list:
    return [x.strip() for x in os.getenv(name, "").split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    api_keys: list            # keys your app uses to call /link
    admin_key: str            # key for /admin/* (uploading state.json / meta.json)
    data_dir: Path            # where state.json / meta.json live
    allowed_hosts: list       # only these sites may be opened (strongly recommended)
    max_concurrency: int      # parallel browser contexts
    max_queue: int            # requests allowed to wait for a free slot before HTTP 429
    default_timeout: float    # seconds to wait for the link, per press
    max_timeout: float
    request_timeout: float    # hard cap for one whole request incl. retries
    retries: int              # max re-presses after a quota/limit error (REQUEST_TIMEOUT is the real limit)
    retry_delay: float        # extra wait between retries; 0 = press as soon as the button is enabled
    cache_ttl: int            # seconds a resolved link is reused (0 = off)
    save_state: bool          # write refreshed cookies back to state.json
    no_sandbox: bool          # needed when Chromium runs inside Docker
    recycle_after: int        # restart Chromium after N requests (memory hygiene)
    debug_dump: bool          # save tab HTML to DATA_DIR/debug when no link is found
    docs: bool                # expose /docs (Swagger UI)
    allow_query_key: bool     # accept ?key=... in the URL (handy for browser tests; keys end up in logs)
    max_json_bytes: int


def load() -> Settings:
    return Settings(
        api_keys=_list("API_KEYS"),
        admin_key=os.getenv("ADMIN_KEY", "").strip(),
        data_dir=Path(os.getenv("DATA_DIR", "./data")),
        allowed_hosts=[h.lower() for h in _list("ALLOWED_HOSTS")],
        max_concurrency=_num("MAX_CONCURRENCY", 2, int),
        max_queue=_num("MAX_QUEUE", 20, int),
        default_timeout=_num("DEFAULT_TIMEOUT", 30.0),
        max_timeout=_num("MAX_TIMEOUT", 90.0),
        request_timeout=_num("REQUEST_TIMEOUT", 120.0),
        retries=_num("RETRIES", 30, int),
        retry_delay=_num("RETRY_DELAY", 0.0),
        cache_ttl=_num("CACHE_TTL", 300, int),
        save_state=_bool("SAVE_STATE", False),
        no_sandbox=_bool("CHROMIUM_NO_SANDBOX", True),
        recycle_after=_num("RECYCLE_AFTER", 200, int),
        debug_dump=_bool("DEBUG_DUMP", False),
        docs=_bool("DOCS_ENABLED", False),
        allow_query_key=_bool("ALLOW_QUERY_KEY", False),
        max_json_bytes=_num("MAX_JSON_BYTES", 2_000_000, int),
    )


settings = load()