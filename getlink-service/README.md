# GetLink service

Send a site URL (e.g. `https://driveseed.org/file/...`), get the Google Drive link back.
It runs your logged-in Chromium session (`state.json` + `meta.json`) behind a small HTTP API.

```
your app ──HTTPS──► Caddy ──► FastAPI (one shared Chromium) ──► site ──► Drive link
```

## API

All calls need your app key: header `X-API-Key: <key from API_KEYS>`.

| Method | Path | What it does |
|---|---|---|
| `GET`  | `/health` | liveness check (no key) |
| `GET`  | `/link?url=...` | get one link |
| `POST` | `/link` | same, JSON body `{"url": "...", "timeout": 30, "retries": 3, "retry_delay": 10, "fresh": false}` (all but `url` optional) |
| `POST` | `/links` | up to 20 URLs: `{"urls": ["...", "..."]}` |
| `GET`  | `/status` | queue, browser and session info |

Success:
```json
{"ok": true, "url": "https://driveseed.org/file/abc", "link": "https://drive.google.com/file/d/…/view", "cached": false, "elapsed_ms": 4210}
```
Failure (`ok:false`, HTTP status + a stable `error` code your app can switch on):

| `error` | HTTP | meaning / what to do |
|---|---|---|
| `unauthorized` | 401 | wrong/missing key |
| `session_expired` | 503 | login cookies are dead → push a fresh `state.json` |
| `no_session` | 503 | `state.json` never uploaded |
| `rate_limited` | 429 | Drive/site quota; retry later |
| `busy` | 429 | too many requests queued; retry shortly |
| `link_not_found` | 502 | pressed the button, no link appeared |
| `timeout` | 504 | exceeded `REQUEST_TIMEOUT` |
| `host_not_allowed` / `bad_url` | 403 / 400 | URL rejected (see `ALLOWED_HOSTS`) |

### Admin (needs `X-API-Key: <ADMIN_KEY>`)

| Method | Path | |
|---|---|---|
| `PUT`  | `/admin/state` | body = contents of `state.json` |
| `PUT`  | `/admin/meta` | body = contents of `meta.json` |
| `POST` | `/admin/upload` | multipart, fields `state` and/or `meta` (files) |
| `GET`  | `/admin/status` | which files exist, cookie count, earliest cookie expiry |
| `POST` | `/admin/reload-browser` | restart Chromium |

Uploads are validated, written atomically and take effect on the **next request** (no restart). The previous file is kept as `*.json.bak`.

### Push your JSON files

```bash
# helper script (no dependencies)
python scripts/push_json.py --server https://api.example.com --admin-key YOUR_ADMIN_KEY --state state.json --meta meta.json

# or plain curl
curl -X PUT -H "X-API-Key: YOUR_ADMIN_KEY" --data-binary @state.json https://api.example.com/admin/state
curl -H "X-API-Key: YOUR_ADMIN_KEY" -F state=@state.json -F meta=@meta.json https://api.example.com/admin/upload
```
PowerShell: `curl.exe -X PUT -H "X-API-Key: YOUR_ADMIN_KEY" --data-binary "@state.json" https://api.example.com/admin/state`

### Calling it from your app

```python
import requests
r = requests.get("https://api.example.com/link", params={"url": page_url},
                 headers={"X-API-Key": "APP_KEY"}, timeout=130)
data = r.json()
link = data["link"] if data["ok"] else None   # else look at data["error"]
```
```js
const r = await fetch("https://api.example.com/link", {
  method: "POST",
  headers: {"X-API-Key": "APP_KEY", "Content-Type": "application/json"},
  body: JSON.stringify({url: pageUrl}),
});
const data = await r.json();
```
Set your HTTP client timeout above `REQUEST_TIMEOUT` (default 120 s). Don't put the key in a public client; call this from your own backend.

## Run locally

```bash
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt && playwright install chromium
cp .env.example .env                                  # edit keys, ALLOWED_HOSTS
set -a; . ./.env; set +a                              # (PowerShell: set the vars by hand)
uvicorn app.main:app --port 8000 --workers 1
python scripts/push_json.py --admin-key <ADMIN_KEY> --state state.json --meta meta.json
```
The original command-line tool still works: `python app/getlink.py URL --state state.json --meta meta.json --headed`.

## Push to GitHub

```bash
git init && git add . && git commit -m "getlink service"
git branch -M main
git remote add origin https://github.com/<you>/getlink-service.git
git push -u origin main
```
`.gitignore` already excludes `.env`, `state.json`, `meta.json` and `data/`. **They contain your login session; never commit them.** Use a private repo anyway.

## Deploy on Oracle Cloud (Ubuntu, Docker)

1. **Open the ports twice** (Oracle blocks them in two places):
   - OCI console → your VCN → Security List → add ingress rules for TCP **80** and **443** from `0.0.0.0/0`.
   - On the VM:
     ```bash
     sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80  -j ACCEPT
     sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
     sudo netfilter-persistent save   # sudo apt install iptables-persistent if missing
     ```
2. **Install Docker**: `curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker $USER` (log out/in).
3. **Small VM (1 GB RAM)?** Add swap and set `MAX_CONCURRENCY=1`:
   `sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile`
4. **Get the code and configure**
   ```bash
   git clone https://github.com/<you>/getlink-service.git && cd getlink-service
   cp .env.example .env && nano .env      # set API_KEYS, ADMIN_KEY, ALLOWED_HOSTS, DOMAIN
   ```
   Generate strong keys with `openssl rand -hex 24`.
5. **Start**
   - With HTTPS (point a DNS A record for `DOMAIN` to the VM's public IP first):
     `docker compose --profile https up -d --build`
   - Without a domain (plain HTTP on port 8000; test only): change the port mapping in `docker-compose.yml` to `"8000:8000"`, open 8000 like step 1, then `docker compose up -d --build`.
6. **Push your session** from your PC: `python scripts/push_json.py --server https://api.example.com --admin-key <ADMIN_KEY> --state state.json --meta meta.json`
7. **Test**: `curl -H "X-API-Key: <APP_KEY>" "https://api.example.com/link?url=https://driveseed.org/file/..."`

Update later: `git pull && docker compose --profile https up -d --build`.
Logs: `docker compose logs -f getlink`. ARM (Ampere) and x86 instances both work.

No Docker? See `deploy/getlink.service` (systemd) and run `playwright install --with-deps chromium` in a venv.

## Configuration (`.env`)

| Variable | Default | Notes |
|---|---|---|
| `API_KEYS` | – (required) | comma-separated; server refuses to start without it |
| `ADMIN_KEY` | – | empty = admin endpoints disabled |
| `ALLOWED_HOSTS` | – | e.g. `driveseed.org`. Empty = any public host (internal/LAN/metadata addresses are always blocked) |
| `MAX_CONCURRENCY` | 2 | parallel browser tabs |
| `MAX_QUEUE` | 20 | waiting requests before `busy` |
| `DEFAULT_TIMEOUT` / `MAX_TIMEOUT` | 30 / 90 | seconds per press |
| `REQUEST_TIMEOUT` | 120 | hard cap per request |
| `RETRIES` / `RETRY_DELAY` | 3 / 10 | quota-error re-presses (delay grows: 10, 20, 30 s) |
| `CACHE_TTL` | 300 | reuse a link for N s (identical simultaneous requests always share one run) |
| `SAVE_STATE` | false | persist refreshed cookies |
| `DEBUG_DUMP` | false | on failure save tab HTML to `data/debug/` (`docker compose exec getlink ls /data/debug`) |

## Notes

- **One worker only.** The browser is shared in-process; scale with `MAX_CONCURRENCY`, not `--workers`.
- Chromium restarts itself after a crash and every `RECYCLE_AFTER` requests.
- Watch `/admin/status` → `earliest_cookie_expiry` to know when to re-login and push a new `state.json`.
- Drive's "requests per minute" quota is the real throttle; the cache and retry settings exist to stay under it.
