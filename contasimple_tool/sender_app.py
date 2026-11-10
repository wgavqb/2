"""
ContaSimple Quick Sender
Запуск: python sender_app.py  →  http://127.0.0.1:5050

Поток:
  1. Расширение Chrome отправляет куки ContaSimple → /api/auth/cookie
  2. Пользователь вставляет почты + текст → нажимает Отправить
  3. Для каждой почты: создаём клиента → создаём инвойс → шлём email
"""
import os, re, random, string, datetime, time, json, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse, parse_qs, quote_plus
from pathlib import Path

from flask import Flask, request, jsonify, render_template_string, make_response
import requests
from bs4 import BeautifulSoup

BASE_URL  = 'https://app.contasimple.com'
COOKIE_FILE = Path(__file__).parent / '.cookies.json'

SESSION   = requests.Session()
SESSION.trust_env = False
STOP_FLAG = threading.Event()
PROGRESS  = {'done': 0, 'total': 0, 'log': [], 'running': False}
SESSION.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9,es;q=0.8',
    'Accept-Encoding': 'gzip, deflate, br',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'same-origin',
    'Sec-Fetch-User': '?1',
})

def save_cookies(cookie_str):
    COOKIE_FILE.write_text(json.dumps({'cookies': cookie_str}), encoding='utf-8')

def load_cookies():
    if COOKIE_FILE.exists():
        try:
            data = json.loads(COOKIE_FILE.read_text(encoding='utf-8'))
            cookie_str = data.get('cookies', '')
            if cookie_str:
                SESSION.cookies.clear()
                for part in cookie_str.split('; '):
                    if '=' in part:
                        name, _, value = part.partition('=')
                        SESSION.cookies.set(name.strip(), value.strip(), domain='.contasimple.com')
                return True
        except Exception:
            pass
    return False

load_cookies()

app = Flask(__name__)

@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['Access-Control-Allow-Methods'] = 'GET,POST,OPTIONS'
    return response

@app.before_request
def handle_options():
    if request.method == 'OPTIONS':
        resp = make_response('', 204)
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        resp.headers['Access-Control-Allow-Methods'] = 'GET,POST,OPTIONS'
        return resp

CONFIG = {
    'capsolver_key': '',
    'email': '',
    'password': '',
    'proxy': '',
}

def apply_proxy(proxy_url=''):
    """Применяем прокси к SESSION. Формат: protocol://user:pass@host:port"""
    proxy_url = proxy_url.strip()
    if not proxy_url:
        SESSION.proxies.clear()
    else:
        if '://' not in proxy_url:
            proxy_url = 'http://' + proxy_url
        SESSION.proxies = {'http': proxy_url, 'https': proxy_url}
    API_TOKENS.update({'state_token': None, 'company_id': None, 'user_id': None, 'period': None, 'reply_to': None})

# ── CapSolver ─────────────────────────────────────────────────────────────────

def solve_image_captcha(image_b64):
    """Решаем image captcha через CapSolver."""
    key = CONFIG.get('capsolver_key', '')
    if not key:
        return ''
    try:
        r = requests.post('https://api.capsolver.com/createTask', json={
            'clientKey': key,
            'task': {'type': 'ImageToTextTask', 'body': image_b64}
        }, timeout=60)
        task_id = r.json().get('taskId')
        if not task_id:
            return ''
        for _ in range(20):
            time.sleep(2)
            res = requests.post('https://api.capsolver.com/getTaskResult', json={
                'clientKey': key, 'taskId': task_id
            }, timeout=10)
            data = res.json()
            if data.get('status') == 'ready':
                return data.get('solution', {}).get('text', '')
        return ''
    except Exception:
        return ''

def get_captcha_answer(soup):
    """Находим и решаем капчу если она есть."""
    # Ищем image captcha
    img = soup.find('img', {'id': re.compile(r'[Cc]aptcha', re.I)})
    if not img:
        img = soup.find('img', {'src': re.compile(r'captcha', re.I)})
    if img:
        src = img.get('src', '')
        if src.startswith('/'):
            src = urljoin(BASE_URL, src)
        try:
            r = SESSION.get(src)
            import base64
            b64 = base64.b64encode(r.content).decode()
            return solve_image_captcha(b64)
        except Exception:
            return ''
    # Простой math captcha
    captcha_label = soup.find(string=re.compile(r'\d+\s*[+\-*×]\s*\d+'))
    if captcha_label:
        m = re.search(r'(\d+)\s*([+\-*×])\s*(\d+)', str(captcha_label))
        if m:
            a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
            if op == '+': return str(a + b)
            if op == '-': return str(a - b)
            if op in ('*', '×'): return str(a * b)
    return ''

# ── Page helpers ──────────────────────────────────────────────────────────────

def ensure_auth():
    """Проверяем авторизацию, если нет — пробуем auto-login."""
    r = SESSION.get(urljoin(BASE_URL, '/Modulos/Default.aspx'),
                    allow_redirects=False, timeout=8)
    if r.status_code == 200:
        return True
    email = CONFIG.get('email', '')
    password = CONFIG.get('password', '')
    if email and password:
        ok, err = do_login(email, password)
        if ok:
            return True
        raise Exception(f'Авто-логин не удался: {err}')
    raise Exception('Сессия истекла. Войдите заново или синхронизируйте куки.')

def get_page(path):
    url = urljoin(BASE_URL, path)
    r = SESSION.get(url, allow_redirects=False, timeout=60)
    if r.status_code in (301, 302) and 'login' in r.headers.get('Location', '').lower():
        ensure_auth()
        r = SESSION.get(url, allow_redirects=True, timeout=60)
    elif r.status_code in (301, 302):
        r = SESSION.get(url, allow_redirects=True, timeout=60)
    return BeautifulSoup(r.text, 'html.parser'), r

def get_form_action(html_text, page_url):
    """Извлекаем action формы из сырого HTML (BeautifulSoup портит &copy → ©)."""
    import html as html_mod
    m = re.search(r'<form[^>]+action="([^"]*)"', html_text, re.I)
    if m:
        raw_action = html_mod.unescape(m.group(1))
        return urljoin(page_url, raw_action)
    return page_url

def extract_form_fields(soup):
    """Извлекаем все поля формы (viewstate + visible). Пропускаем disabled, чекбоксы = on/skip."""
    fields = {}
    for inp in soup.find_all('input'):
        name = inp.get('name', '')
        if not name or 'disabled' in inp.attrs:
            continue
        itype = (inp.get('type', '') or '').lower()
        if itype == 'checkbox':
            if 'checked' in inp.attrs:
                fields[name] = inp.get('value', '') or 'on'
        elif itype != 'radio' or 'checked' in inp.attrs:
            fields[name] = inp.get('value', '')
    for sel in soup.find_all('select'):
        name = sel.get('name', '')
        if not name or 'disabled' in sel.attrs:
            continue
        opt = sel.find('option', selected=True)
        if opt:
            fields[name] = opt.get('value', '')
        else:
            first = sel.find('option')
            fields[name] = first.get('value', '') if first else ''
    for ta in soup.find_all('textarea'):
        name = ta.get('name', '')
        if name and 'disabled' not in ta.attrs:
            fields[name] = ta.get_text()
    return fields

def encode_form(data):
    """Standard URL-encode form data (browser-compatible %24 for $)."""
    pairs = []
    for k, v in data.items():
        pairs.append(quote_plus(str(k)) + '=' + quote_plus(str(v)))
    return '&'.join(pairs)

def post_form(url, data, referer=None, _skip_auth_check=False):
    if not url.startswith('http'):
        url = urljoin(BASE_URL, url)
    body = encode_form(data)
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Referer': referer or url,
        'Origin': BASE_URL,
    }
    r = SESSION.post(url, data=body.encode('utf-8'), headers=headers,
                     allow_redirects=True, timeout=60)
    if not _skip_auth_check and 'login' in r.url.lower() and '__OPERATION=norights' in r.url:
        ensure_auth()
        r = SESSION.post(url, data=body.encode('utf-8'), headers=headers,
                         allow_redirects=True, timeout=60)
    return BeautifulSoup(r.text, 'html.parser'), r

# ── Random data ───────────────────────────────────────────────────────────────

def r_nif():
    return str(random.randint(10000000, 99999999)) + random.choice('TRWAGMYFPDXBNJZSQVHLCKE')

def r_company(email):
    domain = email.split('@')[1].split('.')[0].capitalize()
    suffix = random.choice(['S.L.', 'S.A.', 'Corp.', 'Ltd.'])
    return f"{domain} {suffix}"

def r_amount():
    return f"{random.uniform(80, 1500):.2f}"

# ── ContaSimple automation ────────────────────────────────────────────────────

def do_login(email, password):
    """Логин через email/пароль + CapSolver для капчи."""
    import base64
    SESSION.cookies.clear()
    login_url = urljoin(BASE_URL, '/login.aspx')
    r = SESSION.get(login_url, allow_redirects=True, timeout=60)
    soup = BeautifulSoup(r.text, 'html.parser')
    fields = extract_form_fields(soup)

    fields['Username'] = email
    fields['Password'] = password

    captcha_img = soup.find('img', {'src': re.compile(r'[Cc]aptcha')})
    if captcha_img:
        img_src = captcha_img.get('src', '')
        if img_src.startswith('/'):
            img_src = urljoin(BASE_URL, img_src)
        img_r = SESSION.get(img_src, timeout=15)
        b64 = base64.b64encode(img_r.content).decode()
        answer = solve_image_captcha(b64)
        if not answer:
            return False, 'Капча не решена — проверь CapSolver API ключ'
        fields['CaptchaAnswer'] = answer

    fields['ImageButtonLogin.x'] = '100'
    fields['ImageButtonLogin.y'] = '15'

    form_url = get_form_action(r.text, r.url)
    soup2, r2 = post_form(form_url, fields, referer=login_url, _skip_auth_check=True)

    if '/Modulos/' in r2.url and 'login' not in r2.url.lower():
        cookie_str = '; '.join(f'{c.name}={c.value}' for c in SESSION.cookies)
        save_cookies(cookie_str)
        return True, None

    err = soup2.find(class_=re.compile(r'error|alert', re.I))
    err_text = err.get_text(strip=True) if err else 'Логин не удался'
    return False, err_text

API_TOKENS = {'state_token': None, 'company_id': None, 'user_id': None, 'period': None, 'reply_to': None}

def refresh_api_tokens():
    """Загружаем токены для API из любой страницы ContaSimple."""
    _, r = get_page('/Modulos/Contabilidad/RegistroDeFacturasEmitidas/NuevaFactura.aspx')
    m = re.search(r'stateToken\s*[:=]\s*["\']([^"\']+)', r.text)
    if m: API_TOKENS['state_token'] = m.group(1)
    m = re.search(r'companyinfo\.id\s*[:=]\s*["\']?(\d+)', r.text)
    if m: API_TOKENS['company_id'] = m.group(1)
    m = re.search(r'userinfo\.id\s*[:=]\s*["\']?(\d+)', r.text)
    if m: API_TOKENS['user_id'] = m.group(1)
    m = re.search(r'ExercisePeriod_RealValue.*?value="([^"]+)"', r.text)
    if m: API_TOKENS['period'] = m.group(1)
    m = re.search(r'userinfo\.Email\s*[:=]\s*["\']([^"\']+)', r.text)
    if m: API_TOKENS['reply_to'] = m.group(1)
    return all(API_TOKENS[k] for k in ['state_token', 'company_id', 'user_id', 'period'])

def cs_api(method, service_url, body=None, _retry=2):
    """Вызов внутреннего API ContaSimple через прокси."""
    if not API_TOKENS['state_token']:
        refresh_api_tokens()
    req = {'StateToken': API_TOKENS['state_token'], 'Method': method, 'Service': service_url}
    if body is not None:
        req['Body'] = body
    headers = {
        'Content-Type': 'application/json; charset=utf-8',
        'Current-Company-Id': API_TOKENS['company_id'],
        'Current-User-Id': API_TOKENS['user_id'],
    }
    for attempt in range(_retry):
        try:
            r = SESSION.post(BASE_URL + '/Services/ApiService/ApiService.asmx/Call',
                             data=json.dumps({'request': req}), headers=headers, timeout=60)
            if r.status_code == 401 or 'InvalidToken' in r.text:
                refresh_api_tokens()
                req['StateToken'] = API_TOKENS['state_token']
                r = SESSION.post(BASE_URL + '/Services/ApiService/ApiService.asmx/Call',
                                 data=json.dumps({'request': req}), headers=headers, timeout=60)
            return r.status_code, r.json()
        except requests.exceptions.Timeout:
            if attempt < _retry - 1:
                time.sleep(2)
                continue
            raise
        except requests.exceptions.ConnectionError:
            if attempt < _retry - 1:
                time.sleep(3)
                continue
            raise

def create_customer(email):
    """Создаём клиента через API. Возвращает entity_id."""
    nif = r_nif()
    code, resp = cs_api('POST', '/entities/customers', {
        'type': 'Target', 'organization': r_company(email), 'nif': nif,
        'email': email, 'countryId': 231,
        'phone': '+34' + str(random.randint(600000000, 699999999)),
    })
    if code == 200 and 'data' in resp:
        return resp['data']['id']
    raise Exception(f"Клиент не создан: {resp.get('errorMessage', str(resp))[:200]}")

def create_invoice(entity_id, description):
    """Создаём инвойс через API. Возвращает invoice_id."""
    period = API_TOKENS.get('period') or '2026-3T'
    code, resp = cs_api('GET', f'/accounting/{period}/invoices/issued/nextInvoiceNumber?type=normal')
    inv_number = resp.get('data', f'AUTO-{random.randint(1000,9999)}')

    code, resp = cs_api('POST', f'/accounting/{period}/invoices/issued', {
        'TargetEntityId': entity_id,
        'Date': datetime.datetime.now().strftime('%Y-%m-%dT00:00:00'),
        'InvoiceClass': 700,
        'OperationType': 'Nacional',
        'Number': inv_number,
        'Lines': [{'Concept': description or 'Professional services',
                    'UnitPrice': float(r_amount()), 'Quantity': 1, 'TaxPercentage': 0}],
    })
    if code == 200 and 'data' in resp:
        return resp['data']['id']
    raise Exception(f"Инвойс не создан: {resp.get('errorMessage', str(resp))[:200]}")

def to_html(text):
    """Конвертирует plain text в HTML, сохраняя переносы строк."""
    if re.search(r'<[a-z][\s\S]*?>', text, re.I):
        return text  # уже HTML
    import html as html_mod
    # нормализуем переносы строк (Windows \r\n → \n)
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    lines = text.split('\n')
    html_lines = []
    for line in lines:
        escaped = html_mod.escape(line)
        html_lines.append(escaped if escaped.strip() else '&nbsp;')
    body = '<br>\n'.join(html_lines)
    return f'<div style="font-family:sans-serif;font-size:14px;line-height:1.8">{body}</div>'

def send_invoice(invoice_id, recipient_email, subject, message):
    """Отправляем инвойс по email через API."""
    period = API_TOKENS.get('period') or '2026-3T'
    reply_to = API_TOKENS.get('reply_to') or 'mard.aws.on7.9.2@gmail.com'
    code, resp = cs_api('POST', f'/accounting/{period}/invoices/issued/{invoice_id}/send', {
        'To': recipient_email,
        'ReplyTo': reply_to,
        'Subject': subject,
        'Body': to_html(message),
        'SendPdf': True,
        'SendCopyToMe': False,
    })
    if code == 200 and resp.get('data') is True:
        return True, None
    return False, resp.get('errorMessage', str(resp))[:200]

# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route('/api/auth/cookie_get')
def auth_cookie_get():
    cookie_str = request.args.get('c', '')
    if not cookie_str:
        return '', 204
    SESSION.cookies.clear()
    for part in cookie_str.split('; '):
        if '=' in part:
            name, _, value = part.partition('=')
            SESSION.cookies.set(name.strip(), value.strip(), domain='.contasimple.com')
    save_cookies(cookie_str)
    return '', 204

@app.route('/api/auth/cookie', methods=['POST'])
def auth_cookie():
    data = request.get_json() or {}
    cookie_str = data.get('cookies', '')
    if not cookie_str:
        return jsonify({'ok': False, 'error': 'No cookies provided'})

    SESSION.cookies.clear()
    for part in cookie_str.split('; '):
        if '=' in part:
            name, _, value = part.partition('=')
            SESSION.cookies.set(name.strip(), value.strip(), domain='.contasimple.com')

    save_cookies(cookie_str)
    return jsonify({'ok': True, 'count': len(SESSION.cookies)})

@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    data = request.get_json() or {}
    email = data.get('email', '')
    password = data.get('password', '')
    if not email or not password:
        return jsonify({'ok': False, 'error': 'Email и пароль обязательны'})
    try:
        ok, err = do_login(email, password)
        if ok:
            CONFIG['email'] = email
            CONFIG['password'] = password
        return jsonify({'ok': ok, 'error': err})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})

@app.route('/api/auth/status')
def auth_status():
    try:
        r = SESSION.get(urljoin(BASE_URL, '/Modulos/Default.aspx'),
                        allow_redirects=False, timeout=8)
        # 200 = logged in; 302 to login page = not logged in
        ok = r.status_code == 200
        return jsonify({'ok': ok, 'status': r.status_code})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})

@app.route('/api/stop', methods=['POST'])
def stop_sending():
    STOP_FLAG.set()
    return jsonify({'ok': True})

@app.route('/api/progress')
def get_progress():
    return jsonify(PROGRESS)

@app.route('/api/config', methods=['POST'])
def update_config():
    data = request.get_json() or {}
    if 'capsolver_key' in data:
        CONFIG['capsolver_key'] = data['capsolver_key']
    if 'proxy' in data:
        CONFIG['proxy'] = data['proxy']
        apply_proxy(data['proxy'])
    return jsonify({'ok': True})

@app.route('/api/debug/customer', methods=['POST'])
def debug_customer():
    """Debug endpoint: try creating a customer and return raw info."""
    path = '/Modulos/Entidades/NuevoCliente.aspx'
    soup, r = get_page(path)
    fields = extract_form_fields(soup)
    prefix = 'ctl00$ctl00$MainContentMP$MainContent$EntityControl$entityInfo$'
    fields[prefix + 'txtNif']     = '12345678Z'
    fields[prefix + 'txtCompany'] = 'Debug Company SL'
    fields[prefix + 'txtEmail']   = 'debug@example.com'
    fields[prefix + 'txtPhone']   = '+34612345678'
    fields.pop('ctl00$ctl00$GeneralActionsPlaceHolder$ActionsPlaceHolder$List', None)
    fields.pop('search_autocomplete', None)
    fields['ctl00$ctl00$GeneralActionsPlaceHolder$ActionsPlaceHolder$AcceptButton'] = 'Create'

    action_url = get_form_action(r.text, r.url)
    soup2, r2 = post_form(action_url, fields, referer=r.url)

    # Find all validation messages
    all_errs = []
    for el in soup2.find_all(True):
        cls = ' '.join(el.get('class', []))
        txt = el.get_text(strip=True)
        if txt and any(x in cls.lower() for x in ['error','alert','valid','warning','msg']):
            all_errs.append({'class': cls, 'text': txt[:200]})

    return jsonify({
        'get_url': r.url,
        'get_status': r.status_code,
        'field_count': len(fields),
        'has_nif_field': (prefix + 'txtNif') in fields,
        'post_url': r2.url,
        'post_status': r2.status_code,
        'errors': all_errs[:10],
        'page_title': soup2.title.string if soup2.title else None,
        'page_snippet': r2.text[500:1500] if len(r2.text) > 500 else r2.text,
    })

@app.route('/api/sniff', methods=['POST'])
def sniff():
    return jsonify({'ok': True})

@app.route('/api/sniff/log')
def sniff_log():
    return jsonify({'ok': True, 'entries': []})

@app.route('/send', methods=['POST'])
def send_all():
    data = request.get_json() or {}
    emails_raw  = data.get('emails', '')
    subject     = data.get('subject', '').strip()
    message     = data.get('message', '').strip()
    description = data.get('description', '').strip() or 'Professional services'

    if not subject:
        subject = f"Invoice - {datetime.date.today().strftime('%B %Y')}"
    if not message:
        message = f"Find attached the invoice for services rendered.\n\nBest regards,"

    emails = [e.strip() for e in emails_raw.replace(',', '\n').split('\n') if '@' in e.strip()]
    if not emails:
        return jsonify({'ok': False, 'error': 'Нет валидных email'})

    THREADS = min(5, len(emails))  # до 5 параллельных потоков

    STOP_FLAG.clear()
    PROGRESS_LOCK = threading.Lock()
    PROGRESS.update({'done': 0, 'total': len(emails), 'log': [''] * len(emails), 'running': True})
    results = [None] * len(emails)

    def process_one(idx, email):
        if STOP_FLAG.is_set():
            with PROGRESS_LOCK:
                PROGRESS['log'][idx] = f'⛔ Остановлено на {email}'
                PROGRESS['done'] += 1
            return {'email': email, 'ok': False, 'error': 'Остановлено'}
        with PROGRESS_LOCK:
            PROGRESS['log'][idx] = f'⏳ [{idx+1}/{len(emails)}] {email}...'
        try:
            random_id = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
            e_subject = subject.replace('{random_id}', random_id)
            e_message = message.replace('{random_id}', random_id)
            e_description = description.replace('{random_id}', random_id)
            entity_id  = create_customer(email)
            invoice_id = create_invoice(entity_id, e_description)
            ok, err    = send_invoice(invoice_id, email, e_subject, e_message)
            with PROGRESS_LOCK:
                PROGRESS['log'][idx] = f'{"✅" if ok else "❌"} [{idx+1}/{len(emails)}] {email}' + (f' — {err}' if err else '')
                PROGRESS['done'] += 1
            return {'email': email, 'ok': ok, 'invoice_id': invoice_id, 'error': err}
        except Exception as e:
            with PROGRESS_LOCK:
                PROGRESS['log'][idx] = f'❌ [{idx+1}/{len(emails)}] {email} — {str(e)[:80]}'
                PROGRESS['done'] += 1
            return {'email': email, 'ok': False, 'error': str(e)}

    with ThreadPoolExecutor(max_workers=THREADS) as executor:
        futures = {executor.submit(process_one, i, email): i for i, email in enumerate(emails)}
        for future in as_completed(futures):
            idx = futures[future]
            results[idx] = future.result()

    PROGRESS['running'] = False
    total_ok = sum(1 for r in results if r and r['ok'])
    return jsonify({'ok': True, 'results': results, 'sent': total_ok, 'total': len(results)})

# ── UI ────────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template_string(HTML_UI)

HTML_UI = r'''<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ContaSimple Sender</title>
<style>
:root{--bg:#080810;--card:#0e0e1a;--border:#1e1e3a;--accent:#6366f1;--accent2:#a78bfa;--text:#dde1f0;--muted:#606880;--ok-bg:#061a0e;--ok-br:#166534;--ok-txt:#86efac;--err-bg:#1a0707;--err-br:#991b1b;--err-txt:#fca5a5;--info-bg:#06101e;--info-br:#1e40af;--info-txt:#93c5fd}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh;display:flex;flex-direction:column;align-items:center;padding:48px 16px}
h1{font-size:2.2rem;font-weight:700;background:linear-gradient(135deg,var(--accent),var(--accent2));-webkit-background-clip:text;-webkit-text-fill-color:transparent;letter-spacing:-.02em;margin-bottom:6px}
.sub{color:var(--muted);font-size:.9rem;margin-bottom:36px}
.card{background:var(--card);border:1px solid var(--border);border-radius:20px;padding:32px 36px;width:100%;max-width:660px;box-shadow:0 20px 60px #00000060}
.conn{display:flex;align-items:center;gap:10px;padding:10px 14px;background:#0a0a18;border:1px solid var(--border);border-radius:10px;margin-bottom:24px;font-size:.85rem;color:var(--muted)}
.dot{width:9px;height:9px;border-radius:50%;background:#ef4444;flex-shrink:0;transition:background .3s}
.dot.on{background:#22c55e}
label{display:block;font-size:.75rem;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);margin:20px 0 8px}
textarea{width:100%;background:#07070f;border:1.5px solid var(--border);border-radius:10px;color:var(--text);font-size:.95rem;padding:12px 14px;resize:vertical;outline:none;transition:border .2s;font-family:inherit}
textarea:focus{border-color:var(--accent)}
#emails{height:110px}
#desc{height:80px}
.row{display:flex;gap:12px;margin-top:20px}
.row input{flex:1;background:#07070f;border:1.5px solid var(--border);border-radius:10px;color:var(--text);font-size:.88rem;padding:10px 14px;outline:none;transition:border .2s}
.row input:focus{border-color:var(--accent)}
.row input::placeholder{color:var(--muted)}
button{margin-top:24px;width:100%;padding:15px;background:linear-gradient(135deg,var(--accent),var(--accent2));border:none;border-radius:12px;color:#fff;font-size:1rem;font-weight:700;cursor:pointer;transition:opacity .2s,transform .1s;letter-spacing:.02em}
button:hover{opacity:.9}
button:active{transform:scale(.98)}
button:disabled{opacity:.35;cursor:not-allowed}
#status{margin-top:18px;padding:13px 16px;border-radius:10px;font-size:.88rem;display:none;line-height:1.5}
.ok-s{background:var(--ok-bg);border:1px solid var(--ok-br);color:var(--ok-txt)}
.err-s{background:var(--err-bg);border:1px solid var(--err-br);color:var(--err-txt)}
.info-s{background:var(--info-bg);border:1px solid var(--info-br);color:var(--info-txt)}
.results{margin-top:14px;display:flex;flex-direction:column;gap:6px}
.rr{padding:9px 14px;border-radius:8px;font-size:.83rem}
.rr.ok-s{background:var(--ok-bg);border:1px solid var(--ok-br);color:var(--ok-txt)}
.rr.err-s{background:var(--err-bg);border:1px solid var(--err-br);color:var(--err-txt)}
#log-box{margin-top:14px;background:#07070f;border:1px solid var(--border);border-radius:10px;padding:12px 14px;font-size:.82rem;font-family:monospace;max-height:220px;overflow-y:auto;display:none;line-height:1.7}
#log-box .log-line{color:var(--muted)}
#log-box .log-line.ok{color:var(--ok-txt)}
#log-box .log-line.err{color:var(--err-txt)}
#log-box .log-line.wait{color:var(--info-txt)}
</style>
</head>
<body>
<h1>ContaSimple Sender</h1>
<p class="sub">Клиент → Инвойс → Email через ContaSimple</p>
<div class="card">
  <div class="conn"><div class="dot" id="dot"></div><span id="conn-txt">Проверка...</span></div>

  <div id="login-section" style="display:none">
    <label>EMAIL CONTASIMPLE</label>
    <div class="row" style="margin-top:0">
      <input id="cs-email" type="email" placeholder="your@email.com">
    </div>
    <label>ПАРОЛЬ</label>
    <div class="row" style="margin-top:0">
      <input id="cs-pass" type="password" placeholder="Пароль ContaSimple">
    </div>
    <button id="btn-login" onclick="doLogin()" style="margin-top:12px;font-size:.9rem;padding:12px">Войти</button>
  </div>

  <div id="send-section">
    <label>EMAILS (по одному на строку)</label>
    <textarea id="emails" placeholder="client@example.com&#10;another@company.com"></textarea>

    <label>ОПИСАНИЕ УСЛУГ (в инвойсе)</label>
    <textarea id="desc" placeholder="Professional consulting services..."></textarea>

    <label>ТЕМА ПИСЬМА <span style="color:var(--accent2);font-size:.7rem;text-transform:none;letter-spacing:0">— используй {random_id} для уникального кода (6 симв.)</span></label>
    <textarea id="subj" style="height:50px" placeholder="Invoice {random_id} - September 2026"></textarea>

    <label>ТЕКСТ ПИСЬМА <span style="color:var(--accent2);font-size:.7rem;text-transform:none;letter-spacing:0">— {random_id} заменится на случайный код</span></label>
    <textarea id="msg" placeholder="Find attached the invoice {random_id} for services rendered.&#10;&#10;Best regards,"></textarea>

    <div class="row">
      <input id="capsolver" type="password" placeholder="CapSolver API Key (если есть капча)">
    </div>
    <div class="row">
      <input id="proxy" type="text" placeholder="Прокси: socks5://user:pass@host:port">
    </div>

    <div style="display:flex;gap:12px;margin-top:24px">
      <button id="btn" onclick="doSend()" style="margin-top:0;flex:1">Отправить инвойсы</button>
      <button id="btn-stop" onclick="doStop()" style="margin-top:0;width:120px;background:linear-gradient(135deg,#ef4444,#dc2626);opacity:.3;cursor:not-allowed" disabled>Стоп</button>
    </div>
  </div>
  <div id="status"></div>
  <div id="log-box" style="margin-top:14px;background:#07070f;border:1px solid var(--border);border-radius:10px;padding:12px 14px;font-size:.82rem;font-family:monospace;max-height:220px;overflow-y:auto;line-height:1.7;min-height:44px;color:var(--muted)">Логи появятся здесь...</div>
  <div class="results" id="results"></div>
</div>

<script>
const $ = id => document.getElementById(id);

async function checkConn() {
  try {
    const j = await fetch('/api/auth/status').then(r=>r.json());
    if (j.ok) {
      $('dot').classList.add('on');
      $('conn-txt').textContent='Подключено к ContaSimple';
      $('login-section').style.display='none';
      $('send-section').style.display='block';
    } else {
      $('conn-txt').textContent='Не авторизован — войди ниже или синхронизируй куки';
      $('login-section').style.display='block';
    }
  } catch { $('conn-txt').textContent='Сервер не отвечает'; }
}

async function doLogin() {
  const btn = $('btn-login'), st = $('status');
  btn.disabled = true;
  st.style.display='block'; st.className='info-s'; st.textContent='Вхожу...';

  // Сначала сохраняем CapSolver ключ
  const csKey = $('capsolver').value;
  if (csKey) await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({capsolver_key:csKey})});

  try {
    const j = await fetch('/api/auth/login',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({email:$('cs-email').value, password:$('cs-pass').value})
    }).then(r=>r.json());
    if (j.ok) {
      st.className='ok-s'; st.textContent='Вошел успешно!';
      $('dot').classList.add('on');
      $('conn-txt').textContent='Подключено к ContaSimple';
      $('login-section').style.display='none';
      localStorage.setItem('cs_email',$('cs-email').value);
    } else {
      st.className='err-s'; st.textContent='Ошибка: '+(j.error||'не удалось');
    }
  } catch(e) { st.className='err-s'; st.textContent='Ошибка: '+e.message; }
  btn.disabled = false;
}

let pollTimer = null;

async function doStop() {
  await fetch('/api/stop',{method:'POST'});
  $('status').textContent='Останавливаю...';
}

function startPolling() {
  const lb = $('log-box');
  lb.style.display='block';
  pollTimer = setInterval(async () => {
    try {
      const p = await fetch('/api/progress').then(r=>r.json());
      lb.innerHTML = p.log.map(line => {
        const cls = line.startsWith('✅') ? 'ok' : line.startsWith('❌') ? 'err' : 'wait';
        return `<div class="log-line ${cls}">${line}</div>`;
      }).join('');
      lb.scrollTop = lb.scrollHeight;
      $('status').textContent = `Отправка: ${p.done}/${p.total}...`;
      if (!p.running) stopPolling();
    } catch {}
  }, 600);
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

async function doSend() {
  const btn = $('btn'), st = $('status'), res = $('results'), stopBtn = $('btn-stop');
  btn.disabled = true;
  stopBtn.disabled = false; stopBtn.style.opacity = '1'; stopBtn.style.cursor = 'pointer';
  st.style.display='block'; st.className='info-s'; st.textContent='Запускаю...';
  res.innerHTML = '';
  $('log-box').innerHTML = '';

  startPolling();
  try {
    const j = await fetch('/send',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({emails:$('emails').value, description:$('desc').value, subject:$('subj').value, message:$('msg').value})
    }).then(r=>r.json());

    stopPolling();
    if (j.ok) {
      const ok = j.results.filter(x=>x.ok).length;
      st.className = ok===j.total?'ok-s':'err-s';
      st.textContent = `Готово: ${ok} из ${j.total} отправлено`;
    } else {
      st.className='err-s'; st.textContent=j.error||'Ошибка';
    }
  } catch(e) { stopPolling(); st.className='err-s'; st.textContent='Ошибка: '+e.message; }
  btn.disabled = false;
  stopBtn.disabled = true; stopBtn.style.opacity = '.3'; stopBtn.style.cursor = 'not-allowed';
}

// Восстанавливаем сохранённые данные
const savedKey = localStorage.getItem('capsolver_key');
if (savedKey) { $('capsolver').value = savedKey; fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({capsolver_key:savedKey})}); }
$('capsolver').addEventListener('change', e => { localStorage.setItem('capsolver_key', e.target.value); fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({capsolver_key:e.target.value})}); });
const savedProxy = localStorage.getItem('proxy');
if (savedProxy) { $('proxy').value = savedProxy; fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({proxy:savedProxy})}); }
$('proxy').addEventListener('change', e => { localStorage.setItem('proxy', e.target.value); fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({proxy:e.target.value})}); });
const savedEmail = localStorage.getItem('cs_email');
if (savedEmail) $('cs-email').value = savedEmail;

checkConn();
</script>
</body>
</html>
'''

if __name__ == '__main__':
    print("ContaSimple Sender -> http://127.0.0.1:5050")
    app.run(port=5050, debug=False, use_reloader=False, threaded=True)
