/* ═══════════════════════════════════════════════════
   EAGLE Panel — Railway Edition · app.js v2
   SPA: ورود + داشبورد + کانفیگ + گروه + ساخت + سیستم
   ═══════════════════════════════════════════════════ */
'use strict';

const $ = (s, p) => (p || document).querySelector(s);
const $$ = (s, p) => Array.from((p || document).querySelectorAll(s));

const S = {
  me: null, links: [], subs: [],
  speedHist: [], curPage: 'dashboard',
  railTimer: null, dashTimer: null,
  theme: localStorage.getItem('eagle-theme') || 'dark',
};

/* ── ابزارها ─────────────────────────────────────────── */
function esc(x) {
  return String(x == null ? '' : x).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
async function api(path, opts = {}) {
  const o = Object.assign({ credentials: 'same-origin', headers: {} }, opts);
  if (o.body && typeof o.body === 'object') {
    o.body = JSON.stringify(o.body);
    o.headers['Content-Type'] = 'application/json';
  }
  const r = await fetch(path, o);
  if (r.status === 401 && S.me) { showAuth(); throw new Error('نشست منقضی شد'); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || j.error || 'خطای ناشناخته ' + r.status);
  return j;
}
function toast(msg, type = 'ok', ms = 3200) {
  const box = document.createElement('div');
  box.className = 'toast ' + type;
  const icon = type === 'ok' ? '✅' : type === 'err' ? '⛔' : '⚠️';
  box.innerHTML = `<span class="ti">${icon}</span><span>${esc(msg)}</span>`;
  $('#toastStack').appendChild(box);
  setTimeout(() => { box.classList.add('out'); setTimeout(() => box.remove(), 380); }, ms);
}
async function copyText(t, silent) {
  try {
    await navigator.clipboard.writeText(t);
    if (!silent) toast('کپی شد ✓');
  } catch {
    const ta = document.createElement('textarea');
    ta.value = t; document.body.appendChild(ta); ta.select();
    document.execCommand('copy'); ta.remove();
    if (!silent) toast('کپی شد ✓');
  }
}
function countUp(el, target, suffix = '') {
  const from = parseFloat((el.dataset.v || '0'));
  const to = parseFloat(target) || 0;
  el.dataset.v = to;
  const t0 = performance.now(), dur = 700;
  (function step(t) {
    const k = Math.min(1, (t - t0) / dur);
    const e = 1 - Math.pow(1 - k, 3);
    el.textContent = Math.round(from + (to - from) * e).toLocaleString('fa-IR') + suffix;
    if (k < 1) requestAnimationFrame(step);
  })(t0);
}

/* ── پروتکل‌ها ───────────────────────────────────────── */
const PROTOS = [
  { v: 'vless-ws', n: 'VLESS WebSocket', star: true },
  { v: 'vless-xhttp', n: 'XHTTP Stream One' },
  { v: 'vless-grpc', n: 'VLESS gRPC' },
  { v: 'vless-http2', n: 'VLESS HTTP/2' },
  { v: 'trojan-ws', n: 'Trojan WebSocket' },
  { v: 'shadowsocks', n: 'Shadowsocks' },
  { v: '_vmess', n: 'VMess WebSocket', demo: true },
  { v: '_socks5', n: 'SOCKS5', demo: true },
  { v: '_http', n: 'HTTP Proxy', demo: true },
  { v: '_hy2', n: 'Hysteria 2', demo: true },
  { v: '_tuic', n: 'TUIC', demo: true },
  { v: '_wg', n: 'WireGuard', demo: true },
  { v: '_hs', n: 'HighSpeed Upload/Download', demo: true },
  { v: '_game', n: 'Gaming Lite', demo: true },
];

function buildSelect(rootSel, hiddenInput, initial) {
  const root = $(rootSel);
  let val = initial || 'vless-ws';
  const cur = () => PROTOS.find(p => p.v === val) || PROTOS[0];
  function renderBtn() {
    const c = cur();
    root.querySelector('.sel-btn')?.remove();
    const b = document.createElement('button');
    b.type = 'button'; b.className = 'sel-btn';
    b.innerHTML = `<span class="sw">${c.star ? '<span>⭐</span>' : ''}${esc(c.n)}${c.demo ? ' <span class="tag demo">(دمو)</span>' : ''}</span><span class="sel-arr">▼</span>`;
    b.onclick = e => { e.stopPropagation(); $$('.sel.open').forEach(x => x !== root && x.classList.remove('open')); root.classList.toggle('open'); };
    root.prepend(b);
  }
  function renderMenu() {
    const m = document.createElement('div');
    m.className = 'sel-menu';
    m.innerHTML = PROTOS.map(p => `
      <div class="sel-opt ${p.v === val ? 'sel-sel' : ''} ${p.demo ? 'is-demo' : ''}" data-v="${p.v}">
        <span>${p.star ? '⭐ ' : ''}${esc(p.n)}</span>
        <span class="tag ${p.demo ? 'demo' : ''}">${p.demo ? 'دمو' : p.v}</span>
      </div>`).join('');
    $$('.sel-opt', m).forEach(o => {
      o.onclick = () => {
        const v = o.dataset.v;
        const p = PROTOS.find(x => x.v === v);
        if (p.demo) { toast('این پروتکل در نسخه‌ی فعلی دمو است — ' + p.n + ' هنوز پشتیبانی نمی‌شود', 'warn', 3600); root.classList.remove('open'); return; }
        val = v; $(hiddenInput).value = v;
        renderBtn(); renderMenu();
        root.classList.remove('open');
      };
    });
    root.querySelector('.sel-menu')?.remove();
    root.appendChild(m);
  }
  renderBtn(); renderMenu();
  return { get: () => val, set: v => { val = v; $(hiddenInput).value = v; renderBtn(); renderMenu(); } };
}
document.addEventListener('click', () => $$('.sel.open').forEach(x => x.classList.remove('open')));

/* ── ناوبری ──────────────────────────────────────────── */
function go(page) {
  S.curPage = page;
  $$('.nav-item').forEach(b => b.classList.toggle('active', b.dataset.page === page));
  $$('.view').forEach(v => v.classList.toggle('on', v.id === 'view-' + page));
  $('#sidebar').classList.remove('open'); $('#scrim').classList.remove('on');
  const loaders = {
    dashboard: loadDashboard, configs: loadLinks, groups: loadGroups,
    stats: loadStats, logs: loadLogs, system: loadSystem,
    telegram: loadTelegram, news: renderNews,
  };
  loaders[page] && loaders[page]();
  history.replaceState(null, '', '#' + page);
}
function initNav() {
  $$('.nav-item').forEach(b => b.onclick = () => go(b.dataset.page));
  $('#burgerBtn').onclick = () => { $('#sidebar').classList.add('open'); $('#scrim').classList.add('on'); };
  $('#scrim').onclick = () => { $('#sidebar').classList.remove('open'); $('#scrim').classList.remove('on'); };
  $('#collapseBtn').onclick = () => {
    const sb = $('#sidebar');
    sb.classList.toggle('mini');
    $('#main').classList.toggle('wide', sb.classList.contains('mini'));
    localStorage.setItem('eagle-mini', sb.classList.contains('mini') ? '1' : '');
  };
  if (localStorage.getItem('eagle-mini')) { $('#sidebar').classList.add('mini'); $('#main').classList.add('wide'); }
  $('#themeBtn').onclick = () => setTheme(S.theme === 'dark' ? 'light' : 'dark');
  $('#refreshBtn').onclick = () => { go(S.curPage); toast('آمار بروزرسانی شد'); };
  $('#updBtx').onclick = () => toast('پنل همیشه بروز — نسخه‌ی فعلی: Railway v2 ✓');
  $('#logoutBtn').onclick = async () => { try { await api('/api/logout', { method: 'POST' }); } catch {} showAuth(); };
}
function setTheme(t) {
  S.theme = t;
  document.documentElement.dataset.theme = t;
  localStorage.setItem('eagle-theme', t);
  $('#themeBtn .ic').textContent = t === 'dark' ? '☀️' : '🌙';
  $('#themeBtn span:last-child').textContent = t === 'dark' ? 'تم روشن' : 'تم تیره';
  api('/api/settings/theme', { method: 'POST', body: { theme: t === 'dark' ? 'cosmic' : 'white' } }).catch(() => {});
  if (S.curPage === 'dashboard') drawChart();
}

/* ── ورود / خروج ─────────────────────────────────────── */
function showAuth() {
  S.me = null;
  clearInterval(S.dashTimer); clearInterval(S.railTimer);
  $('#appShell').classList.remove('on');
  $('#authView').style.display = 'flex';
  fetch('/api/me').then(r => r.json()).catch(() => null).then(j => {
    $('#passHint').style.display = j && j.default_password ? 'block' : 'none';
  });
}
function showApp() {
  $('#authView').style.display = 'none';
  $('#appShell').classList.add('on');
  const h = (location.hash || '#dashboard').slice(1);
  go(['dashboard', 'configs', 'groups', 'create', 'stats', 'logs', 'system', 'telegram', 'news'].includes(h) ? h : 'dashboard');
}
async function boot() {
  setTheme(S.theme);
  initNav();
  initAuth();
  initModals();
  try {
    S.me = await api('/api/me');
    if (S.me && S.me.authenticated) { showApp(); refreshNavCounts(); }
    else showAuth();
  } catch { showAuth(); }
}
function initAuth() {
  $('#loginForm').onsubmit = async e => {
    e.preventDefault();
    const btn = $('#lgBtn');
    btn.disabled = true; btn.textContent = 'در حال ورود…';
    $('#authErr').style.display = 'none';
    try {
      await api('/api/login', { method: 'POST', body: { username: $('#lgUser').value.trim(), password: $('#lgPass').value } });
      const t = $('#railTokenLogin').value.trim();
      if (t) {
        try { await api('/api/settings/railway', { method: 'POST', body: { token: t } }); } catch {}
      }
      S.me = { authenticated: true };
      toast('خوش آمدی! 🦅');
      showApp(); refreshNavCounts();
    } catch (er) {
      const box = $('#authErr');
      box.textContent = er.message === 'Forbidden' || er.message.includes('403') ? 'نام کاربری یا رمز اشتباه است' : er.message;
      box.style.display = 'block';
    } finally { btn.disabled = false; btn.textContent = 'ورود به پنل'; }
  };
  $('#railSaveLogin').onclick = async () => {
    const t = $('#railTokenLogin').value.trim();
    if (!t) return toast('اول توکن را وارد کن', 'warn');
    try {
      const r = await api('/api/settings/railway', { method: 'POST', body: { token: t } });
      toast(r.verified ? 'توکن ریلوی تأیید شد ✓' : 'ذخیره شد — ولی تأیید ناموفق بود', r.verified ? 'ok' : 'warn');
    } catch (er) { toast(er.message, 'err'); }
  };
}
function refreshNavCounts() {
  api('/api/links').then(j => { $('#navCntLinks').textContent = (j.links || []).length; }).catch(() => {});
  api('/api/subs').then(j => { $('#navCntSubs').textContent = (j.subs || []).length; }).catch(() => {});
}
/* ── داشبورد ─────────────────────────────────────────── */
async function loadDashboard() {
  clearInterval(S.dashTimer); clearInterval(S.railTimer);
  await dashTick();
  S.dashTimer = setInterval(dashTick, 5000);
  await railTick();
  S.railTimer = setInterval(railTick, 60000);
}
async function dashTick() {
  try {
    const d = await api('/api/dashboard/stats');
    countUp($('#kLinks'), d.links_count);
    countUp($('#kActive'), d.active_links);
    countUp($('#kConns'), d.connections);
    $('#kTraffic').textContent = d.traffic.total_fmt;
    $('#chSpeed').textContent = d.speed.download_fmt;
    $('#chToday').textContent = d.traffic.today_fmt;
    $('#chReq').textContent = (d.requests || 0).toLocaleString('fa-IR');
    $('#cpuPct').textContent = Math.round(d.cpu.percent) + '%';
    $('#cpuCores').textContent = d.cpu.cores + ' cores';
    const C = 2 * Math.PI * 72;
    $('#cpuRing').style.strokeDashoffset = C * (1 - Math.min(100, d.cpu.percent) / 100);
    $('#diskPct2').textContent = Math.round(d.disk.percent) + '%';
    $('#diskTxt').textContent = d.disk.used_fmt + ' / ' + d.disk.total_fmt;
    $('#diskFree').textContent = d.disk.free_fmt;
    $('#diskBar').style.width = Math.min(100, d.disk.percent) + '%';
    $('#upTxt').textContent = d.uptime || '—';
    $('#memTxt').textContent = (d.memory ? d.memory.used_fmt : '—') + ' / ' + (d.memory ? d.memory.total_fmt : '—');
    S.speedHist.push(d.speed.download || 0);
    if (S.speedHist.length > 60) S.speedHist.shift();
    drawChart();
  } catch {}
}
function drawChart() {
  const cv = $('#trafficChart');
  if (!cv) return;
  const dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth, H = cv.clientHeight;
  if (!W) return;
  cv.width = W * dpr; cv.height = H * dpr;
  const x = cv.getContext('2d');
  x.scale(dpr, dpr);
  x.clearRect(0, 0, W, H);
  const data = S.speedHist;
  if (data.length < 2) return;
  const max = Math.max(...data, 1) * 1.25;
  const px = i => (i / (data.length - 1)) * (W - 10) + 5;
  const py = v => H - 14 - (v / max) * (H - 34);
  // گرید
  x.strokeStyle = 'rgba(140,145,175,.13)'; x.lineWidth = 1;
  for (let g = 0; g < 4; g++) {
    const y = 10 + g * ((H - 30) / 3);
    x.beginPath(); x.moveTo(4, y); x.lineTo(W - 4, y); x.stroke();
  }
  // ناحیه
  const grad = x.createLinearGradient(0, 0, 0, H);
  grad.addColorStop(0, 'rgba(99,110,247,.38)');
  grad.addColorStop(1, 'rgba(99,110,247,0)');
  x.beginPath(); x.moveTo(px(0), py(data[0]));
  for (let i = 1; i < data.length; i++) x.lineTo(px(i), py(data[i]));
  x.lineTo(px(data.length - 1), H - 10); x.lineTo(px(0), H - 10); x.closePath();
  x.fillStyle = grad; x.fill();
  // خط
  x.beginPath(); x.moveTo(px(0), py(data[0]));
  for (let i = 1; i < data.length; i++) x.lineTo(px(i), py(data[i]));
  const lg = x.createLinearGradient(0, 0, W, 0);
  lg.addColorStop(0, '#4f6ef7'); lg.addColorStop(1, '#ec4899');
  x.strokeStyle = lg; x.lineWidth = 2.4; x.lineJoin = 'round'; x.stroke();
  // نقطه‌ی آخر
  const lx = px(data.length - 1), ly = py(data[data.length - 1]);
  x.beginPath(); x.arc(lx, ly, 3.6, 0, 7); x.fillStyle = '#ec4899'; x.fill();
  x.beginPath(); x.arc(lx, ly, 7, 0, 7); x.fillStyle = 'rgba(236,72,153,.25)'; x.fill();
}

/* ── کارت اعتبار ریلوی ───────────────────────────────── */
async function railTick() {
  try {
    const st = await api('/api/railway/status');
    if (!st.has_token) { renderRailEmpty(); return; }
    const d = await api('/api/railway/credit');
    if (!d.ok) {
      renderRailEmpty(d.reason === 'invalid_token'
        ? 'توکن ریلوی نامعتبر است — یک توکن جدید بساز و ثبت کن.'
        : 'دریافت اطلاعات از ریلوی ناموفق بود — کمی بعد دوباره امتحان کن.');
      return;
    }
    $('#railLive').style.display = 'inline-flex';
    $('#railBody').innerHTML = `
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <div class="rail-amount">${esc(d.remaining_credit_fmt)}<small>remaining credit</small></div>
      </div>
      <div class="rail-rows">
        <div class="rail-row"><span class="k">Days remaining</span><span class="v">${d.days_remaining}</span></div>
        <div class="rail-row"><span class="k">Spent this cycle</span><span class="v">${esc(d.spent_this_cycle_fmt)} · ${esc(d.spent_per_day_fmt)}</span></div>
        <div class="rail-row"><span class="k">Traffic that buys</span><span class="v">${esc(d.traffic_buys)}</span></div>
        <div class="rail-row"><span class="k">Used this cycle</span><span class="v">${esc(d.used_fmt)} · ${esc(d.used_per_day_fmt)}</span></div>
        <div class="rail-row"><span class="k">Railway egress</span><span class="v">${esc(d.egress_fmt)}</span></div>
        <div class="rail-row"><span class="k">Railway account</span><span class="v">${esc(d.account || '—')}</span></div>
      </div>`;
  } catch {}
}
function renderRailEmpty(msg) {
  $('#railLive').style.display = 'none';
  $('#railBody').innerHTML = `
    <div class="rail-empty">
      <span class="big">🛰️</span>
      ${esc(msg || 'برای نمایش دقیق مصرف اعتبار ۵ دلاری ریلوی، توکن API ریلوی را ثبت کن.')}
      <br><button class="btn btn-primary" style="margin-top:10px" onclick="openRailModal()">ثبت توکن ریلوی</button>
    </div>`;
}
function openRailModal() {
  openModal(`
    <h3>🛰️ توکن ریلوی</h3>
    <p class="cc-sub">توکن از <b>railway.com/account/tokens</b> بگیر (Team Token یا Account Token). با این توکن پنل مستقیم از سرورهای ریلوی می‌خواند که چقدر از اعتبار ۵ دلاری مصرف شده.</p>
    <div class="field"><label>Railway API Token</label>
      <input class="inp inp-mono" id="railTokInput" placeholder=" paste token…" dir="ltr"></div>
    <div class="m-actions">
      <button class="btn btn-primary" id="railTokSave">ذخیره و بررسی</button>
      <button class="btn btn-err" id="railTokDel">حذف توکن</button>
    </div>
    <div class="rail-hint">پس از ذخیره، کارت «RAILWAY BALANCE» داشبورد زنده می‌شود (به‌روزرسانی هر ۶۰ ثانیه). <a href="https://railway.com/account/tokens" target="_blank" rel="noopener">ساخت توکن ↗</a></div>
  `);
  $('#railTokSave').onclick = async () => {
    const t = $('#railTokInput').value.trim();
    if (!t) return toast('توکن را وارد کن', 'warn');
    try {
      const r = await api('/api/settings/railway', { method: 'POST', body: { token: t } });
      if (r.verified) { toast('تأیید شد ✓ حساب: ' + (r.account || '')); closeModal(); railTick(); }
      else toast('ذخیره شد ولی تأیید نشد: ' + (r.error || '?'), 'warn', 4200);
    } catch (er) { toast(er.message, 'err'); }
  };
  $('#railTokDel').onclick = async () => {
    try { await api('/api/settings/railway', { method: 'DELETE' }); toast('توکن حذف شد'); closeModal(); renderRailEmpty(); }
    catch (er) { toast(er.message, 'err'); }
  };
}

/* ── مودال عمومی ─────────────────────────────────────── */
function openModal(html) { $('#modalBox').innerHTML = html; $('#modalBg').classList.add('on'); }
function closeModal() { $('#modalBg').classList.remove('on'); }
function initModals() {
  $('#modalBg').onclick = e => { if (e.target === $('#modalBg')) closeModal(); };
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });
  $('#railTokenBtn').onclick = openRailModal;
  $('#railAddBtn').onclick = openRailModal;
}

/* ── کانفیگ‌ها ───────────────────────────────────────── */
function usageClass(link) {
  if (!link.limit_bytes) return 'low';
  const p = (link.used_bytes || 0) / link.limit_bytes;
  return p > 0.85 ? 'hot' : p > 0.5 ? 'mid' : 'low';
}
function fmtU(link) {
  const b = link.used_bytes || 0;
  if (b < 1024) return b + ' B';
  if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
  if (b < 1073741824) return (b / 1048576).toFixed(2) + ' MB';
  return (b / 1073741824).toFixed(2) + ' GB';
}
async function loadLinks() {
  try {
    const j = await api('/api/links');
    S.links = j.links || [];
    $('#navCntLinks').textContent = S.links.length;
    renderLinks($('#cfgSearch').value.trim().toLowerCase());
  } catch (er) { toast(er.message, 'err'); }
}
function renderLinks(q) {
  const list = q ? S.links.filter(l => (l.label || '').toLowerCase().includes(q) || l.uuid.includes(q)) : S.links;
  $('#cfgEmpty').style.display = list.length ? 'none' : 'block';
  $('#cfgBody').innerHTML = list.map((l, i) => {
    const active = l.active !== false && !l.expired;
    return `<tr style="animation-delay:${Math.min(i * 40, 400)}ms">
      <td><span style="color:var(--mut2);cursor:grab">⠿</span></td>
      <td><span class="cf-name"><span class="n-usage">${l.sub_id ? '۱' : '۰'}</span>${esc(l.label)}</span></td>
      <td><span class="pill">${esc(l.protocol)}</span></td>
      <td><label class="toggle"><input type="checkbox" ${active ? 'checked' : ''} onchange="toggleLink('${l.uuid}', this)"><span class="tr"></span></label></td>
      <td><span class="usage-val ${usageClass(l)}">${fmtU(l)}</span></td>
      <td><div class="acts">
        <button class="icon-btn" title="کپی لینک" onclick="copyLink('${l.uuid}')">⧉</button>
        <button class="icon-btn ping" title="تست پینگ" onclick="pingTest('${l.uuid}')">📶</button>
        <button class="icon-btn" title="اطلاعات و QR" onclick="infoLink('${l.uuid}')">ⓘ</button>
        <button class="icon-btn" title="صفر کردن مصرف" onclick="resetLink('${l.uuid}')">↺</button>
        <button class="icon-btn danger" title="حذف" onclick="delLink('${l.uuid}')">🗑</button>
      </div></td></tr>`;
  }).join('');
}
window.toggleLink = async (uid, el) => {
  try { await api('/api/links/' + uid, { method: 'PATCH', body: { active: el.checked } });
    toast(el.checked ? 'کانفیگ فعال شد' : 'کانفیگ غیرفعال شد'); }
  catch (er) { toast(er.message, 'err'); el.checked = !el.checked; }
};
window.copyLink = uid => { const l = S.links.find(x => x.uuid === uid); l && copyText(l.vless_link || ''); };
window.pingTest = async uid => {
  const l = S.links.find(x => x.uuid === uid);
  toast('در حال اندازه‌گیری پینگ…', 'warn', 1200);
  const times = [];
  for (let i = 0; i < 3; i++) {
    const t0 = performance.now();
    try { await fetch('/api/ping', { cache: 'no-store' }); times.push(performance.now() - t0); } catch {}
  }
  if (!times.length) return toast('پینگ ناموفق', 'err');
  const best = Math.round(Math.min(...times));
  const avg = Math.round(times.reduce((a, b) => a + b, 0) / times.length);
  toast(`پینگ «${l ? l.label : uid.slice(0, 8)}» — بهترین: ${best}ms · میانگین: ${avg}ms`, 'ok', 4200);
};
window.resetLink = async uid => {
  try { await api('/api/links/' + uid, { method: 'PATCH', body: { reset_usage: true } });
    toast('مصرف صفر شد'); loadLinks(); }
  catch (er) { toast(er.message, 'err'); }
};
window.delLink = async uid => {
  const l = S.links.find(x => x.uuid === uid);
  openModal(`<h3>🗑 حذف کانفیگ</h3>
    <p class="cc-sub">مطمئنی «<b>${esc(l ? l.label : '')}</b>» حذف شود؟ این کار برگشت‌پذیر نیست.</p>
    <div class="m-actions"><button class="btn btn-err" id="delYes">حذف قطعی</button><button class="btn" id="delNo">انصراف</button></div>`);
  $('#delYes').onclick = async () => {
    try { await api('/api/links/' + uid, { method: 'DELETE' }); toast('حذف شد'); closeModal(); loadLinks(); refreshNavCounts(); }
    catch (er) { toast(er.message, 'err'); }
  };
  $('#delNo').onclick = closeModal;
};
window.infoLink = async uid => {
  const l = S.links.find(x => x.uuid === uid);
  if (!l) return;
  openModal(`
    <h3>ⓘ ${esc(l.label)}</h3>
    <div class="kv"><span class="k">UUID</span><span class="v">${esc(l.uuid)}</span></div>
    <div class="kv"><span class="k">Protocol</span><span class="v">${esc(l.protocol)} · ${esc(l.http_version || 'h2')}</span></div>
    <div class="kv"><span class="k">Fingerprint</span><span class="v">${esc(l.fingerprint || 'chrome')}</span></div>
    <div class="kv"><span class="k">مصرف</span><span class="v">${fmtU(l)} / ${l.limit_bytes ? fmtU({ used_bytes: l.limit_bytes }) : 'نامحدود'}</span></div>
    <div class="kv"><span class="k">انقضا</span><span class="v">${esc(l.expires_at ? l.expires_at.slice(0, 10) : 'نامحدود')}</span></div>
    <div class="link-box"><input class="inp" id="lnkVless" readonly value="${esc(l.vless_link || '')}"><button class="icon-btn" onclick="copyText(document.getElementById('lnkVless').value)">⧉</button></div>
    <div class="link-box"><input class="inp" id="lnkSub" readonly value="${esc(l.sub_url || '')}"><button class="icon-btn" onclick="copyText(document.getElementById('lnkSub').value)">⧉</button></div>
    <div id="qrBox"></div>
    <div class="m-actions"><button class="btn btn-primary" id="qrToggle">نمایش QR Code</button></div>`);
  $('#qrToggle').onclick = () => {
    const box = $('#qrBox');
    if (box.dataset.on) { box.innerHTML = ''; box.dataset.on = ''; return; }
    box.dataset.on = '1';
    if (window.QRCode) new QRCode(box, { text: l.vless_link || l.sub_url, width: 210, height: 210 });
    else box.textContent = 'QR لود نشد';
  };
};
$('#cfgSearch') && ($('#cfgSearch').oninput = e => renderLinks(e.target.value.trim().toLowerCase()));
$('#addCfgBtn') && ($('#addCfgBtn').onclick = () => {
  openModal(`
    <h3>＋ کانفیگ جدید</h3>
    <div class="field"><label>نام</label><input class="inp" id="qcName" placeholder="auto"></div>
    <div class="field"><label>پروتکل</label>
      <select class="inp" id="qcProto">
        ${PROTOS.filter(p => !p.demo).map(p => `<option value="${p.v}">${p.star ? '⭐ ' : ''}${p.n}</option>`).join('')}
      </select></div>
    <div class="field"><label>تعداد (۱–۲۰۰)</label><input class="inp inp-mono" id="qcCount" type="number" min="1" max="200" value="1"></div>
    <div class="m-actions"><button class="btn btn-primary" id="qcGo">＋ ساخت</button><button class="btn" id="qcNo">انصراف</button></div>`);
  $('#qcNo').onclick = closeModal;
  $('#qcGo').onclick = () => buildConfigs($('#qcName').value.trim(), $('#qcProto').value, parseInt($('#qcCount').value || '1'), 0, 0, 0, 0);
});

/* ── ساخت کانفیگ ─────────────────────────────────────── */
const NAME_WORDS = ['eagle', 'swift', 'falcon', 'nova', 'orbit', 'pixel', 'pulse', 'comet', 'phoenix', 'radar'];
function rndName() {
  const w = NAME_WORDS[Math.floor(Math.random() * NAME_WORDS.length)];
  return w + '-' + Math.random().toString(36).slice(2, 7);
}
async function buildConfigs(name, protocol, count, expDays, volumeGB, maxIp, speed) {
  count = Math.max(1, Math.min(200, count || 1));
  toast(`در حال ساخت ${count.toLocaleString('fa-IR')} کانفیگ…`, 'warn', 1600);
  const made = [];
  for (let i = 0; i < count; i++) {
    try {
      const body = {
        label: name || rndName(),
        protocol, http_version: protocol === 'vless-xhttp' ? 'h2' : 'h2',
        max_devices: maxIp || 0,
        limit_value: volumeGB || 0, limit_unit: 'GB',
        expires_days: expDays || 0,
      };
      const r = await api('/api/links', { method: 'POST', body });
      made.push(r);
    } catch (er) { toast(er.message, 'err'); break; }
  }
  refreshNavCounts();
  if (!made.length) return;
  const links = made.map(m => m.vless_link).join('\n');
  const subUrl = made[0].sub_url;
  openModal(`
    <h3>✅ ${made.length.toLocaleString('fa-IR')} کانفیگ ساخته شد</h3>
    <div class="kv"><span class="k">UUID اول</span><span class="v">${esc(made[0].uuid)}</span></div>
    <div class="kv"><span class="k">پروتکل</span><span class="v">${esc(protocol)}</span></div>
    <div class="link-box"><input class="inp" readonly value="${esc(made[0].vless_link)}"><button class="icon-btn" onclick="copyText(document.querySelector('#modalBox .link-box input').value)">⧉</button></div>
    <div class="link-box"><input class="inp" readonly value="${esc(subUrl)}"><button class="icon-btn" onclick="copyText(document.querySelectorAll('#modalBox .link-box input')[1].value)">⧉</button></div>
    <div id="qrBox"></div>
    <div class="m-actions">
      <button class="btn btn-primary" id="cpAll">کپی همه (${made.length.toLocaleString('fa-IR')})</button>
      <button class="btn" id="qrAll">QR</button>
      <button class="btn" id="okDone">تمام</button>
    </div>`);
  $('#cpAll').onclick = () => copyText(links, true).then(() => toast('همه کپی شد ✓'));
  $('#qrAll').onclick = () => {
    const box = $('#qrBox'); box.innerHTML = ''; box.dataset.on = '1';
    if (window.QRCode) new QRCode(box, { text: made[0].vless_link, width: 210, height: 210 });
  };
  $('#okDone').onclick = () => { closeModal(); if (S.curPage === 'configs' || S.curPage === 'create') S.curPage === 'create' ? go('configs') : loadLinks(); };
}
function initCreate() {
  buildSelect('#mProtoSel', '#mProto', 'vless-ws');
  buildSelect('#aProtoSel', '#aProto', 'vless-ws');
  $('#mNameRnd').onclick = () => { $('#mName').value = rndName(); };
  $('#mBuild').onclick = () => {
    const c = parseInt($('#mCount').value || '1');
    if (c > 1) return openBulkConfirm(c, () => doManual());
    doManual();
    function doManual() {
      buildConfigs($('#mName').value.trim(), $('#mProto').value, c,
        parseInt($('#mExp').value || '0'), parseFloat($('#mVol').value || '0'),
        parseInt($('#mIp').value || '0'), parseInt($('#mSpeed').value || '0'));
    }
  };
  $('#aBuild').onclick = () => {
    const c = parseInt($('#aCount').value || '1');
    const go2 = () => buildConfigs('', $('#aProto').value, c, 0, 0, 0, 0);
    if (c > 1) return openBulkConfirm(c, go2);
    go2();
  };
}
function openBulkConfirm(count, cb) {
  openModal(`<h3>🧺 ساخت سبد کانفیگ</h3>
    <p class="cc-sub">قرار است <b style="font-family:var(--mono);font-size:20px;color:#c4b5fd">${count.toLocaleString('fa-IR')}</b> کانفیگ ساخته شود. ادامه می‌دهی؟</p>
    <div class="m-actions"><button class="btn btn-primary" id="bcYes">بله، بساز</button><button class="btn" id="bcNo">انصراف</button></div>`);
  $('#bcYes').onclick = () => { closeModal(); cb(); };
  $('#bcNo').onclick = closeModal;
}
/* ── گروه‌ها ─────────────────────────────────────────── */
async function loadGroups() {
  try {
    const j = await api('/api/subs');
    S.subs = j.subs || [];
    $('#navCntSubs').textContent = S.subs.length;
    $('#grpList').innerHTML = S.subs.length ? S.subs.map((s, i) => `
      <div class="grp-row" style="animation-delay:${i * 50}ms">
        <div><div class="g-name">${esc(s.name)}</div>
        <div class="g-meta">${(s.members_count || 0)} members · ${esc(s.sub_url || '')}</div></div>
        <span class="spacer"></span>
        <button class="icon-btn" title="کپی ساب گروه" onclick="copyText('${esc(s.sub_url)}')">⧉</button>
        <button class="chip-del" onclick="delGroup('${s.id}', '${esc(s.name)}')">حذف</button>
      </div>`).join('')
      : `<div class="empty"><span class="e">📁</span><p>هنوز گروهی نیست.<br>از فرم روبه‌رو گروه اولت را بساز!</p></div>`;
  } catch (er) { toast(er.message, 'err'); }
}
$('#grpCreate') && ($('#grpCreate').onclick = async () => {
  const name = $('#grpName').value.trim();
  if (!name) return toast('اسم گروه را بنویس', 'warn');
  try {
    await api('/api/subs', { method: 'POST', body: { name } });
    toast('گروه ساخته شد'); $('#grpName').value = '';
    loadGroups(); refreshNavCounts();
  } catch (er) { toast(er.message, 'err'); }
});
window.delGroup = async (sid, name) => {
  openModal(`<h3>🗑 حذف گروه</h3><p class="cc-sub">گروه «<b>${esc(name)}</b>» حذف شود؟ کانفیگ‌های عضو سالم می‌مانند.</p>
    <div class="m-actions"><button class="btn btn-err" id="dgYes">حذف</button><button class="btn" id="dgNo">انصراف</button></div>`);
  $('#dgYes').onclick = async () => {
    try { await api('/api/subs/' + sid, { method: 'DELETE' }); toast('گروه حذف شد'); closeModal(); loadGroups(); refreshNavCounts(); }
    catch (er) { toast(er.message, 'err'); }
  };
  $('#dgNo').onclick = closeModal;
};

/* ── آمار ────────────────────────────────────────────── */
async function loadStats() {
  try {
    const d = await api('/stats');
    $('#stToday').textContent = fmtB(Object.values(d.hourly || {}).reduce((a, b) => a + b, 0));
    const hours = Object.entries(d.hourly || {}).sort().slice(-24);
    const max = Math.max(1, ...hours.map(h => h[1]));
    $('#hourBars').innerHTML = hours.map(([h, v]) =>
      `<i style="height:${Math.max(2, (v / max) * 100)}%" title="${h} — ${fmtB(v)}"></i>`).join('') || '<div class="empty" style="width:100%">داده‌ای نیست</div>';
    $('#statList').innerHTML = `
      <div class="stat-line"><span class="k">اتصالات فعال</span><span class="v">${d.active_connections}</span></div>
      <div class="stat-line"><span class="k">ترافیک کل</span><span class="v">${d.total_traffic_mb.toFixed(2)} MB</span></div>
      <div class="stat-line"><span class="k">کل درخواست‌ها</span><span class="v">${(d.total_requests || 0).toLocaleString('fa-IR')}</span></div>
      <div class="stat-line"><span class="k">خطاها</span><span class="v">${d.total_errors || 0}</span></div>
      <div class="stat-line"><span class="k">کانفیگ‌ها</span><span class="v">${d.links_count}</span></div>
      <div class="stat-line"><span class="k">فعال / منقضی</span><span class="v">${d.active_links} / ${d.expired_links}</span></div>
      <div class="stat-line"><span class="k">گروه‌ها</span><span class="v">${d.subs_count}</span></div>
      <div class="stat-line"><span class="k">آپ‌تایم</span><span class="v">${esc(d.uptime || '—')}</span></div>`;
    $('#topUser').innerHTML = d.top_user
      ? `<div class="stat-line"><span class="k">${esc(d.top_user.label)}</span><span class="v">${esc(d.top_user.used_fmt)}</span></div>`
      : `<div class="empty"><span class="e">🔍</span><p>هنوز مصرفی ثبت نشده</p></div>`;
  } catch (er) { toast(er.message, 'err'); }
}
function fmtB(b) {
  b = b || 0;
  if (b < 1024) return b + ' B';
  if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
  if (b < 1073741824) return (b / 1048576).toFixed(2) + ' MB';
  return (b / 1073741824).toFixed(2) + ' GB';
}
$('#statRefresh') && ($('#statRefresh').onclick = loadStats);
$('#logRefresh') && ($('#logRefresh').onclick = loadLogs);

/* ── لاگ ─────────────────────────────────────────────── */
async function loadLogs() {
  try {
    const j = await api('/api/activity');
    const logs = (j.logs || []).slice().reverse();
    $('#logList').innerHTML = logs.length ? logs.map(l => {
      const t = l.type || 'info';
      const ic = t === 'link' ? '🔗' : t === 'sub' ? '📁' : t === 'error' ? '⛔' : t === 'conn' ? '🔌' : '⚙️';
      return `<div class="log-row"><div class="ic ${l.level || 'info'}">${ic}</div>
        <div><div class="m">${esc(l.msg || '')}</div><div class="t">${esc(l.at || '')}</div></div></div>`;
    }).join('') : `<div class="empty"><span class="e">📭</span><p>لاگی ثبت نشده</p></div>`;
  } catch (er) { toast(er.message, 'err'); }
}

/* ── سیستم ───────────────────────────────────────────── */
async function loadSystem() {
  try {
    const d = await api('/api/dashboard/stats');
    $('#sysCpu').textContent = Math.round(d.cpu.percent) + '%';
    $('#sysCpuCores').textContent = d.cpu.cores + ' cores';
    const C = 2 * Math.PI * 58;
    $('.sysring').style.strokeDashoffset = C * (1 - Math.min(100, d.cpu.percent) / 100);
    $('#memPct').textContent = Math.round(d.memory.percent) + '%';
    $('#memBar').style.width = Math.min(100, d.memory.percent) + '%';
    $('#memTotal').textContent = d.memory.total_fmt;
    $('#memUsed').textContent = d.memory.used_fmt;
    $('#qTxt').textContent = d.quota.bytes_fmt + ' / 100 GB';
    $('#qPct').textContent = d.quota.percent.toFixed(1) + '%';
    $('#qBar').style.width = Math.min(100, d.quota.percent) + '%';
    $('#quotaWarn').style.display = d.quota.warn_99 ? 'block' : 'none';
  } catch {}
}
$('#bkDl') && ($('#bkDl').onclick = async () => {
  try {
    const j = await api('/api/backup');
    const blob = new Blob([JSON.stringify(j, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'eagle-backup-' + new Date().toISOString().slice(0, 10) + '.json';
    a.click();
    toast('بکاپ دانلود شد');
  } catch (er) { toast(er.message, 'err'); }
});
$('#bkUp') && ($('#bkUp').onclick = () => $('#bkFile').click());
$('#bkFile') && ($('#bkFile').onchange = async e => {
  const f = e.target.files[0];
  if (!f) return;
  try {
    const data = JSON.parse(await f.text());
    await api('/api/backup/restore', { method: 'POST', body: data });
    toast('بازگردانی انجام شد ✓');
    refreshNavCounts();
  } catch (er) { toast('بازگردانی ناموفق: ' + er.message, 'err'); }
});
$('#crSave') && ($('#crSave').onclick = async () => {
  const u = $('#crUser').value.trim(), p = $('#crPass').value;
  if (!u && !p) return toast('چیزی برای تغییر وارد نکردی', 'warn');
  try {
    await api('/api/settings/credentials', { method: 'POST', body: { username: u, password: p } });
    toast('اعتبارنامه تغییر کرد ✓');
    $('#crUser').value = ''; $('#crPass').value = '';
    $('#passHint').style.display = 'none';
  } catch (er) { toast(er.message, 'err'); }
});

/* ── تلگرام ──────────────────────────────────────────── */
async function loadTelegram() {
  try {
    const s = await api('/api/settings/telegram');
    $('#tgToken').value = s.token || '';
    $('#tgChat').value = s.chat_id || '';
    $('#tgStatus').innerHTML = s.enabled
      ? `<div class="stat-line"><span class="k">وضعیت</span><span class="v" style="color:#4ade80">فعال ✓</span></div>
         <div class="stat-line"><span class="k">بات</span><span class="v">${esc(s.bot_username || '—')}</span></div>
         <div class="stat-line"><span class="k">چت‌آی‌دی</span><span class="v">${esc(s.chat_id || '—')}</span></div>`
      : `<div class="empty"><span class="e">✈️</span><p>ربات غیرفعال است.<br>توکن بات و چت‌آی‌دی را ثبت و فعال کن.</p></div>`;
  } catch {}
}
$('#tgSave') && ($('#tgSave').onclick = async () => {
  try {
    await api('/api/settings/telegram', {
      method: 'POST',
      body: { token: $('#tgToken').value.trim(), chat_id: $('#tgChat').value.trim(), enabled: true },
    });
    toast('ربات فعال شد ✓'); loadTelegram();
  } catch (er) { toast(er.message, 'err'); }
});
$('#tgOff') && ($('#tgOff').onclick = async () => {
  try { await api('/api/settings/telegram/disable', { method: 'POST' }); toast('ربات غیرفعال شد'); loadTelegram(); }
  catch (er) { toast(er.message, 'err'); }
});

/* ── اخبار ───────────────────────────────────────────── */
function renderNews() {
  const v = 'v2.0';
  const news = [
    { t: '🦅 نسخه‌ی ۲ — بازطراحی کامل', d: 'ظاهر مینیمال و تیره با انیمیشن‌های زنده، سایدبار جمع‌شونده، ساخت کانفیگ سه‌بعدی و کارت زنده‌ی اعتبار ریلوی. فایل‌های پنل هم مرتب شدند: دیگر pages و فایل‌های سنگین HTML داخل پایتون نیست — رابط کاربری جدا شد و پنل سبک‌تر شد.', n: v },
    { t: '🛰️ کارت اعتبار ریلوی', d: 'توکن Railway را در صفحه‌ی ورود یا دکمه‌ی «تغییر توکن» ثبت کن تا دقیقاً ببینی از اعتبار ۵ دلاری چقدر مصرف شده: اعتبار باقی‌مانده، روزهای باقی‌مانده، مصرف این سیکل و خروجی شبکه — مستقیم از API رسمی ریلوی.', n: v },
    { t: '📶 پینگ به‌صورت دستی', d: 'پینگ‌سنج زنده از داشبورد حذف شد تا داشبورد تمیز بماند. برای تست سرعت اتصال، روی آیکون 📶 هر کانفیگ بزن — پینگ لحظه‌ای اندازه گرفته می‌شود.', n: v },
    { t: '⚡ رله‌ی واقعی VLESS + XHTTP', d: 'موتور رله (relay_vless.py + xhttp_siz10.py) بدون تغییر حفظ شده — کانفیگ‌ها همان‌طور که بودند وصل می‌شوند و پینگ می‌دهند.', n: v },
  ];
  $('#newsList').innerHTML = news.map(n => `
    <div class="news-card"><div class="nt">${esc(n.n)}</div><h3>${n.t}</h3><p>${n.d}</p></div>`).join('');
}

/* ── افکت سه‌بعدی کارت‌های ساخت ──────────────────────── */
function initTilt() {
  $$('.create-card').forEach(card => {
    card.addEventListener('mousemove', e => {
      const r = card.getBoundingClientRect();
      const rx = ((e.clientY - r.top) / r.height - 0.5) * -7;
      const ry = ((e.clientX - r.left) / r.width - 0.5) * 9;
      card.style.transform = `rotateX(${rx}deg) rotateY(${ry}deg) translateY(-3px)`;
    });
    card.addEventListener('mouseleave', () => { card.style.transform = ''; });
  });
}

/* ── بوت ─────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  boot();
  initCreate();
  initTilt();
});


