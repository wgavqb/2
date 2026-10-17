"""
Gorgias Suite — Autoreg + GorgiasMail + Campaign
Run:  python server.py
Open: http://localhost:5758
"""
from __future__ import annotations
import threading, queue, time, re, csv, io, json, random, string, warnings
import os, hashlib, base64
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, Response, request, jsonify, stream_with_context
import requests as http
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# ── shared autoreg state ───────────────────────────────────────────────────────
_st = {
    "running": False, "stop_flag": False,
    "results": [],    "done": 0, "failed": 0, "start_ts": None,
}
_log_q: queue.Queue = queue.Queue()

# ── proxy state ────────────────────────────────────────────────────────────────
_proxy_list: list = []
_proxy_idx  = 0
_proxy_lock = threading.Lock()

def parse_proxy_line(line: str) -> str | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("http://") or line.startswith("https://") or line.startswith("socks"):
        return line
    parts = line.split(":")
    if len(parts) == 4:
        u, p, h, port = parts
        return f"http://{u}:{p}@{h}:{port}"
    elif len(parts) == 2:
        return f"http://{parts[0]}:{parts[1]}"
    return None

def next_proxy() -> str | None:
    global _proxy_idx
    if not _proxy_list:
        return None
    with _proxy_lock:
        idx = _proxy_idx % len(_proxy_list)
        _proxy_idx += 1
    return _proxy_list[idx]

def proxy_dict(proxy_url: str | None) -> dict:
    if not proxy_url:
        return {}
    return {"http": proxy_url, "https": proxy_url}

# ── captcha state ──────────────────────────────────────────────────────────────
_CAPTCHA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captcha.json")
_captcha = {"service": "capsolver", "key": ""}
try:
    with open(_CAPTCHA_FILE, "r") as _f:
        _saved = json.load(_f)
        _captcha["service"] = _saved.get("service", "capsolver")
        _captcha["key"] = _saved.get("key", "")
except Exception:
    pass

# ── accounts store ─────────────────────────────────────────────────────────────
_ACCOUNTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounts.json")
_accounts: list = []
_accounts_lock = threading.Lock()

def _load_accounts():
    global _accounts
    try:
        with open(_ACCOUNTS_FILE, "r") as f:
            _accounts = json.load(f)
    except Exception:
        _accounts = []

def _save_accounts():
    try:
        with open(_ACCOUNTS_FILE, "w") as f:
            json.dump(_accounts, f, indent=2)
    except Exception:
        pass

_load_accounts()

# ── campaign state ─────────────────────────────────────────────────────────────
_camp = {
    "running": False, "stop": False,
    "sent": 0, "failed": 0, "total": 0,
}
_camp_q: queue.Queue = queue.Queue()

# ── GorgiasMail state ──────────────────────────────────────────────────────────
_gm_q: queue.Queue = queue.Queue()

# ── logging helpers ────────────────────────────────────────────────────────────
def log(msg: str, level: str = "info", q: queue.Queue | None = None):
    ts = datetime.now().strftime("%H:%M:%S")
    entry = {"ts": ts, "level": level, "msg": msg}
    target = q if q else _log_q
    try:
        target.put_nowait(entry)
    except queue.Full:
        pass
    print(f"[{ts}] [{level.upper()}] {msg}")

def rand_str(n=8) -> str:
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=n))

def rand_name() -> str:
    first = random.choice(["Alex","Jordan","Taylor","Morgan","Casey","Riley","Avery","Quinn","Blake","Drew",
                            "Chris","Jamie","Sam","Pat","Robin","Dana","Lee","Kim","Reese","Skyler"])
    last  = random.choice(["Smith","Johnson","Williams","Brown","Jones","Garcia","Martinez","Davis","Wilson","Anderson",
                            "Thomas","Jackson","White","Harris","Martin","Thompson","Young","Allen","King","Wright"])
    return first, last

def rand_company() -> str:
    prefixes = ["Neo","Swift","Apex","Nova","Peak","Bright","Clear","Fast","Smart","Pro"]
    suffixes = ["Store","Shop","Hub","Zone","Base","Central","Direct","Online","Market","Commerce"]
    return random.choice(prefixes) + random.choice(suffixes)

def rand_subdomain(company: str) -> str:
    base = re.sub(r'[^a-z0-9]', '', company.lower())
    return base + rand_str(4)

# ── AnyMessage API ─────────────────────────────────────────────────────────────
ANYMSG_BASE = "https://api.anymessage.shop"

def _anymsg_headers(key: str) -> dict:
    return {"X-Api-Key": key, "Content-Type": "application/json"}

def order_email(api_key: str, proxy: str | None = None) -> dict:
    """Order a new temporary email via AnyMessage.shop API.
    Tries multiple domains (gmx, mailcom, hotmail, outlook) in order.
    """
    proxies = proxy_dict(proxy)
    domains = ["gmx", "mailcom", "hotmail", "outlook"]
    last_err = {}
    for domain in domains:
        try:
            r = http.get(
                f"{ANYMSG_BASE}/email/order",
                params={"token": api_key, "site": "gorgias.com", "domain": domain},
                timeout=30, verify=False, proxies=proxies,
            )
            data = r.json()
            if isinstance(data, dict) and data.get("status") == "success":
                return data
            last_err = data
        except Exception as e:
            last_err = {"error": str(e)}
    # Return standardized error dict
    return {"error": last_err.get("value", str(last_err)), "_raw": last_err}

def get_email_messages(api_key: str, email_id: str, proxy: str | None = None) -> list:
    """Get message for an ordered email via AnyMessage.shop API."""
    proxies = proxy_dict(proxy)
    try:
        r = http.get(
            f"{ANYMSG_BASE}/email/getmessage",
            params={"token": api_key, "id": email_id, "preview": 1},
            timeout=20, verify=False, proxies=proxies,
        )
        data = r.json()
        if isinstance(data, dict) and data.get("status") == "success":
            html = data.get("message", "")
            value = data.get("value", "")
            return [{"body": html, "html": html, "subject": value}]
        # "wait message" = not arrived yet, other errors logged silently
    except Exception:
        pass
    return []

def wait_for_email(api_key: str, email_id: str, keyword: str,
                   timeout: int = 120, proxy: str | None = None) -> str | None:
    """Poll until an email containing keyword arrives. Returns full body or None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        msgs = get_email_messages(api_key, email_id, proxy=proxy)
        for m in msgs:
            body = m.get("body", "") or m.get("html", "") or m.get("text", "") or ""
            subj = m.get("subject", "")
            combined = body + subj
            if keyword.lower() in combined.lower():
                return combined
        time.sleep(5)
    return None

# ── Captcha solving ────────────────────────────────────────────────────────────
CAPSOLVER_URL  = "https://api.capsolver.com"
TWOCAP_URL     = "https://2captcha.com/in.php"
CAPMONSTER_URL = "https://api.capmonster.cloud"
RUCAPTCHA_URL  = "https://rucaptcha.com/in.php"

def solve_recaptcha_v2(sitekey: str, page_url: str, proxy: str | None = None) -> str | None:
    svc = _captcha.get("service", "capsolver")
    key = _captcha.get("key", "")
    if not key:
        return None
    px = proxy_dict(proxy)
    try:
        if svc == "capsolver":
            payload = {
                "clientKey": key,
                "task": {
                    "type": "ReCaptchaV2Task" if proxy else "ReCaptchaV2TaskProxyless",
                    "websiteURL": page_url,
                    "websiteKey": sitekey,
                }
            }
            if proxy:
                parsed = _parse_proxy_for_capsolver(proxy)
                payload["task"].update(parsed)
            r = http.post(f"{CAPSOLVER_URL}/createTask", json=payload, timeout=30, verify=False)
            tid = r.json().get("taskId")
            if not tid:
                return None
            for _ in range(60):
                time.sleep(3)
                r2 = http.post(f"{CAPSOLVER_URL}/getTaskResult", json={"clientKey": key, "taskId": tid}, timeout=20, verify=False)
                d = r2.json()
                if d.get("status") == "ready":
                    return d["solution"]["gRecaptchaResponse"]
        elif svc in ("2captcha", "rucaptcha"):
            base = TWOCAP_URL if svc == "2captcha" else RUCAPTCHA_URL
            params = {"key": key, "method": "userrecaptcha", "googlekey": sitekey, "pageurl": page_url, "json": 1}
            r = http.post(base, params=params, proxies=px, timeout=30, verify=False)
            tid = r.json().get("request")
            if not tid:
                return None
            result_url = base.replace("/in.php", "/res.php")
            for _ in range(60):
                time.sleep(5)
                r2 = http.get(result_url, params={"key": key, "action": "get", "id": tid, "json": 1}, proxies=px, timeout=20, verify=False)
                d = r2.json()
                if d.get("status") == 1:
                    return d.get("request")
                if d.get("request") == "ERROR_CAPTCHA_UNSOLVABLE":
                    return None
        elif svc == "capmonster":
            payload = {
                "clientKey": key,
                "task": {
                    "type": "NoCaptchaTaskProxyless" if not proxy else "NoCaptchaTask",
                    "websiteURL": page_url,
                    "websiteKey": sitekey,
                }
            }
            r = http.post(f"{CAPMONSTER_URL}/createTask", json=payload, proxies=px, timeout=30, verify=False)
            tid = r.json().get("taskId")
            if not tid:
                return None
            for _ in range(60):
                time.sleep(3)
                r2 = http.post(f"{CAPMONSTER_URL}/getTaskResult", json={"clientKey": key, "taskId": tid}, proxies=px, timeout=20, verify=False)
                d = r2.json()
                if d.get("status") == "ready":
                    return d["solution"]["gRecaptchaResponse"]
    except Exception:
        pass
    return None

def solve_recaptcha_v3(sitekey: str, page_url: str, action: str = "signup", proxy: str | None = None) -> str | None:
    svc = _captcha.get("service", "capsolver")
    key = _captcha.get("key", "")
    if not key:
        return None
    try:
        if svc == "capsolver":
            payload = {
                "clientKey": key,
                "task": {
                    "type": "ReCaptchaV3TaskProxyless",
                    "websiteURL": page_url,
                    "websiteKey": sitekey,
                    "pageAction": action,
                    "minScore": 0.5,
                }
            }
            r = http.post(f"{CAPSOLVER_URL}/createTask", json=payload, timeout=30, verify=False)
            tid = r.json().get("taskId")
            if not tid:
                return None
            for _ in range(60):
                time.sleep(3)
                r2 = http.post(f"{CAPSOLVER_URL}/getTaskResult", json={"clientKey": key, "taskId": tid}, timeout=20, verify=False)
                d = r2.json()
                if d.get("status") == "ready":
                    return d["solution"]["gRecaptchaResponse"]
    except Exception:
        pass
    return None

def _parse_proxy_for_capsolver(proxy_url: str) -> dict:
    try:
        import urllib.parse
        parsed = urllib.parse.urlparse(proxy_url)
        result = {
            "proxyType": parsed.scheme or "http",
            "proxyAddress": parsed.hostname or "",
            "proxyPort": parsed.port or 80,
        }
        if parsed.username:
            result["proxyLogin"] = parsed.username
        if parsed.password:
            result["proxyPassword"] = parsed.password
        return result
    except Exception:
        return {}

# ── Gorgias signup / login / API key ──────────────────────────────────────────

GORGIAS_SIGNUP_URL = "https://www.gorgias.com/api/users"
GORGIAS_SIGNUP_ALT = [
    "https://www.gorgias.com/api/accounts",
    "https://api.gorgias.com/api/users",
    "https://www.gorgias.com/users",
]
GORGIAS_SIGNUP_PAGE = "https://www.gorgias.com/get-started"
GORGIAS_RECAPTCHA_SITEKEY = "6LfsAKcUAAAAAHXmT_pBQLhGEfxX8R6bkFyl2bGP"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

def _make_session(proxy: str | None = None) -> http.Session:
    sess = http.Session()
    sess.headers.update({
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Origin": "https://www.gorgias.com",
        "Referer": "https://www.gorgias.com/",
    })
    if proxy:
        sess.proxies.update(proxy_dict(proxy))
    sess.verify = False
    return sess

def _get_csrf_token(sess: http.Session, lq) -> str | None:
    """Try to get CSRF token from signup page."""
    try:
        r = sess.get(GORGIAS_SIGNUP_PAGE, timeout=20)
        for pattern in [
            r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)["\']',
            r'"csrfToken"\s*:\s*"([^"]+)"',
            r'"csrf_token"\s*:\s*"([^"]+)"',
            r'data-csrf=["\']([^"\']+)["\']',
        ]:
            m = re.search(pattern, r.text)
            if m:
                return m.group(1)
    except Exception as e:
        log(f"CSRF fetch failed: {e}", "warn", lq)
    return None

def _extract_confirmation_link(email_body: str) -> str | None:
    """Extract verification/confirmation link from email body."""
    patterns = [
        r'https?://[^"\s<>]*(?:confirm|verif|activation|activate|email-verified|token)[^"\s<>]*',
        r'https?://[^\s<>"]+/users/confirmation\?[^\s<>"]+',
        r'https?://[^\s<>"]+confirmation_token=[^\s<>"&]+',
        r'https?://(?:www\.)?gorgias\.com/[^\s<>"]+token=[^\s<>"]+',
        r'https?://[a-z0-9-]+\.gorgias\.com/[^\s<>"]+',
    ]
    for p in patterns:
        m = re.search(p, email_body, re.IGNORECASE)
        if m:
            return m.group(0).rstrip('.')
    return None

def _try_signup(sess: http.Session, email: str, password: str,
                first: str, last: str, company: str, subdomain: str,
                lq, proxy: str | None = None) -> dict:
    """
    Try multiple Gorgias signup endpoints.
    Returns {"ok": True, "data": ...} or {"ok": False, "error": ...}
    """
    log(f"Attempting signup for {email} / subdomain={subdomain}", "info", lq)

    recaptcha_token = None
    if _captcha.get("key"):
        log("Solving reCAPTCHA v2...", "info", lq)
        recaptcha_token = solve_recaptcha_v2(
            GORGIAS_RECAPTCHA_SITEKEY, GORGIAS_SIGNUP_PAGE, proxy=proxy
        )
        if recaptcha_token:
            log("reCAPTCHA solved OK", "ok", lq)
        else:
            log("reCAPTCHA solve failed, trying without token", "warn", lq)

    payloads = [
        {
            "email": email,
            "password": password,
            "first_name": first,
            "last_name": last,
            "company": company,
            "subdomain": subdomain,
            **({
                "g-recaptcha-response": recaptcha_token} if recaptcha_token else {}),
        },
        {
            "email": email,
            "password": password,
            "firstName": first,
            "lastName": last,
            "companyName": company,
            "shopName": subdomain,
            **({
                "recaptchaToken": recaptcha_token} if recaptcha_token else {}),
        },
        {
            "user": {
                "email": email,
                "password": password,
                "first_name": first,
                "last_name": last,
            },
            "account": {
                "name": company,
                "subdomain": subdomain,
            },
            **({
                "recaptcha": recaptcha_token} if recaptcha_token else {}),
        },
    ]

    endpoints = [GORGIAS_SIGNUP_URL] + GORGIAS_SIGNUP_ALT

    for endpoint in endpoints:
        for payload in payloads:
            try:
                log(f"POST {endpoint}", "info", lq)
                r = sess.post(
                    endpoint,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=25,
                )
                log(f"  → {r.status_code}", "info", lq)
                if r.status_code in (200, 201, 202):
                    try:
                        data = r.json()
                    except Exception:
                        data = {"raw": r.text[:500]}
                    log(f"Signup success: {json.dumps(data)[:200]}", "ok", lq)
                    return {"ok": True, "data": data}
                elif r.status_code == 422:
                    try:
                        err = r.json()
                        err_str = str(err)
                    except Exception:
                        err_str = r.text[:300]
                    if "subdomain" in err_str.lower() and "taken" in err_str.lower():
                        return {"ok": False, "error": f"subdomain_taken:{subdomain}"}
                    if "email" in err_str.lower() and ("taken" in err_str.lower() or "exist" in err_str.lower()):
                        return {"ok": False, "error": f"email_taken:{email}"}
                    log(f"  Validation error: {err_str[:200]}", "warn", lq)
                elif r.status_code == 404:
                    log(f"  Endpoint not found, trying next...", "warn", lq)
                    break
                elif r.status_code == 400:
                    try:
                        log(f"  Bad request: {r.json()}", "warn", lq)
                    except Exception:
                        log(f"  Bad request: {r.text[:200]}", "warn", lq)
                elif r.status_code in (429, 503):
                    log(f"  Rate limited, waiting...", "warn", lq)
                    time.sleep(10)
            except http.exceptions.ConnectionError as e:
                log(f"  Connection error: {e}", "warn", lq)
                break
            except Exception as e:
                log(f"  Error: {e}", "warn", lq)

    return {"ok": False, "error": "All signup endpoints failed"}

def _gorgias_login(subdomain: str, email: str, password: str,
                   lq, proxy: str | None = None) -> http.Session | None:
    """Login to Gorgias and return authenticated session."""
    sess = _make_session(proxy)
    base = f"https://{subdomain}.gorgias.com"

    login_endpoints = [
        (f"{base}/users/sign_in", {"user": {"email": email, "password": password}}),
        (f"{base}/api/users/sign_in", {"email": email, "password": password}),
        (f"{base}/api/auth/login", {"email": email, "password": password}),
        (f"{base}/auth/login", {"email": email, "password": password}),
    ]

    for url, payload in login_endpoints:
        try:
            log(f"Login attempt: POST {url}", "info", lq)
            r = sess.post(url, json=payload, timeout=20)
            log(f"  → {r.status_code}", "info", lq)
            if r.status_code in (200, 201):
                try:
                    data = r.json()
                    log(f"Login OK: {str(data)[:150]}", "ok", lq)
                except Exception:
                    log("Login OK (non-JSON response)", "ok", lq)
                return sess
            elif r.status_code == 401:
                log("Invalid credentials", "error", lq)
                return None
        except Exception as e:
            log(f"Login error: {e}", "warn", lq)
            continue
    return None

def _create_api_key(sess: http.Session, subdomain: str, user_email: str,
                    lq) -> str | None:
    """Create a Gorgias API key for the account. Returns api_key or None."""
    base = f"https://{subdomain}.gorgias.com"
    key_name = f"autokey_{rand_str(6)}"

    endpoints = [
        f"{base}/api/account/access-tokens",
        f"{base}/api/users/me/access-tokens",
        f"{base}/api/access-tokens",
        f"{base}/api/integrations/api",
    ]

    payload_variants = [
        {"name": key_name},
        {"description": key_name, "scope": "full"},
        {"label": key_name},
    ]

    for url in endpoints:
        for payload in payload_variants:
            try:
                log(f"Creating API key: POST {url}", "info", lq)
                r = sess.post(url, json=payload, timeout=20)
                log(f"  → {r.status_code}", "info", lq)
                if r.status_code in (200, 201):
                    try:
                        data = r.json()
                        api_key = (
                            data.get("api_key") or
                            data.get("token") or
                            data.get("access_token") or
                            data.get("key") or
                            (data.get("data", {}) or {}).get("api_key") or
                            (data.get("data", {}) or {}).get("token")
                        )
                        if api_key:
                            log(f"API key created: {api_key[:12]}...", "ok", lq)
                            return api_key
                        log(f"Key response: {str(data)[:200]}", "warn", lq)
                    except Exception:
                        log(f"Key response (non-JSON): {r.text[:200]}", "warn", lq)
                elif r.status_code == 404:
                    break
            except Exception as e:
                log(f"Key create error: {e}", "warn", lq)
                continue
    return None

def _get_user_id(sess: http.Session, subdomain: str, lq) -> str | None:
    """Get current user ID."""
    base = f"https://{subdomain}.gorgias.com"
    for url in [f"{base}/api/users/me", f"{base}/api/account"]:
        try:
            r = sess.get(url, timeout=15)
            if r.status_code == 200:
                data = r.json()
                uid = data.get("id") or (data.get("user", {}) or {}).get("id")
                if uid:
                    return str(uid)
        except Exception:
            pass
    return None

# ── Main autoreg worker ────────────────────────────────────────────────────────

def _register_one(cfg: dict, lq: queue.Queue, proxy: str | None) -> dict:
    """Register a single Gorgias account. Returns result dict."""
    anymsg_key = cfg["anymsg_key"]

    first, last = rand_name()
    company = rand_company()
    subdomain = rand_subdomain(company)
    password = "P@ss" + rand_str(8) + "1!"

    log(f"━━ Starting account: {first} {last} / {company} / {subdomain}.gorgias.com", "info", lq)

    log("Ordering temporary email...", "info", lq)
    email_order = order_email(anymsg_key, proxy=proxy)
    if "error" in email_order:
        log(f"AnyMessage error: {email_order['error']}", "error", lq)
        return {"status": "failed", "reason": "email_order_failed"}

    temp_email = email_order.get("email")
    email_id   = email_order.get("id") or email_order.get("orderId")

    if not temp_email or not email_id:
        log(f"Bad email order response: {email_order}", "error", lq)
        return {"status": "failed", "reason": "email_order_bad_response"}

    log(f"Got temp email: {temp_email}", "ok", lq)

    sess = _make_session(proxy)
    result = _try_signup(sess, temp_email, password, first, last, company, subdomain, lq, proxy)

    if not result["ok"]:
        err = result.get("error", "")
        if "subdomain_taken" in err:
            log(f"Subdomain {subdomain} taken, generating new...", "warn", lq)
            subdomain = rand_subdomain(company)
            result = _try_signup(sess, temp_email, password, first, last, company, subdomain, lq, proxy)

        if not result["ok"]:
            log(f"Signup failed: {result.get('error')}", "error", lq)
            return {"status": "failed", "reason": result.get("error", "signup_failed")}

    log("Waiting for confirmation email (up to 120s)...", "info", lq)
    email_body = wait_for_email(anymsg_key, email_id, "confirm", timeout=120, proxy=proxy)

    if not email_body:
        log("No confirmation email received", "error", lq)
        return {
            "status": "unverified",
            "email": temp_email,
            "password": password,
            "subdomain": subdomain,
            "reason": "no_confirmation_email",
        }

    log("Confirmation email received!", "ok", lq)

    confirm_url = _extract_confirmation_link(email_body)
    if not confirm_url:
        log("Could not extract confirmation link from email body", "warn", lq)
        log(f"Email excerpt: {email_body[:300]}", "warn", lq)
    else:
        log(f"Clicking confirmation link: {confirm_url[:80]}...", "info", lq)
        try:
            rc = sess.get(confirm_url, timeout=20, allow_redirects=True)
            log(f"Confirmation response: {rc.status_code} → {rc.url[:80]}", "ok", lq)
        except Exception as e:
            log(f"Confirmation click error: {e}", "warn", lq)

    log("Logging in to get session...", "info", lq)
    auth_sess = _gorgias_login(subdomain, temp_email, password, lq, proxy)

    api_key = None
    if auth_sess:
        log("Login successful, creating API key...", "ok", lq)
        api_key = _create_api_key(auth_sess, subdomain, temp_email, lq)

    if not api_key:
        log("Could not auto-create API key. Account ready but API key must be created manually.", "warn", lq)

    account = {
        "email":     temp_email,
        "password":  password,
        "subdomain": subdomain,
        "api_key":   api_key or "",
        "first_name": first,
        "last_name":  last,
        "company":   company,
        "created_at": datetime.now().isoformat(),
        "status":    "active" if api_key else "no_api_key",
    }

    with _accounts_lock:
        _accounts.append(account)
        _save_accounts()

    log(f"✓ Account ready: {temp_email} @ {subdomain}.gorgias.com" +
        (f" | API key: {api_key[:12]}..." if api_key else " | (no api key)"), "ok", lq)

    return {"status": "ok", **account}

def _autoreg_worker(cfg: dict):
    global _st
    _st["running"] = True
    _st["stop_flag"] = False
    _st["done"] = 0
    _st["failed"] = 0
    _st["results"] = []
    _st["start_ts"] = time.time()

    count   = cfg["count"]
    workers = cfg.get("workers", 1)

    log(f"Starting autoreg: {count} accounts, {workers} workers", "info", _log_q)

    def _run_one(i):
        if _st["stop_flag"]:
            return None
        proxy = next_proxy()
        if proxy:
            log(f"[{i+1}] Using proxy: {proxy}", "info", _log_q)
        result = _register_one(cfg, _log_q, proxy)
        if result.get("status") == "ok":
            _st["done"] += 1
        else:
            _st["failed"] += 1
        _st["results"].append(result)
        return result

    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(_run_one, i) for i in range(count)]
            for f in as_completed(futures):
                if _st["stop_flag"]:
                    break
    else:
        for i in range(count):
            if _st["stop_flag"]:
                break
            _run_one(i)

    log(f"Autoreg done — ok:{_st['done']} failed:{_st['failed']}", "ok", _log_q)
    _st["running"] = False

# ── Gorgias email sending ──────────────────────────────────────────────────────

def gorgias_send_email(subdomain: str, api_email: str, api_key: str,
                       to_email: str, subject: str, body_html: str,
                       sender_name: str | None = None,
                       lq: queue.Queue | None = None) -> dict:
    """
    Send an outbound email via Gorgias tickets API.
    Returns {"ok": True, "ticket_id": ...} or {"ok": False, "error": ...}
    """
    if lq is None:
        lq = _log_q

    if not sender_name:
        sender_name = api_email.split("@")[0].replace(".", " ").title()

    sender_email = f"{subdomain}@{subdomain}.gorgias.com"
    base_url = f"https://{subdomain}.gorgias.com"
    auth = (api_email, api_key)

    body_text = re.sub(r'<[^>]+>', '', body_html).strip()

    payload = {
        "channel": "email",
        "via": "api",
        "subject": subject,
        "customer": {
            "email": to_email
        },
        "messages": [
            {
                "channel": "email",
                "via": "api",
                "from_agent": True,
                "body_html": body_html,
                "body_text": body_text,
                "sender": {
                    "email": sender_email,
                    "name": sender_name,
                },
                "receiver": {
                    "email": to_email,
                    "name": to_email.split("@")[0],
                },
                "source": {
                    "from": {
                        "address": sender_email,
                        "name": sender_name,
                    },
                    "to": [
                        {"address": to_email, "name": to_email.split("@")[0]}
                    ]
                }
            }
        ]
    }

    try:
        log(f"Sending email → {to_email} via {subdomain}.gorgias.com", "info", lq)
        r = http.post(
            f"{base_url}/api/tickets",
            auth=auth,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=25,
            verify=False,
        )
        log(f"  → {r.status_code}", "info", lq)

        if r.status_code in (200, 201):
            try:
                data = r.json()
                ticket_id = data.get("id") or data.get("ticket_id")
                log(f"✓ Email sent, ticket_id={ticket_id}", "ok", lq)
                return {"ok": True, "ticket_id": ticket_id, "data": data}
            except Exception:
                log(f"✓ Email sent (non-JSON response)", "ok", lq)
                return {"ok": True, "ticket_id": None}
        else:
            try:
                err = r.json()
            except Exception:
                err = r.text[:300]
            log(f"Send failed: {r.status_code} → {err}", "error", lq)
            return {"ok": False, "error": f"HTTP {r.status_code}: {err}", "status_code": r.status_code}

    except Exception as e:
        log(f"Send exception: {e}", "error", lq)
        return {"ok": False, "error": str(e)}

def gorgias_test_connection(subdomain: str, api_email: str, api_key: str) -> dict:
    """Test Gorgias API connection."""
    try:
        r = http.get(
            f"https://{subdomain}.gorgias.com/api/account",
            auth=(api_email, api_key),
            timeout=15,
            verify=False,
        )
        if r.status_code == 200:
            data = r.json()
            return {"ok": True, "account": data}
        elif r.status_code == 401:
            return {"ok": False, "error": "Invalid credentials"}
        else:
            return {"ok": False, "error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def _gorgiasmail_send_worker(cfg: dict):
    """Worker for single GorgiasMail send."""
    recipients = cfg["recipients"]
    subdomain  = cfg["subdomain"]
    api_email  = cfg["api_email"]
    api_key    = cfg["api_key"]
    subject    = cfg["subject"]
    body_html  = cfg["body_html"]
    sender_name = cfg.get("sender_name", "")
    delay      = float(cfg.get("delay", 1))

    ok_count = 0
    fail_count = 0

    for i, recip in enumerate(recipients):
        recip = recip.strip()
        if not recip:
            continue
        result = gorgias_send_email(
            subdomain, api_email, api_key,
            recip, subject, body_html,
            sender_name=sender_name or None,
            lq=_gm_q,
        )
        if result["ok"]:
            ok_count += 1
        else:
            fail_count += 1
        if i < len(recipients) - 1:
            time.sleep(delay)

    log(f"GorgiasMail done: sent={ok_count} failed={fail_count}", "ok", _gm_q)

# ── Campaign worker ────────────────────────────────────────────────────────────

def _campaign_worker(cfg: dict):
    global _camp

    recipients   = [r.strip() for r in cfg["recipients"] if r.strip()]
    subject      = cfg["subject"]
    body_html    = cfg["body_html"]
    per_account  = int(cfg.get("per_account", 10))
    delay        = float(cfg.get("delay", 2))
    rotate_after = int(cfg.get("rotate_after", per_account))
    sender_name  = cfg.get("sender_name", "")

    _camp["running"] = True
    _camp["stop"]    = False
    _camp["sent"]    = 0
    _camp["failed"]  = 0
    _camp["total"]   = len(recipients)

    log(f"Campaign start: {len(recipients)} recipients, {per_account}/account, delay={delay}s", "info", _camp_q)

    with _accounts_lock:
        accounts = [a for a in _accounts if a.get("api_key") and a.get("status") == "active"]

    if not accounts:
        log("No active accounts with API keys found!", "error", _camp_q)
        _camp["running"] = False
        return

    log(f"Available accounts: {len(accounts)}", "info", _camp_q)

    acc_idx   = 0
    sent_this = 0

    for i, recip in enumerate(recipients):
        if _camp["stop"]:
            log("Campaign stopped by user", "warn", _camp_q)
            break

        acc = accounts[acc_idx % len(accounts)]
        subdomain = acc["subdomain"]
        api_email = acc["email"]
        api_key   = acc["api_key"]

        log(f"[{i+1}/{len(recipients)}] Sending via {subdomain} → {recip}", "info", _camp_q)

        result = gorgias_send_email(
            subdomain, api_email, api_key,
            recip, subject, body_html,
            sender_name=sender_name or None,
            lq=_camp_q,
        )

        if result["ok"]:
            _camp["sent"] += 1
            sent_this += 1
        else:
            _camp["failed"] += 1
            err = result.get("error", "")
            if "401" in err or "Unauthorized" in err.lower() or "invalid" in err.lower():
                log(f"Account {subdomain} auth failed, rotating...", "warn", _camp_q)
                acc_idx += 1
                sent_this = 0
                continue

        if sent_this >= rotate_after:
            acc_idx += 1
            sent_this = 0
            log(f"Rotating to next account (used {rotate_after})", "info", _camp_q)

        if i < len(recipients) - 1:
            time.sleep(delay)

    log(f"Campaign done: sent={_camp['sent']} failed={_camp['failed']}", "ok", _camp_q)
    _camp["running"] = False

# ── Flask routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return HTML_PAGE

@app.route("/api/settings/captcha", methods=["GET"])
def get_captcha_settings():
    return jsonify(_captcha)

@app.route("/api/settings/captcha", methods=["POST"])
def save_captcha_settings():
    data = request.json or {}
    _captcha["service"] = data.get("service", _captcha["service"])
    _captcha["key"]     = data.get("key", _captcha["key"])
    try:
        with open(_CAPTCHA_FILE, "w") as f:
            json.dump({"service": _captcha["service"], "key": _captcha["key"]}, f, indent=2)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
    return jsonify({"ok": True})

@app.route("/api/proxies", methods=["POST"])
def set_proxies():
    global _proxy_list, _proxy_idx
    data  = request.json or {}
    lines = data.get("proxies", "").splitlines()
    _proxy_list = [p for line in lines for p in [parse_proxy_line(line)] if p]
    _proxy_idx  = 0
    return jsonify({"ok": True, "count": len(_proxy_list)})

@app.route("/api/proxies", methods=["GET"])
def get_proxies():
    return jsonify({"count": len(_proxy_list), "proxies": _proxy_list})

@app.route("/api/autoreg/start", methods=["POST"])
def autoreg_start():
    if _st["running"]:
        return jsonify({"ok": False, "error": "Already running"})
    cfg = request.json or {}
    if not cfg.get("anymsg_key"):
        return jsonify({"ok": False, "error": "AnyMessage API key required"})
    try:
        cfg["count"]   = int(cfg.get("count", 1))
        cfg["workers"] = max(1, min(int(cfg.get("workers", 1)), 10))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)})
    threading.Thread(target=_autoreg_worker, args=(cfg,), daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/autoreg/stop", methods=["POST"])
def autoreg_stop():
    _st["stop_flag"] = True
    return jsonify({"ok": True})

@app.route("/api/autoreg/status")
def autoreg_status():
    elapsed = round(time.time() - _st["start_ts"], 1) if _st["start_ts"] else 0
    return jsonify({
        "running": _st["running"],
        "done":    _st["done"],
        "failed":  _st["failed"],
        "total":   _st["done"] + _st["failed"],
        "elapsed": elapsed,
    })

@app.route("/api/autoreg/stream")
def autoreg_stream():
    def gen():
        while True:
            try:
                entry = _log_q.get(timeout=30)
                yield f"data: {json.dumps(entry)}\n\n"
            except queue.Empty:
                yield 'data: {"ping":1}\n\n'
    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/api/accounts", methods=["GET"])
def get_accounts():
    with _accounts_lock:
        return jsonify({"accounts": _accounts, "count": len(_accounts)})

@app.route("/api/accounts/export", methods=["GET"])
def export_accounts():
    with _accounts_lock:
        if not _accounts:
            return jsonify({"error": "No accounts"}), 404
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=["email","password","subdomain","api_key","first_name","last_name","company","created_at","status"])
        writer.writeheader()
        writer.writerows(_accounts)
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=gorgias_accounts.csv"})

@app.route("/api/accounts/import", methods=["POST"])
def import_accounts():
    data = request.json or {}
    rows = data.get("accounts", [])
    added = 0
    with _accounts_lock:
        existing_emails = {a["email"] for a in _accounts}
        for row in rows:
            if row.get("email") and row["email"] not in existing_emails:
                _accounts.append(row)
                existing_emails.add(row["email"])
                added += 1
        _save_accounts()
    return jsonify({"ok": True, "added": added})

@app.route("/api/accounts/<int:idx>", methods=["DELETE"])
def delete_account(idx):
    with _accounts_lock:
        if 0 <= idx < len(_accounts):
            removed = _accounts.pop(idx)
            _save_accounts()
            return jsonify({"ok": True, "removed": removed["email"]})
    return jsonify({"ok": False, "error": "Index out of range"}), 404

@app.route("/api/accounts/<int:idx>/update", methods=["PATCH"])
def update_account(idx):
    data = request.json or {}
    with _accounts_lock:
        if 0 <= idx < len(_accounts):
            _accounts[idx].update({k: v for k, v in data.items() if k in ("api_key", "status", "password")})
            _save_accounts()
            return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Index out of range"}), 404

@app.route("/api/gorgiasmail/test", methods=["POST"])
def gorgiasmail_test():
    data = request.json or {}
    subdomain = data.get("subdomain", "").strip()
    api_email  = data.get("api_email", "").strip()
    api_key    = data.get("api_key", "").strip()
    if not all([subdomain, api_email, api_key]):
        return jsonify({"ok": False, "error": "subdomain, api_email, and api_key required"})
    result = gorgias_test_connection(subdomain, api_email, api_key)
    return jsonify(result)

@app.route("/api/gorgiasmail/send", methods=["POST"])
def gorgiasmail_send():
    data = request.json or {}
    required = ["subdomain", "api_email", "api_key", "recipients", "subject", "body_html"]
    for f in required:
        if not data.get(f):
            return jsonify({"ok": False, "error": f"Missing: {f}"})
    recipients = [r.strip() for r in data["recipients"].splitlines() if r.strip()]
    if not recipients:
        return jsonify({"ok": False, "error": "No recipients"})
    cfg = {**data, "recipients": recipients}
    threading.Thread(target=_gorgiasmail_send_worker, args=(cfg,), daemon=True).start()
    return jsonify({"ok": True, "count": len(recipients)})

@app.route("/api/gorgiasmail/stream")
def gorgiasmail_stream():
    def gen():
        while True:
            try:
                entry = _gm_q.get(timeout=30)
                yield f"data: {json.dumps(entry)}\n\n"
            except queue.Empty:
                yield 'data: {"ping":1}\n\n'
    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/api/campaign/start", methods=["POST"])
def campaign_start():
    if _camp["running"]:
        return jsonify({"ok": False, "error": "Campaign already running"})
    data = request.json or {}
    recipients = [r.strip() for r in data.get("recipients", "").splitlines() if r.strip()]
    if not recipients:
        return jsonify({"ok": False, "error": "No recipients"})
    if not data.get("subject"):
        return jsonify({"ok": False, "error": "Subject required"})
    if not data.get("body_html"):
        return jsonify({"ok": False, "error": "Body required"})
    cfg = {**data, "recipients": recipients}
    threading.Thread(target=_campaign_worker, args=(cfg,), daemon=True).start()
    return jsonify({"ok": True, "total": len(recipients)})

@app.route("/api/campaign/stop", methods=["POST"])
def campaign_stop():
    _camp["stop"] = True
    return jsonify({"ok": True})

@app.route("/api/campaign/status")
def campaign_status():
    return jsonify({
        "running": _camp["running"],
        "sent":    _camp["sent"],
        "failed":  _camp["failed"],
        "total":   _camp["total"],
    })

@app.route("/api/campaign/stream")
def campaign_stream():
    def gen():
        while True:
            try:
                entry = _camp_q.get(timeout=30)
                yield f"data: {json.dumps(entry)}\n\n"
            except queue.Empty:
                yield 'data: {"ping":1}\n\n'
    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# ── HTML Template ──────────────────────────────────────────────────────────────
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Gorgias Suite</title>
<style>
  :root {
    --bg: #0d0f14;
    --bg2: #161820;
    --bg3: #1e2029;
    --border: #2a2d3a;
    --accent: #5865f2;
    --accent2: #7983f5;
    --green: #3ba55c;
    --red: #ed4245;
    --yellow: #faa61a;
    --text: #e2e5f0;
    --text2: #8b8fa8;
    --radius: 8px;
    --font: 'Segoe UI', system-ui, sans-serif;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: var(--font); font-size: 14px; min-height: 100vh; display: flex; }
  .sidebar {
    width: 220px; background: var(--bg2); border-right: 1px solid var(--border);
    display: flex; flex-direction: column; padding: 20px 0; flex-shrink: 0;
    position: fixed; height: 100vh; overflow-y: auto;
  }
  .sidebar-logo {
    padding: 0 20px 20px; border-bottom: 1px solid var(--border);
    font-size: 18px; font-weight: 700; color: var(--accent2);
    display: flex; align-items: center; gap: 10px;
  }
  .sidebar-logo svg { width: 28px; height: 28px; }
  .sidebar-nav { flex: 1; padding: 12px 0; }
  .nav-item {
    padding: 10px 20px; cursor: pointer; display: flex; align-items: center; gap: 10px;
    color: var(--text2); transition: all 0.15s; border-left: 3px solid transparent;
    font-size: 13.5px;
  }
  .nav-item:hover { background: var(--bg3); color: var(--text); }
  .nav-item.active { background: rgba(88,101,242,0.12); color: var(--accent2); border-left-color: var(--accent); }
  .nav-item svg { width: 18px; height: 18px; flex-shrink: 0; }
  .sidebar-footer { padding: 16px 20px; border-top: 1px solid var(--border); font-size: 12px; color: var(--text2); }
  .main { margin-left: 220px; flex: 1; padding: 28px; min-height: 100vh; }
  .tab-content { display: none; }
  .tab-content.active { display: block; }
  .card {
    background: var(--bg2); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 20px; margin-bottom: 16px;
  }
  .card-title { font-size: 15px; font-weight: 600; margin-bottom: 16px; color: var(--text); }
  .card-subtitle { font-size: 13px; color: var(--text2); margin-top: -10px; margin-bottom: 16px; }
  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px; }
  .form-group { margin-bottom: 14px; }
  .form-group label { display: block; margin-bottom: 6px; font-size: 12.5px; color: var(--text2); font-weight: 500; }
  input[type=text], input[type=number], input[type=email], input[type=password], textarea, select {
    width: 100%; background: var(--bg3); border: 1px solid var(--border); border-radius: 6px;
    padding: 9px 12px; color: var(--text); font-size: 13.5px; outline: none; transition: border 0.15s;
    font-family: var(--font);
  }
  input:focus, textarea:focus, select:focus { border-color: var(--accent); }
  textarea { resize: vertical; min-height: 80px; line-height: 1.5; }
  .input-row { display: flex; gap: 10px; }
  .input-row input, .input-row select { flex: 1; }
  .btn {
    padding: 9px 18px; border: none; border-radius: 6px; cursor: pointer;
    font-size: 13.5px; font-weight: 600; font-family: var(--font);
    transition: all 0.15s; display: inline-flex; align-items: center; gap: 7px;
  }
  .btn:disabled { opacity: 0.45; cursor: not-allowed; }
  .btn-primary { background: var(--accent); color: #fff; }
  .btn-primary:hover:not(:disabled) { background: var(--accent2); }
  .btn-success { background: var(--green); color: #fff; }
  .btn-success:hover:not(:disabled) { background: #45c76e; }
  .btn-danger { background: var(--red); color: #fff; }
  .btn-danger:hover:not(:disabled) { background: #f05658; }
  .btn-ghost { background: var(--bg3); color: var(--text); border: 1px solid var(--border); }
  .btn-ghost:hover:not(:disabled) { background: var(--border); }
  .btn-sm { padding: 5px 12px; font-size: 12px; }
  .btn-row { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 14px; align-items: center; }
  .stats-row { display: flex; gap: 12px; margin-bottom: 16px; }
  .stat-box {
    background: var(--bg3); border: 1px solid var(--border); border-radius: 6px;
    padding: 12px 16px; flex: 1; text-align: center;
  }
  .stat-value { font-size: 22px; font-weight: 700; line-height: 1; }
  .stat-label { font-size: 11px; color: var(--text2); margin-top: 4px; }
  .stat-green { color: var(--green); }
  .stat-red   { color: var(--red); }
  .stat-blue  { color: var(--accent2); }
  .stat-yellow { color: var(--yellow); }
  .log-terminal {
    background: #0a0b0e; border: 1px solid var(--border); border-radius: var(--radius);
    height: 320px; overflow-y: auto; padding: 12px; font-family: 'Consolas', 'Courier New', monospace;
    font-size: 12.5px; line-height: 1.6;
  }
  .log-entry { padding: 1px 0; }
  .log-ts   { color: #4a5068; margin-right: 6px; }
  .log-ok   { color: var(--green); }
  .log-error { color: var(--red); }
  .log-warn { color: var(--yellow); }
  .log-info { color: var(--text2); }
  .progress-bar-wrap { background: var(--bg3); border-radius: 4px; height: 8px; overflow: hidden; margin-bottom: 14px; }
  .progress-bar { background: var(--accent); height: 100%; border-radius: 4px; transition: width 0.3s; width: 0%; }
  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; padding: 10px 12px; background: var(--bg3); color: var(--text2); font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; border-bottom: 1px solid var(--border); }
  td { padding: 10px 12px; border-bottom: 1px solid rgba(42,45,58,0.5); vertical-align: middle; }
  tr:hover td { background: rgba(88,101,242,0.05); }
  .badge {
    display: inline-flex; align-items: center; padding: 2px 8px; border-radius: 12px;
    font-size: 11px; font-weight: 600;
  }
  .badge-green { background: rgba(59,165,92,0.18); color: var(--green); }
  .badge-yellow { background: rgba(250,166,26,0.18); color: var(--yellow); }
  .badge-red { background: rgba(237,66,69,0.18); color: var(--red); }
  .mono { font-family: 'Consolas', monospace; font-size: 12px; }
  .rich-toolbar {
    background: var(--bg3); border: 1px solid var(--border); border-bottom: none;
    border-radius: 6px 6px 0 0; padding: 6px 10px; display: flex; gap: 4px; flex-wrap: wrap;
  }
  .rich-toolbar button {
    background: none; border: none; color: var(--text2); cursor: pointer;
    padding: 3px 7px; border-radius: 4px; font-size: 13px; transition: all 0.1s;
  }
  .rich-toolbar button:hover { background: var(--border); color: var(--text); }
  .rich-toolbar .sep { width: 1px; background: var(--border); margin: 0 3px; }
  .editor-body {
    background: var(--bg3); border: 1px solid var(--border); border-radius: 0 0 6px 6px;
    min-height: 160px; padding: 12px; outline: none; color: var(--text);
    font-size: 13.5px; line-height: 1.6;
  }
  .editor-body:focus { border-color: var(--accent); }
  .inner-tabs { display: flex; gap: 0; border-bottom: 1px solid var(--border); margin-bottom: 18px; }
  .inner-tab { padding: 8px 16px; cursor: pointer; font-size: 13px; color: var(--text2); border-bottom: 2px solid transparent; margin-bottom: -1px; transition: all 0.15s; }
  .inner-tab.active { color: var(--accent2); border-bottom-color: var(--accent); }
  #toast {
    position: fixed; bottom: 24px; right: 24px; z-index: 9999;
    background: var(--bg3); border: 1px solid var(--border); border-radius: 8px;
    padding: 12px 18px; font-size: 13.5px; min-width: 220px;
    display: none; animation: slideIn 0.2s;
  }
  @keyframes slideIn { from { transform: translateY(12px); opacity: 0; } to { transform: none; opacity: 1; } }
  #toast.ok   { border-color: var(--green); color: var(--green); }
  #toast.error { border-color: var(--red); color: var(--red); }
  #toast.info  { border-color: var(--accent); color: var(--accent2); }
  .page-header { margin-bottom: 22px; }
  .page-header h1 { font-size: 22px; font-weight: 700; }
  .page-header p { color: var(--text2); font-size: 13px; margin-top: 4px; }
  @media (max-width: 900px) {
    .grid-2, .grid-3 { grid-template-columns: 1fr; }
    .sidebar { width: 60px; }
    .sidebar .nav-item span, .sidebar-logo span, .sidebar-footer { display: none; }
    .main { margin-left: 60px; }
  }
</style>
</head>
<body>
<nav class="sidebar">
  <div class="sidebar-logo">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <circle cx="12" cy="12" r="10"/><path d="M8 12h8M12 8v8"/>
    </svg>
    <span>Gorgias Suite</span>
  </div>
  <div class="sidebar-nav">
    <div class="nav-item active" onclick="showTab('autoreg')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
      <span>Autoreg</span>
    </div>
    <div class="nav-item" onclick="showTab('gorgiasmail')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>
      <span>GorgiasMail</span>
    </div>
    <div class="nav-item" onclick="showTab('campaign')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg>
      <span>Campaign</span>
    </div>
    <div class="nav-item" onclick="showTab('accounts')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
      <span>Accounts</span>
    </div>
    <div class="nav-item" onclick="showTab('settings')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
      <span>Settings</span>
    </div>
  </div>
  <div class="sidebar-footer">v1.0 • Gorgias Suite</div>
</nav>
<main class="main">
  <div class="tab-content active" id="tab-autoreg">
    <div class="page-header"><h1>Account Registration</h1><p>Auto-register Gorgias accounts via AnyMessage disposable emails</p></div>
    <div class="grid-2">
      <div>
        <div class="card">
          <div class="card-title">Registration Settings</div>
          <div class="form-group"><label>AnyMessage API Key</label><input type="text" id="ar-anymsg-key" placeholder="your-anymessage-api-key"></div>
          <div class="grid-2">
            <div class="form-group"><label>Number of Accounts</label><input type="number" id="ar-count" value="1" min="1" max="100"></div>
            <div class="form-group"><label>Workers (parallel)</label><input type="number" id="ar-workers" value="1" min="1" max="10"></div>
          </div>
          <div class="btn-row">
            <button class="btn btn-success" id="ar-start-btn" onclick="startAutoreg()">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg>
              Start
            </button>
            <button class="btn btn-danger" id="ar-stop-btn" onclick="stopAutoreg()" disabled>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>
              Stop
            </button>
          </div>
        </div>
        <div class="card">
          <div class="card-title">Live Stats</div>
          <div class="stats-row">
            <div class="stat-box"><div class="stat-value stat-green" id="ar-done">0</div><div class="stat-label">Success</div></div>
            <div class="stat-box"><div class="stat-value stat-red" id="ar-failed">0</div><div class="stat-label">Failed</div></div>
            <div class="stat-box"><div class="stat-value stat-blue" id="ar-elapsed">0s</div><div class="stat-label">Elapsed</div></div>
          </div>
          <div class="progress-bar-wrap"><div class="progress-bar" id="ar-progress"></div></div>
          <div style="font-size:12px;color:var(--text2)" id="ar-progress-label">Idle</div>
        </div>
      </div>
      <div>
        <div class="card" style="height: 480px; display: flex; flex-direction: column;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
            <span class="card-title" style="margin-bottom:0">Live Log</span>
            <button class="btn btn-ghost btn-sm" onclick="clearLog('ar-log')">Clear</button>
          </div>
          <div class="log-terminal" id="ar-log" style="flex:1;height:auto"></div>
        </div>
      </div>
    </div>
  </div>
  <div class="tab-content" id="tab-gorgiasmail">
    <div class="page-header"><h1>GorgiasMail</h1><p>Send outbound emails via a Gorgias account's API</p></div>
    <div class="grid-2">
      <div>
        <div class="card">
          <div class="card-title">Account Credentials</div>
          <div class="form-group"><label>Subdomain</label>
            <div class="input-row"><input type="text" id="gm-subdomain" placeholder="myshop"><span style="display:flex;align-items:center;color:var(--text2);white-space:nowrap;font-size:13px">.gorgias.com</span></div>
          </div>
          <div class="form-group"><label>Account Email</label><input type="email" id="gm-api-email" placeholder="you@example.com"></div>
          <div class="form-group"><label>API Key</label><input type="text" id="gm-api-key" placeholder="your-gorgias-api-key"></div>
          <div class="btn-row">
            <button class="btn btn-ghost" onclick="testGorgias()">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 11 12 14 22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>
              Test Connection
            </button>
            <button class="btn btn-ghost btn-sm" onclick="loadFromAccounts()">Load from Accounts</button>
          </div>
          <div id="gm-test-result" style="margin-top:10px;font-size:13px;display:none"></div>
        </div>
        <div class="card">
          <div class="card-title">Email Settings</div>
          <div class="form-group"><label>Sender Name (optional)</label><input type="text" id="gm-sender-name" placeholder="Support Team"></div>
          <div class="form-group"><label>Subject</label><input type="text" id="gm-subject" placeholder="Email subject..."></div>
          <div class="form-group"><label>Recipients (one per line)</label><textarea id="gm-recipients" rows="5" placeholder="email1@example.com&#10;email2@example.com"></textarea></div>
          <div class="form-group"><label>Delay between emails (seconds)</label><input type="number" id="gm-delay" value="1" min="0" step="0.5"></div>
          <div class="btn-row">
            <button class="btn btn-primary" onclick="sendGorgiasMail()">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
              Send
            </button>
          </div>
        </div>
      </div>
      <div>
        <div class="card">
          <div class="card-title">Email Body (HTML)</div>
          <div class="rich-toolbar">
            <button onclick="fmt('bold')" title="Bold"><b>B</b></button>
            <button onclick="fmt('italic')" title="Italic"><i>I</i></button>
            <button onclick="fmt('underline')" title="Underline"><u>U</u></button>
            <div class="sep"></div>
            <button onclick="fmtBlock('h2')" title="Heading">H</button>
            <button onclick="fmtBlock('p')" title="Paragraph">P</button>
            <div class="sep"></div>
            <button onclick="insertLink()" title="Link">&#128279;</button>
            <button onclick="fmt('insertUnorderedList')" title="List">&#8226;</button>
            <div class="sep"></div>
            <button onclick="toggleHtmlMode()" title="HTML" id="html-toggle">HTML</button>
          </div>
          <div id="gm-editor" class="editor-body" contenteditable="true" spellcheck="false">
            <p>Hello,</p><p>This is an automated message from our support team.</p><p>Please let us know if you have any questions.</p><br><p>Best regards,<br>The Support Team</p>
          </div>
          <textarea id="gm-html-source" style="display:none;height:200px" placeholder="<p>Your HTML here...</p>"></textarea>
        </div>
        <div class="card" style="height: 280px; display: flex; flex-direction: column;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
            <span class="card-title" style="margin-bottom:0">Send Log</span>
            <button class="btn btn-ghost btn-sm" onclick="clearLog('gm-log')">Clear</button>
          </div>
          <div class="log-terminal" id="gm-log" style="flex:1;height:auto"></div>
        </div>
      </div>
    </div>
  </div>
  <div class="tab-content" id="tab-campaign">
    <div class="page-header"><h1>Campaign</h1><p>Send bulk emails cycling through all registered accounts</p></div>
    <div class="grid-2">
      <div>
        <div class="card">
          <div class="card-title">Campaign Settings</div>
          <div class="form-group"><label>Sender Name (optional)</label><input type="text" id="cp-sender-name" placeholder="Support Team"></div>
          <div class="form-group"><label>Subject</label><input type="text" id="cp-subject" placeholder="Email subject..."></div>
          <div class="form-group"><label>Recipients (one per line)</label><textarea id="cp-recipients" rows="6" placeholder="email1@example.com&#10;email2@example.com&#10;..."></textarea></div>
          <div class="grid-2">
            <div class="form-group"><label>Emails per account</label><input type="number" id="cp-per-account" value="10" min="1"></div>
            <div class="form-group"><label>Delay (seconds)</label><input type="number" id="cp-delay" value="2" min="0" step="0.5"></div>
          </div>
          <div class="btn-row">
            <button class="btn btn-success" id="cp-start-btn" onclick="startCampaign()">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg>
              Launch Campaign
            </button>
            <button class="btn btn-danger" id="cp-stop-btn" onclick="stopCampaign()" disabled>Stop</button>
          </div>
        </div>
        <div class="card">
          <div class="card-title">Progress</div>
          <div class="stats-row">
            <div class="stat-box"><div class="stat-value stat-green" id="cp-sent">0</div><div class="stat-label">Sent</div></div>
            <div class="stat-box"><div class="stat-value stat-red" id="cp-failed">0</div><div class="stat-label">Failed</div></div>
            <div class="stat-box"><div class="stat-value stat-blue" id="cp-total">0</div><div class="stat-label">Total</div></div>
          </div>
          <div class="progress-bar-wrap"><div class="progress-bar" id="cp-progress"></div></div>
          <div style="font-size:12px;color:var(--text2)" id="cp-status">Idle</div>
        </div>
      </div>
      <div>
        <div class="card">
          <div class="card-title">Email Body (HTML)</div>
          <div class="rich-toolbar">
            <button onclick="fmt2('bold')"><b>B</b></button>
            <button onclick="fmt2('italic')"><i>I</i></button>
            <button onclick="fmt2('underline')"><u>U</u></button>
            <div class="sep"></div>
            <button onclick="fmtBlock2('h2')">H</button>
            <button onclick="fmtBlock2('p')">P</button>
            <div class="sep"></div>
            <button onclick="insertLink2()">&#128279;</button>
          </div>
          <div id="cp-editor" class="editor-body" contenteditable="true" spellcheck="false">
            <p>Hello,</p><p>This is a message from our team.</p><p>Best regards,<br>The Team</p>
          </div>
        </div>
        <div class="card" style="height: 260px; display: flex; flex-direction: column;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
            <span class="card-title" style="margin-bottom:0">Campaign Log</span>
            <button class="btn btn-ghost btn-sm" onclick="clearLog('cp-log')">Clear</button>
          </div>
          <div class="log-terminal" id="cp-log" style="flex:1;height:auto"></div>
        </div>
      </div>
    </div>
  </div>
  <div class="tab-content" id="tab-accounts">
    <div class="page-header"><h1>Registered Accounts</h1><p>Manage all Gorgias accounts</p></div>
    <div class="card" style="margin-bottom:16px">
      <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
        <button class="btn btn-primary" onclick="refreshAccounts()">Refresh</button>
        <button class="btn btn-ghost" onclick="exportAccounts()">Export CSV</button>
        <button class="btn btn-ghost" onclick="document.getElementById('acc-import-file').click()">Import CSV</button>
        <input type="file" id="acc-import-file" accept=".csv,.json" style="display:none" onchange="importAccounts(this)">
        <span id="acc-count" style="color:var(--text2);font-size:13px;margin-left:8px"></span>
        <div style="flex:1"></div>
        <input type="text" id="acc-search" placeholder="Search..." style="width:200px" oninput="filterAccounts()">
      </div>
    </div>
    <div class="card">
      <div class="table-wrap">
        <table>
          <thead><tr><th>#</th><th>Email</th><th>Subdomain</th><th>API Key</th><th>Status</th><th>Created</th><th>Actions</th></tr></thead>
          <tbody id="acc-table-body"><tr><td colspan="7" style="text-align:center;color:var(--text2);padding:30px">Loading...</td></tr></tbody>
        </table>
      </div>
    </div>
  </div>
  <div class="tab-content" id="tab-settings">
    <div class="page-header"><h1>Settings</h1><p>Configure captcha service, proxies, and AnyMessage</p></div>
    <div class="grid-2">
      <div>
        <div class="card">
          <div class="card-title">Captcha Service</div>
          <div class="form-group"><label>Service</label>
            <select id="cap-service">
              <option value="capsolver">CapSolver</option>
              <option value="2captcha">2Captcha</option>
              <option value="capmonster">CapMonster</option>
              <option value="rucaptcha">RuCaptcha</option>
            </select>
          </div>
          <div class="form-group"><label>API Key</label><input type="text" id="cap-key" placeholder="Your captcha service API key"></div>
          <div class="btn-row"><button class="btn btn-primary" onclick="saveCaptcha()">Save Captcha Settings</button></div>
        </div>
        <div class="card">
          <div class="card-title">Proxy Configuration</div>
          <div class="card-subtitle">Format: user:pass:host:port or host:port, one per line</div>
          <div class="form-group"><textarea id="proxy-list" rows="8" placeholder="user:pass:1.2.3.4:8080&#10;1.2.3.4:3128&#10;http://user:pass@1.2.3.4:8080"></textarea></div>
          <div class="btn-row">
            <button class="btn btn-primary" onclick="saveProxies()">Apply Proxies</button>
            <span id="proxy-count" style="color:var(--text2);font-size:13px"></span>
          </div>
        </div>
      </div>
      <div>
        <div class="card">
          <div class="card-title">AnyMessage Guide</div>
          <div style="color:var(--text2);font-size:13.5px;line-height:1.7">
            <p style="margin-bottom:10px">AnyMessage provides temporary email addresses for account verification.</p>
            <p style="margin-bottom:6px"><strong style="color:var(--text)">1.</strong> Get an API key from AnyMessage</p>
            <p style="margin-bottom:6px"><strong style="color:var(--text)">2.</strong> Enter it in the Autoreg tab</p>
            <p style="margin-bottom:6px"><strong style="color:var(--text)">3.</strong> The tool auto-orders temp emails per account</p>
            <p style="margin-bottom:16px"><strong style="color:var(--text)">4.</strong> Verification emails are polled automatically</p>
            <hr style="border-color:var(--border);margin-bottom:16px">
            <p style="margin-bottom:6px"><strong style="color:var(--text)">Gorgias API Key Creation:</strong></p>
            <p style="margin-bottom:6px">After account creation, if auto-key fails:</p>
            <p style="margin-bottom:6px">1. Login to <code style="background:var(--bg3);padding:1px 5px;border-radius:3px">{subdomain}.gorgias.com</code></p>
            <p style="margin-bottom:6px">2. Go to Settings &#8594; REST API</p>
            <p>3. Create a key and add it in the Accounts tab</p>
          </div>
        </div>
        <div class="card">
          <div class="card-title">Gorgias Rate Limits</div>
          <div style="color:var(--text2);font-size:13.5px;line-height:1.7">
            <p style="margin-bottom:8px">Standard limits:</p>
            <p style="margin-bottom:6px">&#8226; <strong style="color:var(--text)">40 requests / 20 seconds</strong> per account</p>
            <p style="margin-bottom:6px">&#8226; Free tier: limited daily outbound emails</p>
            <p style="margin-bottom:6px">&#8226; Use delay &#8805; 1s between sends</p>
            <p style="margin-bottom:6px">&#8226; Rotate accounts to avoid limits</p>
            <p style="margin-bottom:6px">&#8226; Campaign auto-rotates after N emails</p>
          </div>
        </div>
      </div>
    </div>
  </div>
</main>
<div id="toast"></div>
<script>
function showTab(name) {
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  const items = document.querySelectorAll('.nav-item');
  const tabMap = {autoreg:0, gorgiasmail:1, campaign:2, accounts:3, settings:4};
  items[tabMap[name]]?.classList.add('active');
  if (name === 'accounts') refreshAccounts();
  if (name === 'settings') loadSettings();
}
let _toastTimer;
function toast(msg, type='info') {
  const el = document.getElementById('toast');
  el.textContent = msg; el.className = type; el.style.display = 'block';
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { el.style.display = 'none'; }, 3500);
}
function appendLog(containerId, entry) {
  const el = document.getElementById(containerId);
  if (!el) return;
  const row = document.createElement('div');
  row.className = 'log-entry';
  const lvl = entry.level || 'info';
  row.innerHTML = `<span class="log-ts">${entry.ts||''}</span><span class="log-${lvl}">${escHtml(entry.msg||'')}</span>`;
  el.appendChild(row); el.scrollTop = el.scrollHeight;
  if (el.children.length > 2000) el.removeChild(el.firstChild);
}
function clearLog(id) { const el = document.getElementById(id); if (el) el.innerHTML = ''; }
function escHtml(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function startStream(url, logId, onData) {
  const es = new EventSource(url);
  es.onmessage = e => {
    try { const d = JSON.parse(e.data); if (d.ping) return; appendLog(logId, d); if (onData) onData(d); } catch {}
  };
  es.onerror = () => {};
  return es;
}
let _arStream;
function startAutoreg() {
  const key = document.getElementById('ar-anymsg-key').value.trim();
  if (!key) { toast('AnyMessage API key required', 'error'); return; }
  const count = parseInt(document.getElementById('ar-count').value) || 1;
  const workers = parseInt(document.getElementById('ar-workers').value) || 1;
  fetch('/api/autoreg/start', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({anymsg_key:key, count, workers})})
  .then(r=>r.json()).then(d => {
    if (!d.ok) { toast(d.error, 'error'); return; }
    toast('Autoreg started!', 'ok');
    document.getElementById('ar-start-btn').disabled = true;
    document.getElementById('ar-stop-btn').disabled = false;
    if (_arStream) _arStream.close();
    _arStream = startStream('/api/autoreg/stream', 'ar-log');
    _arPoll = setInterval(updateArStatus, 1500);
  });
}
function stopAutoreg() { fetch('/api/autoreg/stop', {method:'POST'}).then(()=>toast('Stop signal sent','info')); }
let _arPoll;
function updateArStatus() {
  fetch('/api/autoreg/status').then(r=>r.json()).then(d => {
    document.getElementById('ar-done').textContent = d.done;
    document.getElementById('ar-failed').textContent = d.failed;
    document.getElementById('ar-elapsed').textContent = d.elapsed + 's';
    const total = d.total || d.done + d.failed;
    const pct = total > 0 ? Math.round(d.done / total * 100) : 0;
    document.getElementById('ar-progress').style.width = pct + '%';
    document.getElementById('ar-progress-label').textContent =
      d.running ? `Running: ${d.done}/${total} (${pct}%)` : `Done: ${d.done} success, ${d.failed} failed`;
    if (!d.running) {
      document.getElementById('ar-start-btn').disabled = false;
      document.getElementById('ar-stop-btn').disabled = true;
      clearInterval(_arPoll);
    }
  });
}
let _gmStream;
let _htmlMode = false;
function testGorgias() {
  const subdomain = document.getElementById('gm-subdomain').value.trim();
  const api_email = document.getElementById('gm-api-email').value.trim();
  const api_key = document.getElementById('gm-api-key').value.trim();
  if (!subdomain||!api_email||!api_key) { toast('Fill in all credential fields','error'); return; }
  const resEl = document.getElementById('gm-test-result');
  resEl.style.display = 'block';
  resEl.innerHTML = '<span style="color:var(--text2)">Testing...</span>';
  fetch('/api/gorgiasmail/test', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({subdomain,api_email,api_key})})
  .then(r=>r.json()).then(d=>{
    if (d.ok) { const name = d.account?.name || subdomain; resEl.innerHTML = `<span style="color:var(--green)">&#10003; Connected: ${escHtml(name)}</span>`; toast('Connection OK','ok'); }
    else { resEl.innerHTML = `<span style="color:var(--red)">&#10007; ${escHtml(d.error)}</span>`; toast(d.error,'error'); }
  });
}
function loadFromAccounts() {
  fetch('/api/accounts').then(r=>r.json()).then(d=>{
    const active = d.accounts.filter(a=>a.api_key && a.status==='active');
    if (!active.length) { toast('No active accounts with API keys','error'); return; }
    const a = active[0];
    document.getElementById('gm-subdomain').value = a.subdomain;
    document.getElementById('gm-api-email').value = a.email;
    document.getElementById('gm-api-key').value = a.api_key;
    toast(`Loaded: ${a.email}`,'ok');
  });
}
function getEditorHtml() {
  if (_htmlMode) return document.getElementById('gm-html-source').value;
  return document.getElementById('gm-editor').innerHTML;
}
function sendGorgiasMail() {
  const subdomain = document.getElementById('gm-subdomain').value.trim();
  const api_email = document.getElementById('gm-api-email').value.trim();
  const api_key = document.getElementById('gm-api-key').value.trim();
  const recipients = document.getElementById('gm-recipients').value.trim();
  const subject = document.getElementById('gm-subject').value.trim();
  const body_html = getEditorHtml().trim();
  const sender_name = document.getElementById('gm-sender-name').value.trim();
  const delay = parseFloat(document.getElementById('gm-delay').value) || 1;
  if (!subdomain||!api_email||!api_key) { toast('Credentials required','error'); return; }
  if (!recipients) { toast('Recipients required','error'); return; }
  if (!subject) { toast('Subject required','error'); return; }
  if (!body_html) { toast('Email body required','error'); return; }
  if (_gmStream) _gmStream.close();
  _gmStream = startStream('/api/gorgiasmail/stream', 'gm-log');
  fetch('/api/gorgiasmail/send', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({subdomain,api_email,api_key,recipients,subject,body_html,sender_name,delay})})
  .then(r=>r.json()).then(d=>{ if (d.ok) toast(`Sending to ${d.count} recipients...`,'ok'); else toast(d.error,'error'); });
}
function fmt(cmd) { document.execCommand(cmd, false, null); }
function fmt2(cmd) { document.execCommand(cmd, false, null); }
function fmtBlock(tag) { document.execCommand('formatBlock', false, tag); }
function fmtBlock2(tag) { document.execCommand('formatBlock', false, tag); }
function insertLink() { const url = prompt('URL:'); if (url) document.execCommand('createLink', false, url); }
function insertLink2() { const url = prompt('URL:'); if (url) document.execCommand('createLink', false, url); }
function toggleHtmlMode() {
  _htmlMode = !_htmlMode;
  const editor = document.getElementById('gm-editor');
  const source = document.getElementById('gm-html-source');
  const btn = document.getElementById('html-toggle');
  if (_htmlMode) { source.value = editor.innerHTML; editor.style.display = 'none'; source.style.display = 'block'; btn.style.background = 'var(--accent)'; btn.style.color = '#fff'; }
  else { editor.innerHTML = source.value; source.style.display = 'none'; editor.style.display = 'block'; btn.style.background = ''; btn.style.color = ''; }
}
let _cpStream, _cpPoll;
function startCampaign() {
  const recipients = document.getElementById('cp-recipients').value.trim();
  const subject = document.getElementById('cp-subject').value.trim();
  const body_html = document.getElementById('cp-editor').innerHTML.trim();
  const per_account = parseInt(document.getElementById('cp-per-account').value) || 10;
  const delay = parseFloat(document.getElementById('cp-delay').value) || 2;
  const sender_name = document.getElementById('cp-sender-name').value.trim();
  const rotate_after = per_account;
  if (!recipients) { toast('Recipients required','error'); return; }
  if (!subject) { toast('Subject required','error'); return; }
  if (!body_html) { toast('Email body required','error'); return; }
  if (_cpStream) _cpStream.close();
  _cpStream = startStream('/api/campaign/stream', 'cp-log');
  fetch('/api/campaign/start', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({recipients,subject,body_html,per_account,delay,rotate_after,sender_name})})
  .then(r=>r.json()).then(d=>{
    if (d.ok) {
      toast(`Campaign started: ${d.total} recipients`,'ok');
      document.getElementById('cp-start-btn').disabled = true;
      document.getElementById('cp-stop-btn').disabled = false;
      document.getElementById('cp-total').textContent = d.total;
      _cpPoll = setInterval(updateCpStatus, 1500);
    } else { toast(d.error,'error'); }
  });
}
function stopCampaign() { fetch('/api/campaign/stop', {method:'POST'}).then(()=>toast('Campaign stopping...','info')); }
function updateCpStatus() {
  fetch('/api/campaign/status').then(r=>r.json()).then(d=>{
    document.getElementById('cp-sent').textContent = d.sent;
    document.getElementById('cp-failed').textContent = d.failed;
    document.getElementById('cp-total').textContent = d.total;
    const pct = d.total > 0 ? Math.round((d.sent+d.failed)/d.total*100) : 0;
    document.getElementById('cp-progress').style.width = pct + '%';
    document.getElementById('cp-status').textContent =
      d.running ? `Running: ${d.sent+d.failed}/${d.total} (${pct}%)` : `Done: sent=${d.sent} failed=${d.failed}`;
    if (!d.running) { document.getElementById('cp-start-btn').disabled = false; document.getElementById('cp-stop-btn').disabled = true; clearInterval(_cpPoll); }
  });
}
let _allAccounts = [];
function refreshAccounts() {
  fetch('/api/accounts').then(r=>r.json()).then(d=>{
    _allAccounts = d.accounts || [];
    document.getElementById('acc-count').textContent = `${_allAccounts.length} account${_allAccounts.length===1?'':'s'}`;
    filterAccounts();
  });
}
function filterAccounts() {
  const q = (document.getElementById('acc-search')?.value||'').toLowerCase();
  const filtered = _allAccounts.filter(a => !q || a.email?.toLowerCase().includes(q) || a.subdomain?.toLowerCase().includes(q));
  renderAccountsTable(filtered);
}
function renderAccountsTable(accounts) {
  const tbody = document.getElementById('acc-table-body');
  if (!accounts.length) { tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--text2);padding:30px">No accounts found</td></tr>'; return; }
  tbody.innerHTML = accounts.map((a, i) => {
    const idx = _allAccounts.indexOf(a);
    const statusBadge = {active:'<span class="badge badge-green">active</span>',no_api_key:'<span class="badge badge-yellow">no api key</span>',unverified:'<span class="badge badge-red">unverified</span>',failed:'<span class="badge badge-red">failed</span>'}[a.status] || `<span class="badge">${escHtml(a.status||'?')}</span>`;
    const apiKeyDisplay = a.api_key ? `<span class="mono" title="${escHtml(a.api_key)}">${escHtml(a.api_key.slice(0,12))}...</span>` : '<span style="color:var(--text2)">&#8212;</span>';
    const created = a.created_at ? new Date(a.created_at).toLocaleDateString() : '&#8212;';
    return `<tr><td style="color:var(--text2)">${i+1}</td><td class="mono">${escHtml(a.email||'')}</td><td><a href="https://${escHtml(a.subdomain)}.gorgias.com" target="_blank" style="color:var(--accent2);text-decoration:none">${escHtml(a.subdomain||'')}</a></td><td>${apiKeyDisplay}</td><td>${statusBadge}</td><td style="color:var(--text2)">${created}</td><td><div style="display:flex;gap:6px"><button class="btn btn-ghost btn-sm" onclick='editApiKey(${idx})'>Key</button><button class="btn btn-ghost btn-sm" onclick='useAccount(${idx})'>Use</button><button class="btn btn-danger btn-sm" onclick='deleteAccount(${idx})'>Del</button></div></td></tr>`;
  }).join('');
}
function editApiKey(idx) {
  const a = _allAccounts[idx];
  const key = prompt(`API key for ${a.email}:`, a.api_key||'');
  if (key === null) return;
  fetch(`/api/accounts/${idx}/update`, {method:'PATCH', headers:{'Content-Type':'application/json'}, body: JSON.stringify({api_key:key, status:key?'active':'no_api_key'})})
  .then(r=>r.json()).then(d=>{ if (d.ok) { toast('API key updated','ok'); refreshAccounts(); } else toast(d.error,'error'); });
}
function useAccount(idx) {
  const a = _allAccounts[idx];
  document.getElementById('gm-subdomain').value = a.subdomain;
  document.getElementById('gm-api-email').value = a.email;
  document.getElementById('gm-api-key').value = a.api_key || '';
  showTab('gorgiasmail');
  toast(`Loaded: ${a.email}`,'ok');
}
function deleteAccount(idx) {
  const a = _allAccounts[idx];
  if (!confirm(`Delete ${a.email}?`)) return;
  fetch(`/api/accounts/${idx}`, {method:'DELETE'}).then(r=>r.json()).then(d=>{ if (d.ok) { toast('Deleted','ok'); refreshAccounts(); } else toast(d.error,'error'); });
}
function exportAccounts() { window.location = '/api/accounts/export'; }
function importAccounts(input) {
  const file = input.files[0]; if (!file) return;
  const reader = new FileReader();
  reader.onload = e => {
    const text = e.target.result;
    try {
      const data = JSON.parse(text);
      const accounts = Array.isArray(data) ? data : (data.accounts || []);
      fetch('/api/accounts/import', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({accounts})})
      .then(r=>r.json()).then(d=>{ toast(`Imported ${d.added} accounts`,'ok'); refreshAccounts(); });
    } catch {
      const lines = text.split('\n').filter(l=>l.trim());
      const headers = lines[0].split(',').map(h=>h.trim().replace(/^"|"$/g,''));
      const accounts = lines.slice(1).map(line => {
        const vals = line.split(',').map(v=>v.trim().replace(/^"|"$/g,''));
        return Object.fromEntries(headers.map((h,i)=>[h,vals[i]||'']));
      }).filter(a=>a.email);
      fetch('/api/accounts/import', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({accounts})})
      .then(r=>r.json()).then(d=>{ toast(`Imported ${d.added} accounts`,'ok'); refreshAccounts(); });
    }
  };
  reader.readAsText(file);
  input.value = '';
}
function loadSettings() {
  fetch('/api/settings/captcha').then(r=>r.json()).then(d=>{
    document.getElementById('cap-service').value = d.service || 'capsolver';
    document.getElementById('cap-key').value = d.key || '';
  });
  fetch('/api/proxies').then(r=>r.json()).then(d=>{
    document.getElementById('proxy-count').textContent = d.count ? `${d.count} proxies loaded` : '';
  });
}
function saveCaptcha() {
  const service = document.getElementById('cap-service').value;
  const key = document.getElementById('cap-key').value.trim();
  fetch('/api/settings/captcha', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({service,key})})
  .then(r=>r.json()).then(d=>{ if (d.ok) toast('Captcha settings saved','ok'); else toast(d.error,'error'); });
}
function saveProxies() {
  const proxies = document.getElementById('proxy-list').value;
  fetch('/api/proxies', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({proxies})})
  .then(r=>r.json()).then(d=>{ if (d.ok) { document.getElementById('proxy-count').textContent = `${d.count} proxies loaded`; toast(`${d.count} proxies applied`,'ok'); } else toast(d.error,'error'); });
}
window.addEventListener('DOMContentLoaded', () => { loadSettings(); });
</script>
</body>
</html>
"""

if __name__ == "__main__":
    print("Gorgias Suite → http://localhost:5758")
    app.run(host="0.0.0.0", port=5758, debug=False, threaded=True)
