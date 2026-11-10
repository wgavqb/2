const DASHBOARD = 'http://127.0.0.1:5050';
const CS_HOST   = 'app.contasimple.com';

// ── Куки ─────────────────────────────────────────────────────────────────────

async function getAllCookies() {
  const cookies = await chrome.cookies.getAll({ domain: 'contasimple.com' });
  return cookies.map(c => `${c.name}=${c.value}`).join('; ');
}

async function sendCookies(cookieStr) {
  try {
    const r = await fetch(`${DASHBOARD}/api/auth/cookie`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cookies: cookieStr }),
    });
    return await r.json();
  } catch(e) { return { ok: false, error: e.message }; }
}

// ── Перехват сетевых запросов ContaSimple ─────────────────────────────────────

const _seen = new Set();

chrome.webRequest.onBeforeRequest.addListener(
  (details) => {
    // Только XHR/fetch запросы к app.contasimple.com
    if (details.type !== 'xmlhttprequest') return;
    const url = new URL(details.url);
    if (!url.hostname.includes(CS_HOST)) return;

    const key = `${details.method}:${url.pathname}`;
    if (_seen.has(key)) return;
    _seen.add(key);

    // Отправляем на наш сервер для анализа
    fetch(`${DASHBOARD}/api/sniff`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        method:   details.method,
        url:      details.url,
        path:     url.pathname,
        query:    url.search,
        type:     details.type,
      }),
    }).catch(() => {});
  },
  { urls: [`https://${CS_HOST}/*`] }
);

// ── Автосинхронизация при изменении куки ─────────────────────────────────────

chrome.cookies.onChanged.addListener(async ({ cookie, removed }) => {
  if (!cookie.domain.includes('contasimple.com')) return;
  const str = await getAllCookies();
  await sendCookies(str);
});

// ── Сообщения от popup ────────────────────────────────────────────────────────

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.action === 'sync') {
    getAllCookies()
      .then(str => sendCookies(str))
      .then(sendResponse);
    return true;
  }
  if (msg.action === 'get') {
    getAllCookies().then(str => sendResponse({ cookies: str }));
    return true;
  }
  if (msg.action === 'clear_seen') {
    _seen.clear();
    sendResponse({ ok: true });
    return true;
  }
});
