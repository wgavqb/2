"""
TB Suite — Unified ThingsBoard Autoreg + ThingsMail
Run:  python server.py
Open: http://localhost:5757
"""
import threading, queue, time, re, csv, io, json, random, string, warnings
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, Response, request, jsonify, stream_with_context
import requests as http, urllib.request, urllib.error
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
SITE = "thingsboard.cloud"
TB   = "https://thingsboard.cloud"

# ── shared state ──────────────────────────────────────────────────────────────
_st = {
    "running": False, "stop_flag": False,
    "results": [],    "done": 0, "failed": 0, "start_ts": None,
}
_log_q: queue.Queue = queue.Queue()
_tm_store = {}   # jwt for ThingsMail

# ── proxy state ───────────────────────────────────────────────────────────────
_proxy_list: list = []   # list of "http://..." strings
_proxy_idx  = 0
_proxy_lock = threading.Lock()

def parse_proxy_line(line: str) -> str | None:
    """Convert user:pass:host:port or host:port to http://... URL."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    # already a full URL
    if line.startswith("http://") or line.startswith("https://"):
        return line
    parts = line.split(":")
    if len(parts) == 4:       # user:pass:host:port
        u, p, h, port = parts
        return f"http://{u}:{p}@{h}:{port}"
    elif len(parts) == 2:     # host:port
        return f"http://{parts[0]}:{parts[1]}"
    return None

def next_proxy() -> str | None:
    """Round-robin proxy selection (thread-safe)."""
    global _proxy_idx
    if not _proxy_list:
        return None
    with _proxy_lock:
        idx = _proxy_idx % len(_proxy_list)
        _proxy_idx += 1
    return _proxy_list[idx]

# ── bulk setup state ──────────────────────────────────────────────────────────
_bulk = {"running": False, "done": 0, "failed": 0, "total": 0, "results": []}
_bulk_q: queue.Queue = queue.Queue()

# ── campaign state ────────────────────────────────────────────────────────────
_camp = {
    "running": False, "stop": False,
    "acc_done": 0, "acc_fail": 0, "total": 0,
    "emails_sent": 0, "recip_idx": 0,
}
_camp_q: queue.Queue = queue.Queue()

# ── captcha state ─────────────────────────────────────────────────────────────
import os as _os
_CAPTCHA_FILE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "captcha.json")
_captcha = {"service": "capsolver", "key": "", "sitekey": "", "version": "v3", "action": "signup"}
try:
    with open(_CAPTCHA_FILE, "r") as _f:
        _saved = json.load(_f)
        _captcha["service"] = _saved.get("service", _captcha["service"])
        _captcha["key"] = _saved.get("key", "")
except Exception:
    pass
_TB_SIGNUP_URL = "https://thingsboard.cloud/signup"

def _get_tb_recaptcha_params(sess) -> dict:
    """Fetch reCAPTCHA params from ThingsBoard API (sitekey, version, action)."""
    if _captcha["sitekey"]:
        return {"sitekey": _captcha["sitekey"], "version": _captcha["version"],
                "action": _captcha["action"]}
    for url in [
        f"{TB}/api/noauth/selfRegistration/signUpSelfRegistrationParams",
        f"{TB}/api/noauth/signup/recaptchaParams",
    ]:
        try:
            r = sess.get(url, timeout=15)
            if r.status_code != 200:
                continue
            d = r.json()
            _captcha["sitekey"]  = d.get("captchaSiteKey", "") or d.get("siteKey", "")
            _captcha["version"]  = d.get("captchaVersion", "v3") or d.get("version", "v3")
            _captcha["action"]   = d.get("captchaAction", "signup") or d.get("actionName", "signup")
            if _captcha["sitekey"]:
                return {"sitekey": _captcha["sitekey"], "version": _captcha["version"],
                        "action": _captcha["action"]}
        except Exception:
            continue
    return {"sitekey": "", "version": "v3", "action": "signup"}

def _solve_recaptcha(sitekey: str, proxy_url: str | None = None,
                     log_fn=None) -> str | None:
    """Submit reCAPTCHA to solving service, return token or None."""
    def _log(msg, lv="info"):
        if log_fn: log_fn(msg, lv)
        else: push(msg, lv)

    key = _captcha["key"]
    svc = _captcha["service"]
    if not key or not sitekey:
        _log("Капча: ключ или sitekey не заданы", "warning")
        return None

    cap_version = _captcha.get("version", "v3")
    cap_action  = _captcha.get("action", "signup")

    # ── CapSolver (JSON API) ─────────────────────────────────────────────
    if svc == "capsolver":
        from urllib.parse import urlparse as _up
        if cap_version == "v3":
            task: dict = {"type": "ReCaptchaV3TaskProxyLess",
                          "websiteURL": _TB_SIGNUP_URL, "websiteKey": sitekey,
                          "pageAction": cap_action}
        else:
            task = {"type": "ReCaptchaV2TaskProxyLess",
                    "websiteURL": _TB_SIGNUP_URL, "websiteKey": sitekey}
        if proxy_url:
            try:
                p = _up(proxy_url)
                ptype = "ReCaptchaV3Task" if cap_version == "v3" else "ReCaptchaV2Task"
                task.update({"type": ptype,
                             "proxyType": "http",
                             "proxyAddress": p.hostname,
                             "proxyPort": p.port or 80,
                             "proxyLogin": p.username or "",
                             "proxyPassword": p.password or ""})
            except Exception:
                pass
        try:
            _log(f"CapSolver: отправка задачи…")
            r = http.post("https://api.capsolver.com/createTask",
                          json={"clientKey": key, "task": task}, timeout=30, verify=False)
            d = r.json()
            if d.get("errorId"):
                _log(f"CapSolver error {d.get('errorId')}: {d.get('errorDescription','?')}", "error")
                return None
            task_id = d.get("taskId")
            if not task_id:
                _log(f"CapSolver: нет taskId в ответе: {str(d)[:120]}", "error")
                return None
            _log(f"CapSolver: задача #{task_id} создана, ожидание…")
            for attempt in range(40):
                time.sleep(5)
                r2 = http.post("https://api.capsolver.com/getTaskResult",
                               json={"clientKey": key, "taskId": task_id},
                               timeout=15, verify=False)
                d2 = r2.json()
                if d2.get("errorId"):
                    _log(f"CapSolver poll error: {d2.get('errorDescription','?')}", "error")
                    return None
                if d2.get("status") == "ready":
                    _log(f"CapSolver: капча решена за {(attempt+1)*5}с", "success")
                    return d2["solution"]["gRecaptchaResponse"]
                if attempt % 3 == 2:
                    _log(f"CapSolver: ожидание {(attempt+1)*5}с…", "muted")
        except Exception as e:
            _log(f"CapSolver исключение: {e}", "error")
            return None
        _log("CapSolver: таймаут (200с)", "error")
        return None

    base_urls = {
        "2captcha":    "https://2captcha.com",
        "capmonster":  "https://api.capmonster.cloud",
        "anticaptcha": "https://api.anti-captcha.com",
        "rucaptcha":   "https://rucaptcha.com",
    }

    # ── capmonster uses different JSON API ──────────────────────────────
    if svc == "capmonster":
        from urllib.parse import urlparse as _up
        if cap_version == "v3":
            task2: dict = {"type": "RecaptchaV3TaskProxyless",
                           "websiteURL": _TB_SIGNUP_URL, "websiteKey": sitekey,
                           "pageAction": cap_action}
        else:
            task2 = {"type": "NoCaptchaTaskProxyless",
                     "websiteURL": _TB_SIGNUP_URL, "websiteKey": sitekey}
        if proxy_url:
            try:
                p = _up(proxy_url)
                ptype = "RecaptchaV3Task" if cap_version == "v3" else "NoCaptchaTask"
                task2.update({"type": ptype,
                              "proxyType": "http",
                              "proxyAddress": p.hostname,
                              "proxyPort": p.port or 80,
                              "proxyLogin": p.username or "",
                              "proxyPassword": p.password or ""})
            except Exception:
                pass
        try:
            _log("CapMonster: отправка задачи…")
            r = http.post(f"{base_urls['capmonster']}/createTask",
                          json={"clientKey": key, "task": task2}, timeout=30, verify=False)
            d = r.json()
            if d.get("errorId"):
                _log(f"CapMonster error: {d.get('errorDescription','?')}", "error")
                return None
            task_id = d.get("taskId")
            _log(f"CapMonster: задача #{task_id} создана, ожидание…")
            for attempt in range(40):
                time.sleep(5)
                r2 = http.post(f"{base_urls['capmonster']}/getTaskResult",
                               json={"clientKey": key, "taskId": task_id},
                               timeout=15, verify=False)
                d2 = r2.json()
                if d2.get("status") == "ready":
                    _log(f"CapMonster: решена за {(attempt+1)*5}с", "success")
                    return d2["solution"]["gRecaptchaResponse"]
                if attempt % 3 == 2:
                    _log(f"CapMonster: ожидание {(attempt+1)*5}с…", "muted")
        except Exception as e:
            _log(f"CapMonster исключение: {e}", "error")
            return None
        _log("CapMonster: таймаут", "error")
        return None

    # ── 2captcha-compatible API (2captcha, rucaptcha) ─────────────────
    base = base_urls.get(svc, base_urls["2captcha"])
    try:
        params = {"key": key, "method": "userrecaptcha",
                  "googlekey": sitekey, "pageurl": _TB_SIGNUP_URL,
                  "json": 1, "invisible": 0}
        if proxy_url:
            params["proxy"] = proxy_url.replace("http://","").replace("https://","")
            params["proxytype"] = "HTTP"
        _log(f"{svc}: отправка задачи…")
        r = http.get(f"{base}/in.php", params=params, timeout=30, verify=False)
        d = r.json()
        if d.get("status") != 1:
            _log(f"{svc} error: {d.get('request','?')}", "error")
            return None
        task_id = d["request"]
        _log(f"{svc}: задача #{task_id} создана, ожидание…")
        for attempt in range(40):
            time.sleep(5)
            r2 = http.get(f"{base}/res.php",
                          params={"key": key, "action": "get", "id": task_id, "json": 1},
                          timeout=15, verify=False)
            d2 = r2.json()
            if d2.get("status") == 1:
                _log(f"{svc}: решена за {(attempt+1)*5}с", "success")
                return d2["request"]
            if d2.get("request") not in ("CAPCHA_NOT_READY", "CAPTCHA_NOT_READY"):
                _log(f"{svc} error: {d2.get('request','?')}", "error")
                return None
            if attempt % 3 == 2:
                _log(f"{svc}: ожидание {(attempt+1)*5}с…", "muted")
    except Exception as e:
        _log(f"{svc} исключение: {e}", "error")
        return None
    _log(f"{svc}: таймаут", "error")
    return None

BUILD_EMAIL_JS = """
var email = {};
email.to      = '' + msg.email_to;
email.subject = '' + msg.email_subject;
email.body    = '' + msg.email_body;
return {msg: email, metadata: metadata, msgType: 'SEND_EMAIL'};
""".strip()

# ── helpers ───────────────────────────────────────────────────────────────────
FIRST = ["Alex","Sam","Jordan","Taylor","Morgan","Casey","Riley","Dakota","Quinn",
         "Blake","Avery","Skyler","Jamie","Reese","Drew","Logan","Peyton","Cameron",
         "River","Finley","Emery","Harley","Rowan","Sage","Shiloh"]
LAST  = ["Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis",
         "Wilson","Moore","Anderson","Taylor","Thomas","Jackson","White","Harris",
         "Martin","Thompson","Young","Walker","Allen","King","Wright","Scott"]

def rand_name(): return random.choice(FIRST), random.choice(LAST)

def rand_password(n=14):
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    p = (random.choice(string.ascii_uppercase) + random.choice(string.ascii_lowercase)
         + random.choice(string.digits) + random.choice("!@#$%^&*")
         + "".join(random.choices(chars, k=n - 4)))
    lst = list(p); random.shuffle(lst); return "".join(lst)

def extract_token(html):
    for pat in [r'emailCode=([A-Za-z0-9_\-\.]+)',
                r'activateToken=([A-Za-z0-9_\-\.]+)',
                r'/activate\?activateToken=([A-Za-z0-9_\-\.]+)',
                r'/activateEmail\?emailCode=([A-Za-z0-9_\-\.]+)',
                r'/emailVerified\?emailCode=([A-Za-z0-9_\-\.]+)']:
        m = re.search(pat, html)
        if m: return m.group(1)
    return None

def push(msg, level="info"):
    _log_q.put({"msg": msg, "level": level, "ts": datetime.now().strftime("%H:%M:%S")})

# ── single-account worker (runs in thread pool) ───────────────────────────────
def _make_session(proxy_url: str | None) -> http.Session:
    s = http.Session()
    s.verify = False
    if proxy_url:
        s.proxies = {"http": proxy_url, "https": proxy_url}
    return s

def register_one(slot: int, token: str, domain: str, proxy_url: str | None) -> dict:
    """Returns dict with 'ok', 'rec' or 'error'."""
    tag = f"[T{slot}]"
    p_tag = f" proxy={proxy_url.split('@')[-1] if proxy_url and '@' in proxy_url else (proxy_url or 'нет')}"

    def log(msg, level="info"):
        push(f"{tag} {msg}", level)

    sess = _make_session(proxy_url)
    log(f"Заказ почты [{domain}]{p_tag}…")

    # 1. Order email (AnyMessage — через прокси если есть)
    try:
        r = sess.get("https://api.anymessage.shop/email/order",
                     params={"token": token, "site": SITE, "domain": domain}, timeout=30)
        d = r.json()
    except Exception as e:
        log(f"Ошибка запроса: {e}", "error"); return {"ok": False, "error": str(e)}

    if d.get("status") != "success":
        err = d.get("value", "?")
        log(f"AnyMessage: {err}", "error"); return {"ok": False, "error": err}

    email, oid = d["email"], d["id"]
    log(f"Почта: {email}", "success")

    # 2. Generate creds
    first, last = rand_name(); pwd = rand_password()
    log(f"Имя: {first} {last} | Пароль: {pwd}")

    # 3. Solve reCAPTCHA (if service configured)
    recap_token = ""
    if _captcha["key"]:
        rp = _get_tb_recaptcha_params(sess)
        sitekey = rp["sitekey"]
        if sitekey:
            log(f"Решение капчи [{_captcha['service']}] ({rp['version']})…")
            recap_token = _solve_recaptcha(sitekey, proxy_url, log_fn=log) or ""
            if recap_token:
                log("Капча решена", "success")
            else:
                log("Капча не решена — продолжаем без токена", "warning")
        else:
            log("SiteKey не найден — пропуск капчи", "warning")

    # 4. ThingsBoard signup (через прокси)
    log("Регистрация…")
    payload = {
        "fields": {
            "FIRST_NAME": first,
            "LAST_NAME": last,
            "EMAIL": email,
            "PASSWORD": pwd,
        },
        "recaptchaResponse": recap_token or "",
    }
    try:
        r2 = sess.post(f"{TB}/api/noauth/signup",
                       json=payload,
                       headers={"Content-Type": "application/json",
                                "Accept": "application/json",
                                "Origin": "https://thingsboard.cloud",
                                "Referer": "https://thingsboard.cloud/signup"},
                       timeout=30)
        resp_body = r2.text[:400].strip()
        if r2.status_code not in (200, 201):
            log(f"TB HTTP {r2.status_code}: {resp_body}", "error")
            return {"ok": False, "error": f"TB {r2.status_code}: {resp_body}"}
        log(f"TB {r2.status_code}: {resp_body[:150]}", "success")
    except Exception as e:
        log(f"TB signup: {e}", "error"); return {"ok": False, "error": str(e)}

    # 4. Poll email
    log("Ожидание письма…")
    act_token = None
    poll_sess = _make_session(None)   # AnyMessage polling без прокси (быстрее)
    for attempt in range(1, 31):
        if _st["stop_flag"]:
            _cancel_email(token, oid); return {"ok": False, "error": "stopped"}
        try:
            r3 = poll_sess.get("https://api.anymessage.shop/email/getmessage",
                               params={"token": token, "id": oid}, timeout=30)
            d3 = r3.json()
            if d3.get("status") == "success":
                body = d3.get("message", "") or d3.get("value", "")
                act_token = extract_token(body)
                if act_token: break
                v = d3.get("value", "")
                if v and v != "wait message": act_token = v; break
            elif d3.get("value") != "wait message":
                log(f"getmessage: {d3.get('value')}", "warning")
        except Exception as e:
            log(f"Poll: {e}", "warning")
        log(f"  попытка {attempt}/30…", "muted")
        time.sleep(10)

    if not act_token:
        log("Таймаут письма", "error")
        _cancel_email(token, oid); return {"ok": False, "error": "timeout"}

    log("Токен получен", "success")

    # 5. Activate via POST activateByEmailCode (emailCode as query param)
    log(f"Активация (emailCode={act_token[:20]}...)…")
    ok = False
    for activate_url in [
        f"{TB}/api/noauth/activateByEmailCode",
        f"{TB}/api/noauth/cloud/activateByEmailCode",
    ]:
        try:
            r4 = sess.post(activate_url,
                           params={"emailCode": act_token},
                           timeout=30, verify=False)
            ok = r4.status_code in (200, 201)
            if ok:
                log(f"Activate {r4.status_code} OK", "success")
                break
            elif r4.status_code == 404:
                log(f"Activate {activate_url} -> 404, пробуем другой…", "muted")
                continue
            else:
                log(f"Activate {r4.status_code}: {r4.text[:200]}", "error")
                break
        except Exception as e:
            log(f"Activation: {e}", "error"); return {"ok": False, "error": str(e)}

    if not ok:
        log("Ошибка активации", "error"); return {"ok": False, "error": "activate failed"}

    # 6. Verify login + accept privacy policy / terms if needed
    log("Проверка логина…")
    try:
        r5 = sess.post(f"{TB}/api/auth/login",
                       json={"username": email, "password": pwd},
                       headers={"Content-Type": "application/json"},
                       timeout=30, verify=False)
        if r5.status_code == 200:
            log("Логин OK", "success")
            jwt = r5.json().get("token", "")
            if jwt:
                auth_h = {"Content-Type": "application/json",
                          "X-Authorization": f"Bearer {jwt}"}
                for accept_url in [f"{TB}/api/signup/acceptPrivacyPolicy",
                                   f"{TB}/api/signup/acceptTermsOfUse"]:
                    try:
                        sess.post(accept_url, headers=auth_h, timeout=10, verify=False)
                    except Exception:
                        pass
        else:
            log(f"Логин {r5.status_code}: {r5.json().get('message','')}", "warning")
    except Exception as e:
        log(f"Логин проверка: {e}", "warning")

    log("Активирован!", "success")
    rec = {"email": email, "password": pwd, "first": first, "last": last,
           "proxy": proxy_url or "",
           "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    return {"ok": True, "rec": rec}


def _cancel_email(token, oid):
    try: http.get("https://api.anymessage.shop/email/cancel",
                  params={"token": token, "id": oid}, timeout=15)
    except: pass


# ── multi-threaded orchestrator ───────────────────────────────────────────────
def run_registration(token, domain, count, delay, threads):
    global _proxy_idx
    _proxy_idx = 0   # reset rotation each run

    _st["running"] = True; _st["stop_flag"] = False
    _st["results"] = []; _st["done"] = 0; _st["failed"] = 0
    _st["start_ts"] = time.time()

    proxy_info = f" · прокси={len(_proxy_list)} шт." if _proxy_list else " · без прокси"
    push(f"Старт: {count} акк. · домен={domain} · "
         f"потоки={threads} · задержка={delay}с{proxy_info}", "section")

    remaining = list(range(1, count + 1))
    slot      = 0

    with ThreadPoolExecutor(max_workers=threads) as pool:
        futures = {}

        def submit_next():
            nonlocal slot
            if not remaining or _st["stop_flag"]: return
            i = remaining.pop(0); slot += 1
            proxy = next_proxy()
            push(f"── Аккаунт {i}/{count} (поток {slot % threads or threads}) ──", "section")
            fut = pool.submit(register_one, slot % threads or threads, token, domain, proxy)
            futures[fut] = i

        # seed initial batch
        for _ in range(min(threads, count)):
            submit_next()

        while futures:
            done_futures = [f for f in futures if f.done()]
            for fut in done_futures:
                idx = futures.pop(fut)
                result = fut.result()
                if result["ok"]:
                    _st["done"] += 1
                    _st["results"].append(result["rec"])
                else:
                    _st["failed"] += 1
                # submit next if any remain and not stopped
                if remaining and not _st["stop_flag"]:
                    if delay > 0: time.sleep(delay)
                    submit_next()
            if futures:
                time.sleep(0.5)

    elapsed = int(time.time() - _st["start_ts"])
    if _st["stop_flag"]:
        push("Остановлено пользователем", "warning")
    push(f"Готово. Создано: {_st['done']}  Ошибок: {_st['failed']}  "
         f"Времени: {elapsed}с", "section")
    _st["running"] = False
    push("__DONE__", "system")


# ── ThingsMail helpers ────────────────────────────────────────────────────────
def _make_opener(proxy_url=None):
    s = http.Session()
    s.verify = False
    if proxy_url:
        s.proxies = {"http": proxy_url, "https": proxy_url}
    return s

def _tb_post(path, data, opener):
    r = opener.post(f"{TB}{path}", json=data,
                    headers={"Content-Type": "application/json"}, timeout=15)
    r.raise_for_status()
    return r.json()

def _tb_post_jwt(path, data, jwt, opener):
    r = opener.post(f"{TB}{path}", json=data,
                    headers={"Content-Type": "application/json",
                             "X-Authorization": f"Bearer {jwt}"}, timeout=15)
    r.raise_for_status()
    return r.json()

def _tb_get(path, jwt, opener):
    r = opener.get(f"{TB}{path}",
                   headers={"X-Authorization": f"Bearer {jwt}"}, timeout=15)
    r.raise_for_status()
    return r.json()

def _setup_rule_chains(jwt, opener):
    log = []
    chains = _tb_get("/api/ruleChains?pageSize=100&page=0", jwt, opener)
    root_chain = email_chain = None
    for c in chains.get("data", []):
        if c.get("root"): root_chain = c
        if c["name"] in ("Email Sender", "1"): email_chain = c

    if not email_chain:
        email_chain = _tb_post_jwt("/api/ruleChain", {"name": "Email Sender", "debugMode": False}, jwt, opener)
        log.append('Создана Rule Chain "Email Sender"')
    else:
        log.append(f'Найдена Rule Chain "{email_chain["name"]}"')

    chain_id = email_chain["id"]
    meta = {
        "ruleChainId": chain_id, "firstNodeIndex": 0,
        "nodes": [
            {"type": "org.thingsboard.rule.engine.transform.TbTransformMsgNode",
             "name": "Build Email", "debugMode": True, "singletonMode": False,
             "configurationVersion": 0,
             "configuration": {"scriptLang": "JS", "jsScript": BUILD_EMAIL_JS, "tbelScript": None},
             "additionalInfo": {"layoutX": 300, "layoutY": 200}},
            {"type": "org.thingsboard.rule.engine.mail.TbSendEmailNode",
             "name": "Send Email", "debugMode": True, "singletonMode": False,
             "configurationVersion": 0,
             "configuration": {"useSystemSmtpSettings": True},
             "additionalInfo": {"layoutX": 700, "layoutY": 200}},
        ],
        "connections": [{"fromIndex": 0, "toIndex": 1, "type": "Success"}],
    }
    _tb_post_jwt("/api/ruleChain/metadata", meta, jwt, opener)
    log.append("Настроены узлы: Build Email → Send Email")

    if root_chain:
        root_meta = _tb_get(f'/api/ruleChain/{root_chain["id"]["id"]}/metadata', jwt, opener)
        mts_idx = rc_idx = None
        for i, node in enumerate(root_meta.get("nodes", [])):
            if "MsgTypeSwitchNode" in node.get("type", ""): mts_idx = i
            if "RuleChainInputNode" in node.get("type", ""):
                if node.get("configuration", {}).get("ruleChainId") == chain_id["id"]: rc_idx = i

        if rc_idx is None:
            root_meta["nodes"].append({
                "type": "org.thingsboard.rule.engine.flow.TbRuleChainInputNode",
                "name": email_chain["name"], "debugMode": False, "singletonMode": False,
                "configurationVersion": 0,
                "configuration": {"ruleChainId": chain_id["id"]},
                "additionalInfo": {"layoutX": 500, "layoutY": 700},
            })
            rc_idx = len(root_meta["nodes"]) - 1
            log.append(f'Добавлен узел "{email_chain["name"]}" в Root Rule Chain')

        conn_exists = any(
            c["fromIndex"] == mts_idx and c["toIndex"] == rc_idx and c["type"] == "Post telemetry"
            for c in root_meta.get("connections", [])
        ) if mts_idx is not None else True

        if not conn_exists and mts_idx is not None:
            root_meta["connections"].append({"fromIndex": mts_idx, "toIndex": rc_idx, "type": "Post telemetry"})
            log.append("Связь: Message Type Switch → Post telemetry → Email chain")

        _tb_post_jwt("/api/ruleChain/metadata", root_meta, jwt, opener)
        log.append("Root Rule Chain обновлён")

    return {"status": "ok", "log": log}


# ── API routes — Autoreg ──────────────────────────────────────────────────────
@app.route("/api/balance")
def api_balance():
    try:
        r = http.get("https://api.anymessage.shop/user/balance",
                     params={"token": request.args.get("token","")}, timeout=15)
        return jsonify(r.json())
    except Exception as e: return jsonify({"status":"error","value":str(e)})

@app.route("/api/quantity")
def api_quantity():
    try:
        r = http.get("https://api.anymessage.shop/email/quantity",
                     params={"token": request.args.get("token",""), "site": SITE}, timeout=15)
        return jsonify(r.json())
    except Exception as e: return jsonify({"status":"error","value":str(e)})

@app.route("/api/start", methods=["POST"])
def api_start():
    if _st["running"]: return jsonify({"ok": False, "error": "Already running"})
    d = request.json or {}
    token = d.get("token","")
    if not token: return jsonify({"ok": False, "error": "No token"})
    threading.Thread(
        target=run_registration,
        args=(token, d.get("domain","gmx"), int(d.get("count",1)),
              float(d.get("delay",5)), int(d.get("threads",2))),
        daemon=True,
    ).start()
    return jsonify({"ok": True})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    _st["stop_flag"] = True; return jsonify({"ok": True})

@app.route("/api/status")
def api_status():
    return jsonify({
        "running": _st["running"], "done": _st["done"],
        "failed": _st["failed"], "total": len(_st["results"]),
        "elapsed": int(time.time() - _st["start_ts"]) if _st["start_ts"] else 0,
    })

@app.route("/api/results")
def api_results(): return jsonify(_st["results"])

@app.route("/api/proxies", methods=["POST"])
def api_set_proxies():
    global _proxy_list, _proxy_idx
    raw = (request.json or {}).get("proxies", "")
    parsed = [p for line in raw.splitlines() if (p := parse_proxy_line(line))]
    _proxy_list = parsed
    _proxy_idx  = 0
    return jsonify({"ok": True, "count": len(parsed)})

@app.route("/api/proxies")
def api_get_proxies():
    return jsonify({"count": len(_proxy_list)})

@app.route("/api/captcha", methods=["POST"])
def api_set_captcha():
    d = request.json or {}
    if "service" in d: _captcha["service"] = d["service"]
    if "key" in d:     _captcha["key"] = d["key"].strip()
    _captcha["sitekey"] = ""
    try:
        with open(_CAPTCHA_FILE, "w") as f:
            json.dump({"service": _captcha["service"], "key": _captcha["key"]}, f)
    except Exception:
        pass
    return jsonify({"ok": True, "service": _captcha["service"],
                    "key_set": bool(_captcha["key"])})

@app.route("/api/captcha")
def api_get_captcha():
    return jsonify({"service": _captcha["service"], "key_set": bool(_captcha["key"]),
                    "key": _captcha["key"]})

@app.route("/api/export")
def api_export():
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=["email","password","first","last","proxy","ts"])
    w.writeheader(); w.writerows(_st["results"])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             f"attachment; filename=thingsboard_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"})

@app.route("/api/log")
def api_log():
    def generate():
        while True:
            try: yield f"data: {json.dumps(_log_q.get(timeout=30))}\n\n"
            except queue.Empty: yield ": ping\n\n"
    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


# ── API routes — ThingsMail ───────────────────────────────────────────────────
@app.route("/connect", methods=["POST"])
def tm_connect():
    b = request.json or {}
    opener = _make_opener(b.get("proxy"))
    try:
        jwt = _tb_post("/api/auth/login",
                       {"username": b["email"], "password": b["password"]}, opener)["token"]
        devs = _tb_get("/api/tenant/devices?pageSize=1&page=0&sortProperty=createdTime&sortOrder=DESC",
                       jwt, opener)
        if not devs.get("data"):
            dev = _tb_post_jwt("/api/device", {"name": "ThingsMail Gateway", "type": "default"}, jwt, opener)
            dev_id = dev["id"]["id"]
        else:
            dev_id = devs["data"][0]["id"]["id"]
        creds = _tb_get(f"/api/device/{dev_id}/credentials", jwt, opener)
        _tm_store["jwt"] = jwt
        return jsonify({"deviceToken": creds["credentialsId"]})
    except http.exceptions.HTTPError as e:
        return jsonify({"error": f"TB {e.response.status_code}: {e.response.text[:200]}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/send", methods=["POST"])
def tm_send():
    b = request.json or {}
    opener = _make_opener(b.get("proxy"))
    token, recipients = b["deviceToken"], b["recipients"]
    sent, errors = 0, []
    for r in recipients:
        try:
            resp = opener.post(
                f"{TB}/api/v1/{token}/telemetry",
                json={"email_to": r["to"], "email_subject": r["subject"],
                      "email_body": r["body"]},
                headers={"Content-Type": "application/json"}, timeout=10)
            resp.raise_for_status(); sent += 1
        except Exception as e:
            errors.append(f'{r["to"]}: {e}')
    return jsonify({"sent": sent, "errors": errors})

@app.route("/setup-rules", methods=["POST"])
def tm_setup():
    jwt = _tm_store.get("jwt")
    if not jwt: return jsonify({"error": "Сначала подключись"}), 400
    opener = _make_opener((request.json or {}).get("proxy"))
    try:
        return jsonify(_setup_rule_chains(jwt, opener))
    except http.exceptions.HTTPError as e:
        return jsonify({"error": f"TB {e.response.status_code}: {e.response.text[:200]}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


def _bulk_push(msg, level="info"):
    _bulk_q.put({"msg": msg, "level": level, "ts": datetime.now().strftime("%H:%M:%S")})


def _run_bulk_setup(accounts, proxy):
    _bulk["running"] = True
    _bulk["done"] = 0
    _bulk["failed"] = 0
    _bulk["total"] = len(accounts)
    _bulk["results"] = []
    proxy_info = f" · прокси={len(_proxy_list)} шт." if _proxy_list else ""
    _bulk_push(f"Применение Rule Chain на {len(accounts)} аккаунт(ов){proxy_info}…", "section")

    for i, acc in enumerate(accounts, 1):
        email, password = acc["email"], acc["password"]
        # rotate through proxy list; fallback to ThingsMail proxy field
        bulk_proxy = next_proxy() or proxy
        p_tag = f" [{bulk_proxy.split('@')[-1] if bulk_proxy and '@' in bulk_proxy else (bulk_proxy or 'нет')}]"
        _bulk_push(f"[{i}/{len(accounts)}] {email}{p_tag}")
        opener = _make_opener(bulk_proxy)
        try:
            jwt = _tb_post("/api/auth/login",
                           {"username": email, "password": password}, opener)["token"]
        except Exception as e:
            _bulk_push(f"  ✗ Логин: {e}", "error")
            _bulk["failed"] += 1
            _bulk["results"].append({"email": email, "ok": False, "error": str(e)})
            continue

        try:
            devs = _tb_get(
                "/api/tenant/devices?pageSize=1&page=0&sortProperty=createdTime&sortOrder=DESC",
                jwt, opener)
            if not devs.get("data"):
                dev = _tb_post_jwt("/api/device",
                                   {"name": "ThingsMail Gateway", "type": "default"},
                                   jwt, opener)
                dev_id = dev["id"]["id"]
            else:
                dev_id = devs["data"][0]["id"]["id"]
            _tb_get(f"/api/device/{dev_id}/credentials", jwt, opener)
        except Exception as e:
            _bulk_push(f"  ✗ Устройство: {e}", "error")
            _bulk["failed"] += 1
            _bulk["results"].append({"email": email, "ok": False, "error": str(e)})
            continue

        try:
            res = _setup_rule_chains(jwt, opener)
            _bulk_push(f"  ✓ Rule Chain настроен", "success")
            _bulk["done"] += 1
            _bulk["results"].append({"email": email, "ok": True, "log": res.get("log", [])})
        except Exception as e:
            _bulk_push(f"  ✗ Rule Chain: {e}", "error")
            _bulk["failed"] += 1
            _bulk["results"].append({"email": email, "ok": False, "error": str(e)})

        time.sleep(0.5)  # small pause between accounts

    _bulk_push(f"Готово. Успешно: {_bulk['done']}  Ошибок: {_bulk['failed']}", "section")
    _bulk["running"] = False
    _bulk_q.put({"msg": "__BULK_DONE__", "level": "system",
                 "ts": datetime.now().strftime("%H:%M:%S")})


@app.route("/setup-rules-all", methods=["POST"])
def tm_setup_all():
    if _bulk["running"]:
        return jsonify({"error": "Уже запущено"}), 400
    accounts = _st.get("results", [])
    if not accounts:
        return jsonify({"error": "Нет зарегистрированных аккаунтов"}), 400
    proxy = (request.json or {}).get("proxy")
    threading.Thread(target=_run_bulk_setup, args=(accounts, proxy), daemon=True).start()
    return jsonify({"ok": True, "total": len(accounts)})


@app.route("/setup-rules-all/status")
def tm_setup_all_status():
    return jsonify({
        "running": _bulk["running"],
        "done": _bulk["done"],
        "failed": _bulk["failed"],
        "total": _bulk["total"],
        "results": _bulk["results"],
    })


@app.route("/setup-rules-all/log")
def tm_setup_all_log():
    def generate():
        while True:
            try:
                item = _bulk_q.get(timeout=30)
                yield f"data: {json.dumps(item)}\n\n"
            except queue.Empty:
                yield ": ping\n\n"
    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── Campaign: use account → send N emails → delete → next ────────────────────
def _camp_push(msg, level="info"):
    _camp_q.put({"msg": msg, "level": level, "ts": datetime.now().strftime("%H:%M:%S")})

def _gen_id():
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))

def _tb_delete_self(jwt, opener):
    """Attempt to delete the current tenant account."""
    try:
        user = _tb_get("/api/auth/user", jwt, opener)
        tenant_id = user.get("tenantId", {}).get("id")
        if not tenant_id:
            return False, "tenant id not found"
        r = opener.delete(f"{TB}/api/tenant/{tenant_id}",
                          headers={"X-Authorization": f"Bearer {jwt}"}, timeout=10)
        r.raise_for_status()
        return True, "deleted"
    except http.exceptions.HTTPError as e:
        return False, f"HTTP {e.response.status_code if e.response else '?'}"
    except Exception as e:
        return False, str(e)

def _run_campaign(recipients, subject, body, emails_per_account, proxy):
    _camp["running"] = True; _camp["stop"] = False
    _camp["acc_done"] = 0; _camp["acc_fail"] = 0
    _camp["emails_sent"] = 0; _camp["recip_idx"] = 0
    accounts = list(_st["results"])   # snapshot copy
    _camp["total"] = len(accounts)

    needed = (len(recipients) + emails_per_account - 1) // emails_per_account if recipients else 0
    use_accounts = accounts[:needed] if needed else accounts
    _camp["total"] = len(use_accounts)

    _camp_push(
        f"Кампания: {len(use_accounts)} акк. (из {len(accounts)}) · "
        f"{emails_per_account} писем/акк. · {len(recipients)} получателей", "section"
    )

    for i, acc in enumerate(use_accounts, 1):
        if _camp["stop"]:
            _camp_push("Остановлено", "warning"); break

        email, password = acc["email"], acc["password"]
        acct_proxy = next_proxy() or proxy
        p_short = acct_proxy.split("@")[-1] if acct_proxy and "@" in acct_proxy else (acct_proxy or "нет")

        chunk_start = (i - 1) * emails_per_account
        chunk_end = chunk_start + emails_per_account
        chunk = recipients[chunk_start:chunk_end]
        if not chunk:
            _camp_push(f"── Аккаунт {i} · пропуск (нет получателей) ──", "muted")
            continue

        _camp_push(f"── Аккаунт {i}/{len(use_accounts)} · {email} · "
                   f"{len(chunk)} получателей · proxy={p_short} ──", "section")
        opener = _make_opener(acct_proxy)

        # 1. Login
        try:
            jwt = _tb_post("/api/auth/login",
                           {"username": email, "password": password}, opener)["token"]
        except Exception as e:
            _camp_push(f"✗ Логин: {e}", "error"); _camp["acc_fail"] += 1; continue

        # 2. Get / create device
        try:
            devs = _tb_get(
                "/api/tenant/devices?pageSize=1&page=0&sortProperty=createdTime&sortOrder=DESC",
                jwt, opener)
            if not devs.get("data"):
                dev = _tb_post_jwt("/api/device",
                                   {"name": "ThingsMail Gateway", "type": "default"}, jwt, opener)
                dev_id = dev["id"]["id"]
            else:
                dev_id = devs["data"][0]["id"]["id"]
            creds = _tb_get(f"/api/device/{dev_id}/credentials", jwt, opener)
            device_token = creds["credentialsId"]
        except Exception as e:
            _camp_push(f"✗ Устройство: {e}", "error"); _camp["acc_fail"] += 1; continue

        # 3. Setup Rule Chain
        try:
            _setup_rule_chains(jwt, opener)
            _camp_push("✓ Rule Chain настроен", "success")
        except Exception as e:
            _camp_push(f"✗ Rule Chain: {e}", "error"); _camp["acc_fail"] += 1; continue

        # 4. Send to this account's chunk of recipients
        sent = 0
        for to in chunk:
            if _camp["stop"]: break
            uid = _gen_id()
            try:
                r_tel = opener.post(
                    f"{TB}/api/v1/{device_token}/telemetry",
                    json={
                        "email_to":      to,
                        "email_subject": subject.replace("{id}", uid),
                        "email_body":    body.replace("{id}", uid),
                    },
                    headers={"Content-Type": "application/json"},
                    timeout=10,
                )
                r_tel.raise_for_status()
                sent += 1; _camp["emails_sent"] += 1
                _camp_push(f"  -> {to}", "success")
                time.sleep(0.3)
            except Exception as e:
                _camp_push(f"✗ Отправка -> {to}: {e}", "error")

        _camp_push(f"✓ Отправлено {sent}/{len(chunk)} писем", "success")

        # 5. Delete account
        ok, reason = _tb_delete_self(jwt, opener)
        if ok:
            _camp_push("✓ Аккаунт удалён", "success")
        else:
            _camp_push(f"⚠ Удаление: {reason} (удалён из списка)", "warning")

        # 6. Remove from local results regardless
        _st["results"] = [r for r in _st["results"] if r["email"] != email]
        _camp["acc_done"] += 1

        time.sleep(1)

    _camp_push(
        f"Кампания завершена. Аккаунтов: {_camp['acc_done']}  "
        f"Писем отправлено: {_camp['emails_sent']}  Ошибок: {_camp['acc_fail']}",
        "section"
    )
    _camp["running"] = False
    _camp_q.put({"msg": "__CAMP_DONE__", "level": "system",
                 "ts": datetime.now().strftime("%H:%M:%S")})


@app.route("/campaign/start", methods=["POST"])
def camp_start():
    if _camp["running"]: return jsonify({"error": "Уже запущено"}), 400
    accounts = _st.get("results", [])
    if not accounts: return jsonify({"error": "Нет аккаунтов"}), 400
    d = request.json or {}
    recipients = [e.strip() for e in re.split(r"[\n,;]+", d.get("recipients","")) if "@" in e.strip()]
    if not recipients: return jsonify({"error": "Нет получателей"}), 400
    subject = d.get("subject","").strip() or "Сообщение"
    body    = d.get("body","").strip()    or "Привет!"
    per_acc = max(1, int(d.get("per_account", 10)))
    proxy   = d.get("proxy")
    threading.Thread(target=_run_campaign,
                     args=(recipients, subject, body, per_acc, proxy), daemon=True).start()
    return jsonify({"ok": True, "total": len(accounts), "recipients": len(recipients)})

@app.route("/campaign/stop", methods=["POST"])
def camp_stop():
    _camp["stop"] = True; return jsonify({"ok": True})

@app.route("/campaign/status")
def camp_status():
    return jsonify({**_camp,
                    "accounts_left": len(_st.get("results", []))})

@app.route("/campaign/log")
def camp_log():
    def generate():
        while True:
            try: yield f"data: {json.dumps(_camp_q.get(timeout=30))}\n\n"
            except queue.Empty: yield ": ping\n\n"
    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


# ── HTML ──────────────────────────────────────────────────────────────────────
@app.route("/")
def index(): return HTML

# ─── full page HTML ────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TB Suite</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#06080F; --s1:#0D1220; --s2:#131B2E; --s3:#1A2540; --s4:#1F2D4A;
  --border:rgba(255,255,255,0.07); --border2:rgba(255,255,255,0.12);
  --accent:#00E5BE; --accent-dim:#009E84; --accent2:#6366F1;
  --ok:#22C55E; --err:#EF4444; --warn:#F59E0B;
  --text:#E2E8F0; --dim:#94A3B8; --muted:#64748B;
  --glass:rgba(255,255,255,0.03); --shadow:0 8px 32px rgba(0,0,0,.6);
}
html,body{height:100%;overflow:hidden;background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;font-size:14px}
body::before{
  content:'';position:fixed;inset:0;
  background-image:linear-gradient(rgba(0,229,190,0.025) 1px,transparent 1px),
    linear-gradient(90deg,rgba(0,229,190,0.025) 1px,transparent 1px);
  background-size:44px 44px;animation:gridmove 25s linear infinite;
  pointer-events:none;z-index:0;
}
@keyframes gridmove{to{background-position:44px 44px}}
body::after{
  content:'';position:fixed;width:700px;height:700px;border-radius:50%;
  background:radial-gradient(circle,rgba(99,102,241,.06) 0%,transparent 70%);
  top:-300px;right:-200px;pointer-events:none;z-index:0;
}
.shell{display:flex;height:100vh;position:relative;z-index:1}

/* ── sidebar ── */
.sidebar{
  width:300px;min-width:300px;background:var(--s1);
  border-right:1px solid var(--border);
  display:flex;flex-direction:column;overflow-y:auto;overflow-x:hidden;
}
.sidebar::-webkit-scrollbar{width:3px}
.sidebar::-webkit-scrollbar-thumb{background:var(--s3);border-radius:2px}

.logo{padding:20px 20px 16px;border-bottom:1px solid var(--border)}
.logo-row{display:flex;align-items:center;gap:10px}
.logo-gem{
  width:32px;height:32px;border-radius:9px;flex-shrink:0;
  background:linear-gradient(135deg,var(--accent),var(--accent2));
  display:grid;place-items:center;font-size:15px;
}
.logo-name{font-family:'Space Grotesk',sans-serif;font-size:16px;font-weight:700}
.logo-sub{font-size:10px;color:var(--muted);margin-top:1px;letter-spacing:.4px}

/* sidebar sections */
.s-sec{padding:16px 20px 0}
.s-lbl{font-size:9.5px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;
       color:var(--muted);display:flex;align-items:center;gap:8px;margin-bottom:12px}
.s-lbl::after{content:'';flex:1;height:1px;background:var(--border)}
.s-field{margin-bottom:12px}
.s-field label{display:block;font-size:11px;font-weight:500;color:var(--dim);margin-bottom:5px;letter-spacing:.2px}

.inp{
  width:100%;background:var(--s2);border:1px solid var(--border);
  color:var(--text);font-family:'Inter',sans-serif;font-size:13px;
  padding:8px 10px;border-radius:7px;outline:none;
  transition:border-color .18s,box-shadow .18s;
}
.inp:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(0,229,190,.1)}
.inp-wrap{position:relative}
.eye-btn{position:absolute;right:9px;top:50%;transform:translateY(-50%);
         background:none;border:none;color:var(--muted);cursor:pointer;font-size:14px;padding:3px;
         transition:color .15s}
.eye-btn:hover{color:var(--accent)}

/* num row */
.num-row{display:flex;align-items:center;gap:6px}
.n-btn{
  width:30px;height:30px;border-radius:6px;flex-shrink:0;
  background:var(--s2);border:1px solid var(--border);
  color:var(--text);font-size:15px;cursor:pointer;
  display:grid;place-items:center;transition:all .15s;
}
.n-btn:hover{background:var(--s3);border-color:var(--accent)}
.n-inp{
  flex:1;text-align:center;background:var(--s2);border:1px solid var(--border);
  color:var(--text);font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:500;
  padding:5px 6px;border-radius:6px;outline:none;
}
.n-inp:focus{border-color:var(--accent)}

/* domain pills */
.d-pills{display:flex;flex-wrap:wrap;gap:5px}
.d-pill{
  padding:5px 11px;border-radius:20px;border:1px solid var(--border);
  background:var(--s2);color:var(--dim);font-size:11.5px;font-weight:500;
  cursor:pointer;transition:all .15s;
}
.d-pill:hover{border-color:var(--accent);color:var(--accent)}
.d-pill.active{background:rgba(0,229,190,.1);border-color:var(--accent);color:var(--accent)}
.cap-pill{
  padding:5px 11px;border-radius:20px;border:1px solid var(--border);
  background:var(--s2);color:var(--dim);font-size:11px;font-weight:500;
  cursor:pointer;transition:all .15s;
}
.cap-pill:hover{border-color:#a78bfa;color:#a78bfa}
.cap-pill.active{background:rgba(167,139,250,.12);border-color:#a78bfa;color:#a78bfa}

/* balance */
.bal-card{
  background:linear-gradient(135deg,rgba(0,229,190,.07),rgba(99,102,241,.07));
  border:1px solid rgba(0,229,190,.15);border-radius:9px;
  padding:12px 14px;display:flex;align-items:center;justify-content:space-between;
}
.bal-val{font-family:'Space Grotesk',sans-serif;font-size:22px;font-weight:700;
         color:var(--accent);font-variant-numeric:tabular-nums}
.ghost-btn{background:var(--s2);border:1px solid var(--border);color:var(--dim);
           font-size:11.5px;padding:5px 10px;border-radius:6px;cursor:pointer;
           transition:all .15s}
.ghost-btn:hover{border-color:var(--accent);color:var(--accent)}

/* sidebar bottom */
.s-actions{padding:16px 20px;margin-top:auto;border-top:1px solid var(--border)}
.btn-start{
  width:100%;padding:12px;border-radius:9px;border:none;
  background:var(--accent);color:#000;
  font-family:'Space Grotesk',sans-serif;font-size:14px;font-weight:700;
  cursor:pointer;transition:all .2s;display:flex;align-items:center;justify-content:center;gap:7px;
  margin-bottom:7px;
}
.btn-start:hover{background:#00ffcf;box-shadow:0 0 20px rgba(0,229,190,.35)}
.btn-start:disabled{background:var(--s3);color:var(--muted);cursor:not-allowed;box-shadow:none}
.btn-stop{
  width:100%;padding:10px;border-radius:9px;background:transparent;
  border:1px solid rgba(239,68,68,.5);color:var(--err);font-size:13px;font-weight:500;
  cursor:pointer;transition:all .18s;display:flex;align-items:center;justify-content:center;gap:6px;
}
.btn-stop:hover{background:rgba(239,68,68,.08)}
.btn-stop:disabled{border-color:var(--border);color:var(--muted);cursor:not-allowed}

/* ── main ── */
.main{flex:1;display:flex;flex-direction:column;overflow:hidden}

/* tabs */
.tabs{
  display:flex;background:var(--s1);border-bottom:1px solid var(--border);
  padding:0 20px;flex-shrink:0;
}
.tab{
  padding:14px 18px;font-size:13px;font-weight:600;color:var(--muted);
  cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px;
  transition:all .18s;display:flex;align-items:center;gap:7px;
  font-family:'Space Grotesk',sans-serif;
}
.tab:hover{color:var(--dim)}
.tab.active{color:var(--accent);border-bottom-color:var(--accent)}
.tab-icon{font-size:14px}

/* stats bar */
.stats{
  display:flex;align-items:stretch;background:var(--s1);
  border-bottom:1px solid var(--border);padding:0 20px;flex-shrink:0;
}
.stat{padding:14px 20px;display:flex;flex-direction:column;gap:2px;border-right:1px solid var(--border)}
.stat:last-child{border-right:none;margin-left:auto;justify-content:center}
.stat-v{font-family:'Space Grotesk',sans-serif;font-size:20px;font-weight:700;
        font-variant-numeric:tabular-nums;line-height:1}
.stat-l{font-size:10.5px;color:var(--muted);letter-spacing:.2px}
.c-ok{color:var(--ok)} .c-err{color:var(--err)} .c-acc{color:var(--accent)} .c-dim{color:var(--dim)}

.export-btn{
  background:var(--s2);border:1px solid var(--border);color:var(--dim);
  font-size:12.5px;font-weight:500;padding:7px 14px;border-radius:7px;cursor:pointer;
  transition:all .15s;white-space:nowrap;display:flex;align-items:center;gap:5px;
}
.export-btn:hover{border-color:var(--accent2);color:var(--accent2)}

/* tab panels */
.panel{display:none;flex:1;flex-direction:column;overflow:hidden}
.panel.active{display:flex}

/* ── autoreg panel ── */
.ar-content{flex:1;display:flex;flex-direction:column;overflow:hidden}

.log-wrap{flex:1;display:flex;flex-direction:column;padding:16px 20px 12px;min-height:0}
.phdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
.ptitle{
  font-family:'Space Grotesk',sans-serif;font-size:12.5px;font-weight:600;
  color:var(--dim);display:flex;align-items:center;gap:7px;
}
.live-dot{width:7px;height:7px;border-radius:50%;background:var(--ok);animation:blink 1.4s ease infinite}
.live-dot.idle{background:var(--muted);animation:none}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.25}}
.clr-btn{background:none;border:1px solid var(--border);color:var(--muted);
         font-size:11.5px;padding:4px 10px;border-radius:6px;cursor:pointer;transition:all .15s}
.clr-btn:hover{color:var(--text);border-color:var(--border2)}

.log-box{
  flex:1;background:var(--s1);border:1px solid var(--border);border-radius:9px;
  overflow-y:auto;padding:12px 14px;
  font-family:'JetBrains Mono',monospace;font-size:11.5px;line-height:1.7;min-height:0;
}
.log-box::-webkit-scrollbar{width:3px}
.log-box::-webkit-scrollbar-thumb{background:var(--s3);border-radius:2px}
.log-ln{display:flex;gap:9px;align-items:baseline}
.log-ts{color:var(--muted);flex-shrink:0;font-size:10.5px}
.log-msg{word-break:break-all}
.l-info{color:var(--text)} .l-success{color:var(--ok)} .l-error{color:var(--err)}
.l-warning{color:var(--warn)} .l-section{color:var(--accent);font-weight:500}
.l-muted{color:var(--muted)}

/* results table */
.tbl-wrap{
  border-top:1px solid var(--border);padding:0 20px 16px;
  flex-shrink:0;max-height:220px;display:flex;flex-direction:column;
}
.tbl-wrap .phdr{margin:12px 0 8px}
.tbl-scroll{overflow-y:auto;flex:1}
.tbl-scroll::-webkit-scrollbar{width:3px}
.tbl-scroll::-webkit-scrollbar-thumb{background:var(--s3);border-radius:2px}
table{width:100%;border-collapse:collapse}
thead th{
  position:sticky;top:0;background:var(--s2);
  font-size:9.5px;font-weight:700;letter-spacing:1px;text-transform:uppercase;
  color:var(--muted);padding:7px 10px;text-align:left;border-bottom:1px solid var(--border);
}
tbody tr{border-bottom:1px solid rgba(255,255,255,.025);transition:background .1s}
tbody tr:hover{background:var(--glass)}
tbody td{padding:8px 10px;font-family:'JetBrains Mono',monospace;font-size:11.5px;color:var(--text)}
.td-email{color:var(--accent)} .td-pwd{color:var(--dim)} .td-ts{color:var(--muted)}
.empty-td{text-align:center;color:var(--muted);padding:18px;font-family:'Inter',sans-serif;font-size:13px}

/* ── ThingsMail panel ── */
.tm-panel{flex:1;overflow-y:auto;padding:24px}
.tm-panel::-webkit-scrollbar{width:4px}
.tm-panel::-webkit-scrollbar-thumb{background:var(--s3);border-radius:2px}
.tm-card{
  max-width:560px;margin:0 auto;
  background:var(--s1);border:1px solid var(--border);
  border-radius:12px;overflow:hidden;box-shadow:var(--shadow);
}
.tm-sec-toggle{
  width:100%;display:flex;align-items:center;gap:8px;
  padding:12px 20px;background:none;border:none;border-bottom:1px solid var(--border);
  cursor:pointer;font-family:'Space Grotesk',sans-serif;font-size:11px;font-weight:700;
  letter-spacing:.07em;text-transform:uppercase;color:var(--dim);
  text-align:left;transition:background .12s;
}
.tm-sec-toggle:hover{background:var(--s2)}
.tm-sec-toggle .chev{margin-left:auto;transition:transform .2s;opacity:.5}
.tm-sec-toggle[aria-expanded="true"] .chev{transform:rotate(180deg)}
.badge{font-size:10px;font-weight:700;padding:2px 7px;border-radius:10px;margin-left:4px}
.badge.on{background:rgba(34,197,94,.15);color:var(--ok)}
.badge.off{background:var(--s3);color:var(--muted)}

.tm-body{padding:16px 20px;border-bottom:1px solid var(--border);background:var(--s2)}
.tm-body[hidden]{display:none}
.auth-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.tm-field{display:flex;flex-direction:column;gap:5px;margin-bottom:12px}
.tm-field label{font-size:11px;font-weight:600;letter-spacing:.07em;
                text-transform:uppercase;color:var(--dim)}
.tm-inp{
  font-family:'Inter',sans-serif;font-size:13px;color:var(--text);
  background:var(--s1);border:1.5px solid var(--border);
  border-radius:7px;padding:8px 11px;outline:none;width:100%;
  transition:border-color .15s,box-shadow .15s;
}
.tm-inp:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(0,229,190,.1)}
.tm-inp.err{border-color:var(--err)}
textarea.tm-inp{resize:vertical;line-height:1.65}

.status-bar{
  display:flex;align-items:center;gap:8px;margin-top:10px;
  padding:8px 11px;border-radius:7px;font-size:12.5px;font-weight:500;
}
.status-bar.idle{background:var(--s1);color:var(--muted);border:1px solid var(--border)}
.status-bar.ok{background:rgba(34,197,94,.08);color:var(--ok);border:1px solid rgba(34,197,94,.25)}
.status-bar.err{background:rgba(239,68,68,.08);color:var(--err);border:1px solid rgba(239,68,68,.25)}
.status-bar.busy{background:rgba(0,229,190,.08);color:var(--accent);border:1px solid rgba(0,229,190,.25)}
.s-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.idle .s-dot{background:var(--muted)} .ok .s-dot{background:var(--ok)}
.err .s-dot{background:var(--err)} .busy .s-dot{background:var(--accent);animation:blink 1s ease infinite}

.tm-btn{
  font-family:'Space Grotesk',sans-serif;font-size:13px;font-weight:700;
  color:#000;background:var(--accent);border:none;border-radius:7px;
  padding:10px 16px;cursor:pointer;width:100%;display:flex;align-items:center;
  justify-content:center;gap:7px;transition:all .18s;margin-top:8px;
}
.tm-btn:hover:not(:disabled){background:#00ffcf;box-shadow:0 0 16px rgba(0,229,190,.3)}
.tm-btn:disabled{opacity:.45;cursor:not-allowed}
.tm-btn.info{background:var(--accent2);color:#fff}
.tm-btn.info:hover:not(:disabled){background:#4f52e0}
.spin{width:14px;height:14px;border:2px solid currentColor;border-top-color:transparent;
      border-radius:50%;animation:spin .65s linear infinite;display:none;flex-shrink:0}
@keyframes spin{to{transform:rotate(360deg)}}
.tm-btn.loading .spin{display:block}
.tm-btn.loading .btn-t,.tm-btn.loading .btn-ico{display:none}

.tm-form{padding:20px}
.meta-row{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:5px}
.label-row{display:flex;align-items:center;justify-content:space-between;gap:8px}
.email-badge{font-size:11px;font-weight:600;padding:2px 8px;border-radius:20px}
.email-badge.none{background:var(--s3);color:var(--muted)}
.email-badge.some{background:rgba(0,229,190,.12);color:var(--accent)}
.id-btn{font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:600;
        color:var(--accent);background:rgba(0,229,190,.1);border:none;border-radius:5px;
        padding:3px 8px;cursor:pointer;transition:opacity .15s}
.id-btn:hover{opacity:.7}
.char-c{font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums}
.id-hint{font-size:11px;color:var(--muted)}
.id-hint code{font-family:'JetBrains Mono',monospace;color:var(--accent);
              background:rgba(0,229,190,.1);padding:1px 5px;border-radius:4px;font-size:10.5px}

/* tm log */
.tm-log-body{border-top:1px solid var(--border);background:var(--bg)}
.tm-log-body[hidden]{display:none}
.tm-log-scroll{
  height:160px;overflow-y:auto;padding:10px 14px;
  font-family:'JetBrains Mono',monospace;font-size:11.5px;line-height:1.7;
}
.tm-log-scroll::-webkit-scrollbar{width:3px}
.tm-log-scroll::-webkit-scrollbar-thumb{background:var(--s3);border-radius:2px}
.tm-log-entry{display:flex;align-items:baseline;gap:8px;padding:1px 0}
.tm-ts{color:var(--muted);flex-shrink:0;font-size:10.5px}
.tm-icon{flex-shrink:0;width:12px;text-align:center}
.tm-icon.ok{color:var(--ok)} .tm-icon.err{color:var(--err)}
.tm-icon.info{color:var(--accent)} .tm-icon.warn{color:var(--warn)}
.tm-msg{color:var(--dim);word-break:break-all}
.log-toolbar{display:flex;justify-content:flex-end;padding:6px 14px;border-bottom:1px solid var(--border)}
.clr2{background:none;border:1px solid var(--border);color:var(--muted);
      font-size:11px;font-weight:600;padding:3px 9px;border-radius:5px;
      cursor:pointer;transition:all .15s}
.clr2:hover{color:var(--text);border-color:var(--border2)}
.tm-empty{padding:12px 14px;color:var(--muted);font-family:'JetBrains Mono',monospace;font-size:11.5px}

.tm-foot{padding:10px 20px;border-top:1px solid var(--border);
         display:flex;align-items:center;justify-content:center;gap:6px;
         font-size:11px;color:var(--muted);background:var(--s2)}

/* toast */
#toast{
  position:fixed;bottom:20px;left:50%;transform:translateX(-50%) translateY(8px);
  padding:9px 16px;border-radius:8px;font-size:13px;font-weight:500;
  display:flex;align-items:center;gap:7px;box-shadow:var(--shadow);
  opacity:0;pointer-events:none;transition:opacity .2s,transform .2s;z-index:999;
  max-width:calc(100vw - 32px);
}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
#toast.ok{background:var(--ok);color:#000} #toast.err{background:var(--err);color:#fff}

::-webkit-scrollbar{width:4px}
::-webkit-scrollbar-thumb{background:var(--s3)}
</style>
</head>
<body>
<div class="shell">

<!-- ══════════════════════════════════════════
     SIDEBAR
════════════════════════════════════════════ -->
<aside class="sidebar">
  <div class="logo">
    <div class="logo-row">
      <div class="logo-gem">⚡</div>
      <div>
        <div class="logo-name">TB Suite</div>
        <div class="logo-sub">Autoreg · ThingsMail</div>
      </div>
    </div>
  </div>

  <!-- AnyMessage -->
  <div class="s-sec">
    <div class="s-lbl">AnyMessage API</div>
    <div class="s-field">
      <label>Токен</label>
      <div class="inp-wrap">
        <input id="tokenInp" type="password" class="inp" placeholder="Вставьте токен…" style="padding-right:34px">
        <button class="eye-btn" id="eyeBtn">👁</button>
      </div>
    </div>
    <div class="bal-card" style="margin-bottom:6px">
      <div>
        <div style="font-size:10px;color:var(--muted);margin-bottom:3px">Баланс AnyMessage</div>
        <div class="bal-val" id="balVal">—</div>
      </div>
      <button class="ghost-btn" onclick="checkBalance()">Обновить</button>
    </div>
  </div>

  <!-- Proxies -->
  <div class="s-sec" style="margin-top:16px">
    <div class="s-lbl">Прокси-ротация</div>
    <div class="s-field">
      <label>Список прокси <span id="proxyCount" style="color:var(--accent);margin-left:4px">0 шт.</span></label>
      <textarea id="proxyArea" class="inp" rows="4"
        style="resize:vertical;font-family:'JetBrains Mono',monospace;font-size:11px;line-height:1.6;padding:8px 10px"
        placeholder="user:pass:host:port&#10;user:pass:host:port&#10;host:port&#10;http://host:port"
        oninput="proxyChanged()"></textarea>
    </div>
    <div style="display:flex;align-items:center;gap:7px;margin-bottom:4px">
      <button class="ghost-btn" onclick="loadProxies()" style="flex:1">Применить</button>
      <button class="ghost-btn" onclick="clearProxies()" style="color:var(--err);border-color:rgba(239,68,68,.3)">Очистить</button>
    </div>
    <div id="proxyStatus" style="font-size:11px;color:var(--muted);margin-bottom:2px">Ротация: выкл</div>
  </div>

  <!-- Captcha -->
  <div class="s-sec" style="margin-top:16px">
    <div class="s-lbl">Антикапча</div>
    <div class="s-field">
      <label>Сервис</label>
      <div style="display:flex;gap:5px;flex-wrap:wrap">
        <button class="cap-pill active" data-svc="capsolver">CapSolver</button>
        <button class="cap-pill" data-svc="2captcha">2Captcha</button>
        <button class="cap-pill" data-svc="capmonster">CapMonster</button>
        <button class="cap-pill" data-svc="rucaptcha">RuCaptcha</button>
      </div>
    </div>
    <div class="s-field" style="margin-top:8px">
      <label>API ключ</label>
      <div class="inp-wrap">
        <input id="capKeyInp" type="password" class="inp" placeholder="Вставьте ключ…" style="padding-right:34px" oninput="capKeyChanged()">
        <button class="eye-btn" id="capEyeBtn" onclick="toggleCapEye()">👁</button>
      </div>
    </div>
    <div id="capStatus" style="font-size:11px;color:var(--muted);margin-top:2px">Ключ не задан</div>
  </div>

  <!-- Domain -->
  <div class="s-sec" style="margin-top:16px">
    <div class="s-lbl">Домен почты</div>
    <div class="d-pills" id="dPills">
      <button class="d-pill active" data-d="outlook">outlook.com</button>
      <button class="d-pill" data-d="hotmail">hotmail.com</button>
      <button class="d-pill" data-d="rambler">rambler.ru</button>
      <button class="d-pill" data-d="gmail">gmail.com</button>
    </div>
    <div id="dAvail" style="font-size:11px;color:var(--muted);margin-top:7px">— доступно</div>
  </div>

  <!-- Params -->
  <div class="s-sec" style="margin-top:16px">
    <div class="s-lbl">Параметры</div>
    <div class="s-field">
      <label>Количество аккаунтов</label>
      <div class="num-row">
        <button class="n-btn" onclick="adj('count',-1)">−</button>
        <input id="countInp" class="n-inp" type="number" value="3" min="1" max="500">
        <button class="n-btn" onclick="adj('count',1)">+</button>
      </div>
    </div>
    <div class="s-field">
      <label>Потоков (параллельно)</label>
      <div class="num-row">
        <button class="n-btn" onclick="adj('threads',-1)">−</button>
        <input id="threadsInp" class="n-inp" type="number" value="3" min="1" max="20">
        <button class="n-btn" onclick="adj('threads',1)">+</button>
      </div>
    </div>
    <div class="s-field">
      <label>Задержка (сек)</label>
      <div class="num-row">
        <button class="n-btn" onclick="adj('delay',-1)">−</button>
        <input id="delayInp" class="n-inp" type="number" value="3" min="0" max="120">
        <button class="n-btn" onclick="adj('delay',1)">+</button>
      </div>
    </div>
  </div>

  <div class="s-actions">
    <button class="btn-start" id="startBtn" onclick="startReg()">▶ Запустить</button>
    <button class="btn-stop"  id="stopBtn"  disabled onclick="stopReg()">⏹ Остановить</button>
  </div>
</aside>

<!-- ══════════════════════════════════════════
     MAIN
════════════════════════════════════════════ -->
<div class="main">

  <!-- tabs -->
  <div class="tabs">
    <div class="tab active" data-tab="autoreg" onclick="switchTab('autoreg')">
      <span class="tab-icon">⚙</span> Autoreg
    </div>
    <div class="tab" data-tab="thingsmail" onclick="switchTab('thingsmail')">
      <span class="tab-icon">✉</span> ThingsMail
    </div>
    <div class="tab" data-tab="campaign" onclick="switchTab('campaign')">
      <span class="tab-icon">🚀</span> Кампания
    </div>
  </div>

  <!-- ── AUTOREG tab ── -->
  <div class="panel active" id="tab-autoreg">

    <!-- stats -->
    <div class="stats">
      <div class="stat"><div class="stat-v c-ok" id="sDone">0</div><div class="stat-l">Создано</div></div>
      <div class="stat"><div class="stat-v c-err" id="sFail">0</div><div class="stat-l">Ошибок</div></div>
      <div class="stat"><div class="stat-v c-acc" id="sElap">—</div><div class="stat-l">Прошло</div></div>
      <div class="stat"><div class="stat-v c-dim" id="sStat">Ожидание</div><div class="stat-l">Статус</div></div>
      <div class="stat">
        <button class="export-btn" onclick="exportCsv()">↓ Экспорт CSV</button>
      </div>
    </div>

    <div class="ar-content">
      <!-- log -->
      <div class="log-wrap">
        <div class="phdr">
          <div class="ptitle">
            <div class="live-dot idle" id="liveDot"></div>
            Журнал событий
          </div>
          <button class="clr-btn" onclick="clearLog()">Очистить</button>
        </div>
        <div class="log-box" id="logBox"></div>
      </div>

      <!-- results -->
      <div class="tbl-wrap">
        <div class="phdr">
          <div class="ptitle">Результаты</div>
          <div style="font-size:11.5px;color:var(--muted)" id="resCount">0 записей</div>
        </div>
        <div class="tbl-scroll">
          <table>
            <thead>
              <tr>
                <th style="width:30%">Email</th>
                <th style="width:19%">Пароль</th>
                <th style="width:14%">Имя</th>
                <th style="width:21%">Прокси</th>
                <th style="width:16%">Создан</th>
              </tr>
            </thead>
            <tbody id="tblBody">
              <tr><td class="empty-td" colspan="4">Аккаунты появятся здесь</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div><!-- /tab-autoreg -->

  <!-- ── THINGSMAIL tab ── -->
  <div class="panel" id="tab-thingsmail">
    <div class="tm-panel">
      <div class="tm-card">

        <!-- Auth -->
        <button class="tm-sec-toggle" id="tmAuthToggle" aria-expanded="true" onclick="tmToggle('tmAuth')">
          🔐 Аккаунт ThingsBoard
          <span class="badge off" id="tmAuthBadge">Не подключено</span>
          <svg class="chev" width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><path d="M7.41 8.59L12 13.17l4.59-4.58L18 10l-6 6-6-6z"/></svg>
        </button>
        <div class="tm-body" id="tmAuthBody">
          <div class="auth-grid" style="margin-bottom:10px">
            <div class="tm-field" style="margin-bottom:0">
              <label>Email</label>
              <input type="email" class="tm-inp" id="tmEmail" placeholder="you@example.com">
            </div>
            <div class="tm-field" style="margin-bottom:0">
              <label>Пароль</label>
              <input type="password" class="tm-inp" id="tmPass" placeholder="••••••••">
            </div>
          </div>
          <button class="tm-btn" id="tmConnBtn" onclick="tmConnect()">
            <svg class="btn-ico" width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>
            <span class="btn-t">Подключить</span>
            <div class="spin"></div>
          </button>
          <div class="status-bar idle" id="tmStatus">
            <div class="s-dot"></div><span id="tmStatusTxt">Не подключено</span>
          </div>
          <button class="tm-btn info" id="tmSetupBtn" onclick="tmSetupAll()" style="margin-top:8px">
            <svg class="btn-ico" width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><path d="M19.14 12.94c.04-.31.06-.63.06-.94s-.02-.63-.06-.94l2.03-1.58a.49.49 0 00.12-.61l-1.92-3.32a.49.49 0 00-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54a.484.484 0 00-.48-.41H9.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96a.49.49 0 00-.59.22L2.74 8.87a.49.49 0 00.12.61l2.03 1.58c-.04.31-.06.63-.06.94s.02.63.06.94L2.86 14.53a.49.49 0 00-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32a.49.49 0 00-.12-.61l-2.01-1.59zM12 15.6A3.6 3.6 0 1115.6 12 3.6 3.6 0 0112 15.6z"/></svg>
            <span class="btn-t">Настроить Rule Chain — все аккаунты</span>
            <div class="spin"></div>
          </button>

          <!-- bulk progress -->
          <div id="bulkProgress" hidden style="margin-top:10px;border:1px solid var(--border);border-radius:8px;overflow:hidden">
            <div style="display:flex;align-items:center;justify-content:space-between;padding:9px 12px;background:var(--s1);border-bottom:1px solid var(--border)">
              <div style="font-size:11px;font-weight:600;color:var(--dim);display:flex;align-items:center;gap:6px">
                <div class="live-dot" id="bulkDot" style="width:6px;height:6px"></div>
                Прогресс
              </div>
              <div style="font-size:11px;color:var(--muted)">
                <span style="color:var(--ok)" id="bulkDone">0</span> /
                <span id="bulkTotal">0</span>
                &nbsp;·&nbsp;
                <span style="color:var(--err)" id="bulkFail">0</span> ошибок
              </div>
            </div>
            <div id="bulkLog" style="
              max-height:150px;overflow-y:auto;padding:10px 12px;
              font-family:'JetBrains Mono',monospace;font-size:11px;line-height:1.7;
              background:var(--bg);
            "></div>
          </div>
        </div>

        <!-- Proxy -->
        <button class="tm-sec-toggle" id="tmProxyToggle" aria-expanded="false" onclick="tmToggle('tmProxy')">
          🌐 Прокси
          <span class="badge off" id="tmProxyBadge">Выкл</span>
          <svg class="chev" width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><path d="M7.41 8.59L12 13.17l4.59-4.58L18 10l-6 6-6-6z"/></svg>
        </button>
        <div class="tm-body" id="tmProxyBody" hidden>
          <div class="tm-field">
            <label>URL прокси</label>
            <input type="text" class="tm-inp" id="tmProxy" placeholder="http://host:port или http://user:pass@host:port" oninput="tmProxyUpdate()">
          </div>
        </div>

        <!-- Form -->
        <form class="tm-form" id="tmForm" novalidate onsubmit="tmSend(event)">
          <div class="tm-field">
            <div class="label-row" style="margin-bottom:5px">
              <label>Кому</label>
              <span class="email-badge none" id="tmEBadge">0 адресов</span>
            </div>
            <textarea class="tm-inp" id="tmTo" style="min-height:70px;font-size:12.5px"
              placeholder="Адреса — каждый с новой строки или через запятую" oninput="tmCountEmails()"></textarea>
          </div>
          <div class="tm-field">
            <div class="label-row" style="margin-bottom:5px">
              <label>Тема</label>
              <button type="button" class="id-btn" onclick="tmInsertId('tmSubject')">{id}</button>
            </div>
            <input type="text" class="tm-inp" id="tmSubject" placeholder="Тема письма" maxlength="200">
          </div>
          <div class="tm-field">
            <div class="label-row" style="margin-bottom:5px">
              <label>Сообщение</label>
              <button type="button" class="id-btn" onclick="tmInsertId('tmBody')">{id}</button>
            </div>
            <textarea class="tm-inp" id="tmBody" style="min-height:110px" maxlength="5000" oninput="tmCharCount()"></textarea>
            <div class="meta-row" style="margin-top:4px">
              <span class="id-hint">Каждому придёт свой <code>{id}</code></span>
              <span class="char-c" id="tmCharC">0 / 5000</span>
            </div>
          </div>
          <button type="submit" class="tm-btn" id="tmSendBtn" disabled>
            <svg class="btn-ico" width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
            <span class="btn-t" id="tmSendLbl">Отправить</span>
            <div class="spin"></div>
          </button>
        </form>

        <!-- Log -->
        <button class="tm-sec-toggle" id="tmLogToggle" aria-expanded="true" onclick="tmToggle('tmLog')" style="border-top:1px solid var(--border)">
          📋 Логи
          <span class="badge off" id="tmLogC">0</span>
          <svg class="chev" width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><path d="M7.41 8.59L12 13.17l4.59-4.58L18 10l-6 6-6-6z"/></svg>
        </button>
        <div class="tm-log-body" id="tmLogBody">
          <div class="log-toolbar">
            <button class="clr2" onclick="tmClearLog()">Очистить</button>
          </div>
          <div class="tm-log-scroll" id="tmLogScroll">
            <div class="tm-empty" id="tmLogEmpty">Пока нет событий…</div>
          </div>
        </div>

        <div class="tm-foot">
          <span>ThingsBoard Cloud</span>
          <span style="width:3px;height:3px;background:var(--muted);border-radius:50%;display:inline-block;opacity:.5"></span>
          <span>Rule Chain</span>
          <span style="width:3px;height:3px;background:var(--muted);border-radius:50%;display:inline-block;opacity:.5"></span>
          <span>100 писем / месяц (Free)</span>
        </div>
      </div>
    </div>
  </div><!-- /tab-thingsmail -->

  <!-- ── CAMPAIGN tab ── -->
  <div class="panel" id="tab-campaign">
    <div class="tm-panel">
      <div class="tm-card">

        <!-- stats row -->
        <div style="display:flex;border-bottom:1px solid var(--border);background:var(--s2)">
          <div style="flex:1;padding:14px 16px;border-right:1px solid var(--border)">
            <div style="font-size:10px;color:var(--muted);margin-bottom:3px">Аккаунтов осталось</div>
            <div class="stat-v c-acc" id="cAccLeft">—</div>
          </div>
          <div style="flex:1;padding:14px 16px;border-right:1px solid var(--border)">
            <div style="font-size:10px;color:var(--muted);margin-bottom:3px">Использовано</div>
            <div class="stat-v c-ok" id="cAccDone">0</div>
          </div>
          <div style="flex:1;padding:14px 16px;border-right:1px solid var(--border)">
            <div style="font-size:10px;color:var(--muted);margin-bottom:3px">Писем отправлено</div>
            <div class="stat-v" style="color:var(--accent2)" id="cEmailsSent">0</div>
          </div>
          <div style="flex:1;padding:14px 16px">
            <div style="font-size:10px;color:var(--muted);margin-bottom:3px">Ошибок</div>
            <div class="stat-v c-err" id="cFail">0</div>
          </div>
        </div>

        <!-- form -->
        <div class="tm-form">
          <div class="tm-field">
            <div class="label-row" style="margin-bottom:5px">
              <label>Получатели</label>
              <span class="email-badge none" id="cRecipBadge">0 адресов</span>
            </div>
            <textarea class="tm-inp" id="cRecip" style="min-height:80px;font-size:12px"
              placeholder="Адреса через запятую или каждый с новой строки" oninput="cCountRecip()"></textarea>
          </div>
          <div class="tm-field">
            <div class="label-row" style="margin-bottom:5px">
              <label>Тема</label>
              <button type="button" class="id-btn" onclick="tmInsertId('cSubject')">{id}</button>
            </div>
            <input type="text" class="tm-inp" id="cSubject" placeholder="Тема письма" maxlength="200">
          </div>
          <div class="tm-field">
            <div class="label-row" style="margin-bottom:5px">
              <label>Сообщение</label>
              <button type="button" class="id-btn" onclick="tmInsertId('cBody')">{id}</button>
            </div>
            <textarea class="tm-inp" id="cBody" style="min-height:100px" maxlength="5000"
              placeholder="Текст письма… {id} = уникальный код для каждого получателя"></textarea>
          </div>
          <div class="tm-field">
            <label>Писем с одного аккаунта</label>
            <div class="num-row">
              <button class="n-btn" onclick="adjC(-1)">−</button>
              <input id="cPerAcc" class="n-inp" type="number" value="10" min="1" max="500">
              <button class="n-btn" onclick="adjC(1)">+</button>
            </div>
          </div>

          <div style="display:flex;gap:8px;margin-top:4px">
            <button class="tm-btn" id="cStartBtn" onclick="campStart()" style="flex:1">
              <svg class="btn-ico" width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>
              <span class="btn-t">Запустить кампанию</span>
              <div class="spin"></div>
            </button>
            <button class="tm-btn" id="cStopBtn" onclick="campStop()" disabled
              style="flex:0 0 auto;background:transparent;border:1px solid rgba(239,68,68,.5);color:var(--err);min-width:120px">
              <span class="btn-t">⏹ Стоп</span>
              <div class="spin"></div>
            </button>
          </div>
        </div>

        <!-- progress log -->
        <div style="border-top:1px solid var(--border)">
          <div class="log-toolbar" style="display:flex;justify-content:space-between;align-items:center;padding:8px 14px;border-bottom:1px solid var(--border)">
            <div class="ptitle" style="font-size:12px">
              <div class="live-dot idle" id="cDot" style="width:6px;height:6px"></div>
              Прогресс кампании
            </div>
            <button class="clr2" onclick="document.getElementById('cLog').innerHTML=''">Очистить</button>
          </div>
          <div id="cLog" style="
            max-height:220px;overflow-y:auto;padding:10px 14px;
            font-family:'JetBrains Mono',monospace;font-size:11px;line-height:1.7;
            background:var(--bg);
          "></div>
        </div>

        <div class="tm-foot">
          <span>Авто-цикл: аккаунт → письма → удаление → следующий</span>
        </div>
      </div>
    </div>
  </div><!-- /tab-campaign -->

</div><!-- /main -->
</div><!-- /shell -->

<div id="toast" role="alert"></div>

<script>
// ══════════════════════════════════════════
//  TABS
// ══════════════════════════════════════════
function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t =>
    t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.panel').forEach(p =>
    p.classList.toggle('active', p.id === 'tab-' + name));
}

// ══════════════════════════════════════════
//  SIDEBAR utils
// ══════════════════════════════════════════
let selDomain = 'outlook';
document.querySelectorAll('.d-pill').forEach(b => b.addEventListener('click', () => {
  document.querySelectorAll('.d-pill').forEach(x => x.classList.remove('active'));
  b.classList.add('active'); selDomain = b.dataset.d; updateAvail();
}));
document.getElementById('eyeBtn').addEventListener('click', () => {
  const i = document.getElementById('tokenInp');
  i.type = i.type === 'password' ? 'text' : 'password';
});

// ── Captcha section ──────────────────────────────────────────────────────
let selCapSvc = 'capsolver';
document.querySelectorAll('.cap-pill').forEach(b => b.addEventListener('click', () => {
  document.querySelectorAll('.cap-pill').forEach(x => x.classList.remove('active'));
  b.classList.add('active'); selCapSvc = b.dataset.svc; saveCapSettings();
}));
function toggleCapEye() {
  const i = document.getElementById('capKeyInp');
  i.type = i.type === 'password' ? 'text' : 'password';
}
let _capSaveTimer = null;
function capKeyChanged() {
  clearTimeout(_capSaveTimer);
  _capSaveTimer = setTimeout(saveCapSettings, 800);
}
async function saveCapSettings() {
  const key = document.getElementById('capKeyInp').value.trim();
  const st  = document.getElementById('capStatus');
  try {
    const r = await fetch('/api/captcha', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({service: selCapSvc, key})});
    const d = await r.json();
    st.textContent = d.key_set ? `✓ ${selCapSvc} · ключ сохранён` : 'Ключ не задан';
    st.style.color  = d.key_set ? 'var(--ok)' : 'var(--muted)';
  } catch(e) { st.textContent = 'Ошибка сохранения'; }
}
// load saved state on page load
(async () => {
  try {
    const d = await fetch('/api/captcha').then(r=>r.json());
    selCapSvc = d.service || 'capsolver';
    document.querySelectorAll('.cap-pill').forEach(b => {
      b.classList.toggle('active', b.dataset.svc === selCapSvc);
    });
    if (d.key) document.getElementById('capKeyInp').value = d.key;
    const st = document.getElementById('capStatus');
    st.textContent = d.key_set ? `✓ ${selCapSvc} · ключ сохранён` : 'Ключ не задан';
    st.style.color  = d.key_set ? 'var(--ok)' : 'var(--muted)';
  } catch(e) {}
})();
function adj(field, d) {
  const ids = {count:'countInp',threads:'threadsInp',delay:'delayInp'};
  const mins = {count:1,threads:1,delay:0};
  const maxs = {count:500,threads:20,delay:120};
  const el = document.getElementById(ids[field]);
  el.value = Math.min(maxs[field], Math.max(mins[field], (parseFloat(el.value)||0) + d));
}

// ══════════════════════════════════════════
//  AUTOREG
// ══════════════════════════════════════════
let running = false, evtSrc = null, arResults = [];
let tickIv = null, startTs = null;

(async function initAutoreg() {
  try {
    const [data, status] = await Promise.all([
      fetch('/api/results').then(r=>r.json()),
      fetch('/api/status').then(r=>r.json()),
    ]);
    renderTable(data);
    document.getElementById('sDone').textContent = status.done;
    document.getElementById('sFail').textContent = status.failed;
    if (status.elapsed) document.getElementById('sElap').textContent = status.elapsed+'с';
    if (status.running) {
      running = true; startTs = Date.now() - status.elapsed*1000;
      document.getElementById('sStat').textContent = 'Работает';
      document.getElementById('liveDot').classList.remove('idle');
      document.getElementById('startBtn').disabled = true;
      document.getElementById('stopBtn').disabled = false;
      tickIv = setInterval(()=>{
        if(!running){clearInterval(tickIv);return;}
        document.getElementById('sElap').textContent=Math.round((Date.now()-startTs)/1000)+'с';
      },1000);
      if(evtSrc) evtSrc.close();
      evtSrc = new EventSource('/api/log');
      evtSrc.onmessage = e => {
        const d = JSON.parse(e.data);
        addLog(d.msg, d.level, d.ts);
        if (d.msg==='__DONE__') onDone();
      };
      pollResults();
    }
  } catch(e){}
})();

async function checkBalance() {
  const token = document.getElementById('tokenInp').value.trim();
  if (!token) { addLog('Введите токен', 'error'); return; }
  document.getElementById('balVal').textContent = '…';
  try {
    const d = await fetch(`/api/balance?token=${encodeURIComponent(token)}`).then(r=>r.json());
    document.getElementById('balVal').textContent = d.status==='success' ? '$'+d.balance : '!';
    addLog(d.status==='success' ? 'Баланс: $'+d.balance : 'Ошибка: '+d.value,
           d.status==='success' ? 'success' : 'error');
  } catch(e) { document.getElementById('balVal').textContent='!'; }
  updateAvail();
}

async function updateAvail() {
  const token = document.getElementById('tokenInp').value.trim(); if(!token) return;
  try {
    const d = await fetch(`/api/quantity?token=${encodeURIComponent(token)}`).then(r=>r.json());
    if (d.status === 'success') {
      const map = {gmx:'gmx.com',mailcom:'mail.com',rambler:'rambler.ru',outlook:'outlook.com'};
      let cnt = 0;
      if (selDomain === 'mailcom') cnt = (d.data?.['mail.com']?.count||0)+(d.data?.['email.com']?.count||0);
      else cnt = d.data?.[map[selDomain]]?.count||0;
      document.getElementById('dAvail').textContent = cnt.toLocaleString('ru') + ' доступно';
    }
  } catch(e) {}
}

async function startReg() {
  const token = document.getElementById('tokenInp').value.trim();
  if (!token) { toast('Введите API токен', 'err'); return; }
  const count   = parseInt(document.getElementById('countInp').value)||1;
  const threads = parseInt(document.getElementById('threadsInp').value)||2;
  const delay   = parseFloat(document.getElementById('delayInp').value)||0;

  const r = await fetch('/api/start', {method:'POST',headers:{'Content-Type':'application/json'},
    body: JSON.stringify({token, domain:selDomain, count, threads, delay})});
  const d = await r.json();
  if (!d.ok) { toast(d.error||'Ошибка','err'); return; }

  running=true; startTs=Date.now(); arResults=[];
  document.getElementById('tblBody').innerHTML='<tr><td class="empty-td" colspan="4">Загрузка…</td></tr>';
  document.getElementById('resCount').textContent='0 записей';
  ['sDone','sFail'].forEach(id => document.getElementById(id).textContent='0');
  document.getElementById('sElap').textContent='0с';
  document.getElementById('sStat').textContent='Работает';
  document.getElementById('liveDot').classList.remove('idle');
  document.getElementById('startBtn').disabled=true;
  document.getElementById('stopBtn').disabled=false;

  tickIv = setInterval(()=>{
    if(!running){clearInterval(tickIv);return;}
    document.getElementById('sElap').textContent=Math.round((Date.now()-startTs)/1000)+'с';
  },1000);

  if(evtSrc) evtSrc.close();
  evtSrc = new EventSource('/api/log');
  evtSrc.onmessage = e => {
    const d = JSON.parse(e.data);
    addLog(d.msg, d.level, d.ts);
    if (d.msg==='__DONE__') onDone();
  };
  pollResults();
}

async function pollResults() {
  if(!running) return;
  try {
    const [data, status] = await Promise.all([
      fetch('/api/results').then(r=>r.json()),
      fetch('/api/status').then(r=>r.json()),
    ]);
    document.getElementById('sDone').textContent = status.done;
    document.getElementById('sFail').textContent = status.failed;
    renderTable(data);
  } catch(e){}
  if(running) setTimeout(pollResults,2000);
}

async function stopReg() {
  await fetch('/api/stop',{method:'POST'});
  addLog('Сигнал остановки отправлен…','warning');
}

function onDone() {
  running=false; clearInterval(tickIv);
  if(evtSrc) evtSrc.close();
  document.getElementById('sStat').textContent='Завершено';
  document.getElementById('liveDot').classList.add('idle');
  document.getElementById('startBtn').disabled=false;
  document.getElementById('stopBtn').disabled=true;
  Promise.all([fetch('/api/results').then(r=>r.json()), fetch('/api/status').then(r=>r.json())])
    .then(([data,s])=>{
      renderTable(data);
      document.getElementById('sDone').textContent=s.done;
      document.getElementById('sFail').textContent=s.failed;
      document.getElementById('sElap').textContent=s.elapsed+'с';
    });
}

function renderTable(data) {
  arResults=data;
  document.getElementById('resCount').textContent=data.length+' записей';
  const body=document.getElementById('tblBody');
  if(!data.length){body.innerHTML='<tr><td class="empty-td" colspan="4">Нет данных</td></tr>';return;}
  const shortProxy = p => {
    if (!p) return '—';
    try { const u = new URL(p); return u.hostname+':'+u.port; } catch { return p.split('@').pop(); }
  };
  body.innerHTML=data.map(r=>`
    <tr>
      <td class="td-email">${esc(r.email)}</td>
      <td class="td-pwd">${esc(r.password)}</td>
      <td>${esc(r.first+' '+r.last)}</td>
      <td style="color:var(--accent2);font-size:10.5px">${esc(shortProxy(r.proxy||''))}</td>
      <td class="td-ts">${esc(r.ts)}</td>
    </tr>`).join('');
}

function exportCsv(){ window.location.href='/api/export'; }

// log
function addLog(msg,level='info',ts=null) {
  if(msg==='__DONE__') return;
  const box=document.getElementById('logBox');
  const ln=document.createElement('div'); ln.className='log-ln';
  const t=ts||new Date().toLocaleTimeString('ru',{hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit'});
  ln.innerHTML=`<span class="log-ts">${t}</span><span class="log-msg l-${level}">${esc(msg)}</span>`;
  box.appendChild(ln); box.scrollTop=box.scrollHeight;
}
function clearLog(){ document.getElementById('logBox').innerHTML=''; }

// ══════════════════════════════════════════
//  THINGSMAIL
// ══════════════════════════════════════════
const tmState = {token: null};
let tmLogN = 0;

// init
window.addEventListener('DOMContentLoaded', () => {
  const email = localStorage.getItem('tm_email');
  const proxy = localStorage.getItem('tm_proxy');
  if(email) document.getElementById('tmEmail').value=email;
  if(proxy) { document.getElementById('tmProxy').value=proxy; tmProxyUpdate(); }
});

function tmToggle(name) {
  const btn = document.getElementById(name+'Toggle');
  const body = document.getElementById(name+'Body');
  const open = btn.getAttribute('aria-expanded')==='true';
  btn.setAttribute('aria-expanded', String(!open));
  body.hidden = open;
}

function tmProxyUpdate() {
  const v=document.getElementById('tmProxy').value.trim();
  const badge=document.getElementById('tmProxyBadge');
  badge.textContent=v?'Вкл':'Выкл'; badge.className='badge '+(v?'on':'off');
  v ? localStorage.setItem('tm_proxy',v) : localStorage.removeItem('tm_proxy');
}

function tmGetProxy() { return document.getElementById('tmProxy').value.trim()||null; }

function tmParseEmails(txt) {
  return [...new Set(
    txt.split(/[\n,;]+/).map(e=>e.trim().toLowerCase())
      .filter(e=>/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(e))
  )];
}
function tmCountEmails() {
  const n=tmParseEmails(document.getElementById('tmTo').value).length;
  const b=document.getElementById('tmEBadge');
  b.textContent=n===0?'0 адресов':n===1?'1 адрес':n+' адресов';
  b.className='email-badge '+(n>0?'some':'none');
  document.getElementById('tmSendLbl').textContent=n<=1?'Отправить письмо':'Отправить '+n+' письмам';
}
function tmCharCount() {
  document.getElementById('tmCharC').textContent=document.getElementById('tmBody').value.length+' / 5000';
}
function tmInsertId(id) {
  const el=document.getElementById(id);
  const s=el.selectionStart,e=el.selectionEnd;
  el.value=el.value.slice(0,s)+'{id}'+el.value.slice(e);
  el.selectionStart=el.selectionEnd=s+4;
  el.dispatchEvent(new Event('input')); el.focus();
}
function genId() { return Math.random().toString(36).substring(2,8).toUpperCase(); }

function tmSetStatus(type,txt) {
  const b=document.getElementById('tmStatus');
  b.className='status-bar '+type;
  document.getElementById('tmStatusTxt').textContent=txt;
}

async function tmConnect() {
  const email=document.getElementById('tmEmail').value.trim();
  const pass=document.getElementById('tmPass').value;
  if(!email||!pass){toast('Введи email и пароль','err');return;}
  const btn=document.getElementById('tmConnBtn');
  btn.classList.add('loading'); btn.disabled=true;
  tmSetStatus('busy','Подключение…'); tmLog('info','Подключение как '+email+'…');
  const proxy=tmGetProxy();
  if(proxy) tmLog('info','Прокси: '+proxy);
  try {
    const res=await fetch('/connect',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({email,password:pass,proxy})});
    const d=await res.json();
    if(!res.ok) throw new Error(d.error||'Ошибка');
    tmState.token=d.deviceToken;
    localStorage.setItem('tm_email',email);
    tmSetStatus('ok','Подключено — '+email);
    document.getElementById('tmAuthBadge').textContent='Подключено';
    document.getElementById('tmAuthBadge').className='badge on';
    document.getElementById('tmSendBtn').disabled=false;
    tmLog('ok','Подключено — токен '+d.deviceToken.slice(0,8)+'…');
    toast('Подключено!','ok');
    document.getElementById('tmAuthBody').hidden=true;
    document.getElementById('tmAuthToggle').setAttribute('aria-expanded','false');
  } catch(e) {
    tmSetStatus('err',e.message); tmLog('err',e.message); toast(e.message,'err');
  } finally { btn.classList.remove('loading'); btn.disabled=false; }
}

let bulkEvt = null;

async function tmSetupAll() {
  const btn = document.getElementById('tmSetupBtn');
  const prog = document.getElementById('bulkProgress');
  const bulkLog = document.getElementById('bulkLog');

  // Check if we have autoreg results
  const statusRes = await fetch('/api/status').then(r=>r.json()).catch(()=>({total:0}));
  const total = statusRes.total || 0;

  if (total === 0) {
    toast('Нет зарегистрированных аккаунтов в Autoreg', 'err');
    return;
  }

  btn.classList.add('loading'); btn.disabled = true;
  prog.hidden = false;
  bulkLog.innerHTML = '';
  document.getElementById('bulkDone').textContent = '0';
  document.getElementById('bulkFail').textContent = '0';
  document.getElementById('bulkTotal').textContent = total;
  document.getElementById('bulkDot').classList.remove('idle');

  tmLog('info', `Запуск Rule Chain на ${total} аккаунт(ов)…`);

  try {
    const res = await fetch('/setup-rules-all', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({proxy: tmGetProxy()})});
    const d = await res.json();
    if (!res.ok) throw new Error(d.error || 'Ошибка');

    // SSE stream
    if (bulkEvt) bulkEvt.close();
    bulkEvt = new EventSource('/setup-rules-all/log');
    bulkEvt.onmessage = e => {
      const item = JSON.parse(e.data);
      if (item.msg === '__BULK_DONE__') {
        bulkEvt.close();
        btn.classList.remove('loading'); btn.disabled = false;
        document.getElementById('bulkDot').classList.add('idle');
        const ok = parseInt(document.getElementById('bulkDone').textContent);
        toast(`Rule Chain: ${ok}/${total} успешно`, ok === total ? 'ok' : 'err');
        return;
      }
      // add line to bulk log
      const ln = document.createElement('div');
      ln.style.cssText = 'display:flex;gap:8px;align-items:baseline';
      const colors = {success:'var(--ok)',error:'var(--err)',warning:'var(--warn)',section:'var(--accent)',muted:'var(--muted)',info:'var(--dim)'};
      ln.innerHTML = `<span style="color:var(--muted);font-size:10px;flex-shrink:0">${item.ts}</span>`+
        `<span style="color:${colors[item.level]||'var(--dim)'};">${esc(item.msg)}</span>`;
      bulkLog.appendChild(ln);
      bulkLog.scrollTop = bulkLog.scrollHeight;
      tmLog(item.level === 'success' ? 'ok' : item.level === 'error' ? 'err' : 'info', item.msg);
      // update counters
      fetch('/setup-rules-all/status').then(r=>r.json()).then(s=>{
        document.getElementById('bulkDone').textContent = s.done;
        document.getElementById('bulkFail').textContent = s.failed;
        document.getElementById('bulkTotal').textContent = s.total;
      }).catch(()=>{});
    };
  } catch(e) {
    toast(e.message, 'err'); tmLog('err', e.message);
    btn.classList.remove('loading'); btn.disabled = false;
    document.getElementById('bulkDot').classList.add('idle');
  }
}

async function tmSend(e) {
  e.preventDefault();
  if(!tmState.token){toast('Сначала подключись','err');return;}
  const emails=tmParseEmails(document.getElementById('tmTo').value);
  const subj=document.getElementById('tmSubject').value.trim();
  const body=document.getElementById('tmBody').value.trim();
  if(!emails.length||!subj||!body){toast('Заполни все поля','err');return;}
  const recipients=emails.map(to=>{
    const id=genId();
    return {to,subject:subj.replace(/\{id\}/g,id),body:body.replace(/\{id\}/g,id)};
  });
  const proxy=tmGetProxy();
  tmLog('info',`Отправка ${recipients.length} письм${recipients.length===1?'а':'ем'}…`);
  if(proxy) tmLog('info','Прокси: '+proxy);
  const btn=document.getElementById('tmSendBtn');
  btn.classList.add('loading'); btn.disabled=true;
  try {
    const res=await fetch('/send',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({deviceToken:tmState.token,recipients,proxy})});
    const d=await res.json();
    if(!res.ok) throw new Error(d.error||'Ошибка');
    recipients.forEach(r=>{
      const hasErr=d.errors&&d.errors.some(x=>x.startsWith(r.to));
      tmLog(hasErr?'err':'ok',`${r.to} — тема: "${r.subject.slice(0,40)}"`);
    });
    const msg=d.errors?.length ? `Отправлено: ${d.sent}, ошибок: ${d.errors.length}` : `Отправлено ${d.sent} писем!`;
    toast(msg, d.errors?.length?'err':'ok');
    if(!d.errors?.length) tmLog('ok','Готово — '+d.sent+' писем');
  } catch(e) { tmLog('err',e.message); toast(e.message,'err'); }
  finally { btn.classList.remove('loading'); btn.disabled=false; }
}

function tmLog(type,msg) {
  const scroll=document.getElementById('tmLogScroll');
  const empty=document.getElementById('tmLogEmpty');
  if(empty) empty.remove();
  tmLogN++;
  document.getElementById('tmLogC').textContent=tmLogN;
  const icons={ok:'✓',err:'✕',info:'→',warn:'!'};
  const ts=new Date().toLocaleTimeString('ru',{hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit'});
  const el=document.createElement('div'); el.className='tm-log-entry';
  el.innerHTML=`<span class="tm-ts">${ts}</span><span class="tm-icon ${type}">${icons[type]||'·'}</span><span class="tm-msg">${esc(msg)}</span>`;
  scroll.appendChild(el); scroll.scrollTop=scroll.scrollHeight;
}
function tmClearLog() {
  document.getElementById('tmLogScroll').innerHTML='<div class="tm-empty" id="tmLogEmpty">Пока нет событий…</div>';
  tmLogN=0; document.getElementById('tmLogC').textContent='0';
}

// ══════════════════════════════════════════
//  CAMPAIGN
// ══════════════════════════════════════════
let campEvt = null;
let campPollIv = null;

function cCountRecip() {
  const emails = tmParseEmails(document.getElementById('cRecip').value);
  const b = document.getElementById('cRecipBadge');
  b.textContent = emails.length === 0 ? '0 адресов' : emails.length === 1 ? '1 адрес' : emails.length + ' адресов';
  b.className = 'email-badge ' + (emails.length > 0 ? 'some' : 'none');
}
function adjC(d) {
  const el = document.getElementById('cPerAcc');
  el.value = Math.min(500, Math.max(1, parseInt(el.value||10) + d));
}

function cAddLog(msg, level) {
  if (msg === '__CAMP_DONE__') return;
  const box = document.getElementById('cLog');
  const ln = document.createElement('div');
  ln.style.cssText = 'display:flex;gap:8px;align-items:baseline';
  const colors = {success:'var(--ok)',error:'var(--err)',warning:'var(--warn)',section:'var(--accent)',muted:'var(--muted)',info:'var(--dim)'};
  const ts = new Date().toLocaleTimeString('ru',{hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit'});
  ln.innerHTML = `<span style="color:var(--muted);font-size:10px;flex-shrink:0">${ts}</span>`+
    `<span style="color:${colors[level]||'var(--dim)'};">${esc(msg)}</span>`;
  box.appendChild(ln); box.scrollTop = box.scrollHeight;
}

function campPollStatus() {
  fetch('/campaign/status').then(r=>r.json()).then(s=>{
    document.getElementById('cAccLeft').textContent  = s.accounts_left;
    document.getElementById('cAccDone').textContent  = s.acc_done;
    document.getElementById('cEmailsSent').textContent = s.emails_sent;
    document.getElementById('cFail').textContent     = s.acc_fail;
  }).catch(()=>{});
}

async function campStart() {
  const recipients = document.getElementById('cRecip').value;
  const subject    = document.getElementById('cSubject').value.trim();
  const body       = document.getElementById('cBody').value.trim();
  const perAcc     = parseInt(document.getElementById('cPerAcc').value) || 10;

  if (!subject || !body) { toast('Заполни тему и сообщение', 'err'); return; }
  const rEmails = tmParseEmails(recipients);
  if (!rEmails.length) { toast('Добавь получателей', 'err'); return; }

  const statusRes = await fetch('/campaign/status').then(r=>r.json()).catch(()=>({accounts_left:0}));
  if (!statusRes.accounts_left) { toast('Нет аккаунтов в Autoreg', 'err'); return; }

  const btn = document.getElementById('cStartBtn');
  const stopBtn = document.getElementById('cStopBtn');
  btn.classList.add('loading'); btn.disabled = true;
  stopBtn.disabled = false;
  document.getElementById('cDot').classList.remove('idle');

  try {
    const res = await fetch('/campaign/start', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({recipients, subject, body, per_account: perAcc,
                            proxy: tmGetProxy()})});
    const d = await res.json();
    if (!res.ok) throw new Error(d.error || 'Ошибка');

    toast(`Кампания запущена: ${d.total} акк. · ${d.recipients} получателей`, 'ok');
    campPollStatus();
    campPollIv = setInterval(campPollStatus, 2000);

    if (campEvt) campEvt.close();
    campEvt = new EventSource('/campaign/log');
    campEvt.onmessage = e => {
      const d = JSON.parse(e.data);
      cAddLog(d.msg, d.level);
      if (d.msg === '__CAMP_DONE__') {
        campEvt.close(); clearInterval(campPollIv);
        campPollStatus();
        btn.classList.remove('loading'); btn.disabled = false;
        stopBtn.disabled = true;
        document.getElementById('cDot').classList.add('idle');
        toast('Кампания завершена!', 'ok');
      }
    };
  } catch(e) {
    toast(e.message, 'err');
    btn.classList.remove('loading'); btn.disabled = false;
    stopBtn.disabled = true;
    document.getElementById('cDot').classList.add('idle');
  }
}

async function campStop() {
  await fetch('/campaign/stop', {method:'POST'});
  toast('Остановка после текущего аккаунта…', 'ok');
}

// init campaign stats on tab switch
const _origSwitch = switchTab;
switchTab = name => {
  _origSwitch(name);
  if (name === 'campaign') campPollStatus();
};

// ══════════════════════════════════════════
//  PROXY
// ══════════════════════════════════════════
function proxyChanged() {
  const lines = document.getElementById('proxyArea').value.trim().split('\n').filter(l=>l.trim()&&!l.startsWith('#'));
  document.getElementById('proxyCount').textContent = lines.length + ' шт.';
}

async function loadProxies() {
  const raw = document.getElementById('proxyArea').value;
  try {
    const d = await fetch('/api/proxies', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({proxies: raw})}).then(r=>r.json());
    document.getElementById('proxyCount').textContent = d.count + ' шт.';
    document.getElementById('proxyStatus').textContent =
      d.count ? `Ротация: вкл · ${d.count} прокси` : 'Ротация: выкл';
    document.getElementById('proxyStatus').style.color = d.count ? 'var(--ok)' : 'var(--muted)';
    toast(d.count ? `Загружено ${d.count} прокси` : 'Прокси очищены', d.count ? 'ok' : 'err');
    addLog(d.count ? `Прокси загружены: ${d.count} шт.` : 'Список прокси пуст', d.count ? 'success' : 'warning');
  } catch(e) { toast('Ошибка загрузки прокси', 'err'); }
}

async function clearProxies() {
  document.getElementById('proxyArea').value = '';
  proxyChanged();
  await fetch('/api/proxies', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({proxies: ''})});
  document.getElementById('proxyStatus').textContent = 'Ротация: выкл';
  document.getElementById('proxyStatus').style.color = 'var(--muted)';
  toast('Прокси очищены', 'ok');
}

// ══════════════════════════════════════════
//  utils
// ══════════════════════════════════════════
function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
let toastTmr;
function toast(msg,type) {
  const t=document.getElementById('toast'); clearTimeout(toastTmr);
  t.textContent=(type==='ok'?'✓  ':'✕  ')+msg; t.className='show '+type;
  toastTmr=setTimeout(()=>{t.className='';},5000);
}
</script>
</body>
</html>"""

if __name__ == "__main__":
    print("\n  TB Suite -> http://localhost:5757\n")
    app.run(host="127.0.0.1", port=5757, threaded=True, debug=False)
