const DASHBOARD = 'http://127.0.0.1:5050';

async function checkDashboard() {
  const dot  = document.getElementById('dash-dot');
  const text = document.getElementById('dash-text');
  const sub  = document.getElementById('dash-sub');
  try {
    const r = await fetch(`${DASHBOARD}/api/auth/status`, { signal: AbortSignal.timeout(2000) });
    const d = await r.json();
    dot.className  = 'dot ok';
    text.textContent = d.mode
      ? `Conectado (${d.mode === 'cookie' ? '🍪 Cookies' : '🔐 OAuth'})`
      : 'Dashboard activo — sin autenticar';
    sub.textContent = 'http://127.0.0.1:5050';
    return true;
  } catch {
    dot.className  = 'dot err';
    text.textContent = 'Dashboard no disponible';
    sub.textContent  = 'Inicia python app.py primero';
    return false;
  }
}

async function syncNow() {
  const btn    = document.getElementById('btn-sync');
  const result = document.getElementById('result-area');
  btn.disabled = true;
  btn.innerHTML = '<span style="animation:spin .6s linear infinite;display:inline-block">⟳</span> Sincronizando...';

  // Obtener cookies via background
  const { cookies } = await chrome.runtime.sendMessage({ action: 'get' });
  const count = cookies ? cookies.split(';').length : 0;

  if (!cookies) {
    showResult('error', '❌ No hay cookies de ContaSimple. ¿Has iniciado sesión en app.contasimple.com?');
    btn.disabled = false; btn.innerHTML = '🔄 Sincronizar Cookies Ahora'; return;
  }

  const r = await chrome.runtime.sendMessage({ action: 'sync' });

  if (r && r.ok) {
    showResult('ok', `✓ ${count} cookies sincronizadas correctamente`);
    dot('ok');
    await checkDashboard();
  } else {
    const err = r?.error || 'Error desconocido';
    if (err.includes('fetch') || err.includes('ECONNREFUSED') || err.includes('Failed')) {
      showResult('error', '❌ Dashboard no responde. Inicia python app.py primero.');
    } else {
      showResult('warn', '⚠️ ' + err);
    }
  }

  btn.disabled = false;
  btn.innerHTML = '🔄 Sincronizar Cookies Ahora';
}

function showResult(type, msg) {
  const colors = { ok: '#22c55e', error: '#ef4444', warn: '#f59e0b' };
  const bgs    = { ok: '#22c55e15', error: '#ef444415', warn: '#f59e0b15' };
  document.getElementById('result-area').innerHTML = `
    <div style="margin-top:10px;padding:10px 14px;border-radius:8px;
      background:${bgs[type]};border:1px solid ${colors[type]}30;
      font-size:12px;color:${colors[type]};font-weight:500;line-height:1.5">
      ${msg}
    </div>`;
}

function dot(state) {
  const d = document.getElementById('dash-dot');
  d.className = 'dot ' + (state || '');
}

function openDashboard() {
  chrome.tabs.create({ url: DASHBOARD });
}

// Авто-синх при открытии popup
document.addEventListener('DOMContentLoaded', async () => {
  const alive = await checkDashboard();
  if (alive) {
    // Тихая автосинхронизация при открытии popup
    const { cookies } = await chrome.runtime.sendMessage({ action: 'get' });
    if (cookies) {
      const r = await chrome.runtime.sendMessage({ action: 'sync' });
      if (r && r.ok) {
        const count = cookies.split(';').length;
        showResult('ok', `✓ ${count} cookies enviadas automáticamente`);
        await checkDashboard();
      }
    } else {
      showResult('warn', '⚠️ Inicia sesión en app.contasimple.com primero');
    }
  }
});
