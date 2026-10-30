/* 左侧栏的共用行为：折叠、拖拽调宽、对话历史。
 * ---------------------------------------------------------------------------
 * 2026-10-30 把「论文库」「发现」从弹窗改成整页后，侧栏要在四个页面
 * （index / library / discover / 以后的页面）里长得一样、行为一样。
 * 复制粘贴四份的话，改一个 bug 要改四处，必然漏。所以这里抽成一份。
 *
 * 页面只需：
 *   1. <link rel="stylesheet" href="design-system.css"> 自带侧栏外观；
 *   2. 把侧栏 HTML 按约定写好（id 见下）；
 *   3. <script src="sidebar.js"></script> 引本文件。
 *
 * DOM 约定（缺哪块就跳过哪块，不报错——比如独立页可能不需要「开启新对话」）：
 *   #sidebar / #sbToggle / #sbToggleAct / #sbHistory / #sbResizer
 *   .sb-item[href] 里带 data-nav="library" 之类，用来自动高亮当前页
 *
 * 历史条目点开只跳回对话页并带上 ?c=<会话id>，实际的对话渲染在 index.html 里做。
 * 侧栏不复制聊天逻辑——它只需要知道"有哪些会话"。
 */

const SB_SESSIONS_KEY = 'campus-agent-sessions-v1';
const SB_COLLAPSE_KEY = 'campus-agent-sidebar-collapsed-v1';
const SB_WIDTH_KEY = 'campus-agent-sidebar-w-v1';
const SB_MIN_WIDTH = 200;
const SB_MAX_WIDTH = 480;
const SB_DEFAULT_WIDTH = 264;
const SB_TITLE_MAX = 20;
const SB_BUCKET_ORDER = ['今天', '7 天内', '30 天内', '更早'];

function sbReadSessions() {
  try {
    const raw = localStorage.getItem(SB_SESSIONS_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    const list = (parsed && Array.isArray(parsed.sessions)) ? parsed.sessions : [];
    // 空会话不显示：它们只会渲染成「未命名对话」，点开是空白
    return list.filter(s => s && s.id && Array.isArray(s.messages) && s.messages.length);
  } catch (e) {
    return [];
  }
}

function sbTitle(s) {
  const first = s.messages.find(m => m.role === 'user') || s.messages[0] || { content: '' };
  const t = String(s.title || first.content || '').replace(/\s+/g, ' ').trim();
  if (!t) return '未命名对话';
  return t.length > SB_TITLE_MAX ? t.slice(0, SB_TITLE_MAX) + '…' : t;
}

function sbBucket(ts) {
  const now = new Date();
  const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  if (ts >= midnight) return '今天';
  if (ts >= midnight - 7 * 86400000) return '7 天内';
  if (ts >= midnight - 30 * 86400000) return '30 天内';
  return '更早';
}

function sbRenderHistory() {
  const box = document.getElementById('sbHistory');
  if (!box) return;
  const list = sbReadSessions().sort((a, b) => (Number(b.updatedAt) || 0) - (Number(a.updatedAt) || 0));
  const activeId = (() => {
    try { return localStorage.getItem(SB_SESSIONS_KEY + ':active') || ''; } catch (e) { return ''; }
  })();

  if (!list.length) {
    // 没有历史时整个区域留空，不放提示文案（用户不需要被告知"还没有对话"，
    // 空着本身就说明没内容，提示反而占了侧栏的位置）
    box.innerHTML = '';
    return;
  }

  const groups = new Map();
  list.forEach(s => {
    const b = sbBucket(Number(s.updatedAt) || Number(s.createdAt) || Date.now());
    if (!groups.has(b)) groups.set(b, []);
    groups.get(b).push(s);
  });

  let html = '';
  SB_BUCKET_ORDER.forEach(label => {
    const items = groups.get(label);
    if (!items || !items.length) return;
    html += `<div class="sb-label">${label}</div>`;
    items.forEach(s => {
      const on = s.id === activeId ? ' on' : '';
      html += `<div class="sb-hist${on}" data-id="${s.id}">`
        + `<a class="sb-hist__open" href="index.html?c=${encodeURIComponent(s.id)}" title="${escapeAttr(sbTitle(s))}">`
        + `<span class="sb-item__txt">${escapeAttr(sbTitle(s))}</span></a>`
        + `<button class="sb-hist__del" title="删除这条对话" aria-label="删除">×</button></div>`;
    });
  });
  box.innerHTML = html;

  box.querySelectorAll('.sb-hist__del').forEach(btn => {
    btn.addEventListener('click', ev => {
      ev.preventDefault();
      ev.stopPropagation();
      const row = btn.closest('.sb-hist');
      const id = row && row.dataset.id;
      if (!id) return;
      if (!confirm('删除这条对话？删除后不可恢复。')) return;
      try {
        const raw = JSON.parse(localStorage.getItem(SB_SESSIONS_KEY) || '{"sessions":[]}');
        raw.sessions = (raw.sessions || []).filter(s => s.id !== id);
        localStorage.setItem(SB_SESSIONS_KEY, JSON.stringify(raw));
        if (localStorage.getItem(SB_SESSIONS_KEY + ':active') === id) {
          localStorage.removeItem(SB_SESSIONS_KEY + ':active');
        }
      } catch (e) { /* 隐私模式写不进去就算了 */ }
      sbRenderHistory();
    });
  });
}

function escapeAttr(text) {
  return String(text == null ? '' : text)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// ---------- 窄屏抽屉（900px 以下） ----------
// 2026-10-31 加。窄屏下侧栏是 position:fixed 的覆盖式抽屉，靠 body.sb-open 控制进出。
// 桌面端这套类名不产生任何效果（对应的 CSS 全在 @media (max-width:900px) 里），
// 所以 mnavBtn / sbMask 在桌面上是隐藏元素，点了也只会留个无害的类。
function sbSetDrawer(open) {
  document.body.classList.toggle('sb-open', !!open);
  const btn = document.getElementById('mnavBtn');
  if (btn) btn.setAttribute('aria-expanded', open ? 'true' : 'false');
}

function sbDrawerOpen() {
  return document.body.classList.contains('sb-open');
}

function sbInitDrawer() {
  const btn = document.getElementById('mnavBtn');
  const mask = document.getElementById('sbMask');
  if (btn) btn.addEventListener('click', () => sbSetDrawer(!sbDrawerOpen()));
  if (mask) mask.addEventListener('click', () => sbSetDrawer(false));

  // Esc 关抽屉。只在真的开着的时候拦，避免抢走输入框自己的 Esc 行为
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && sbDrawerOpen()) sbSetDrawer(false);
  });

  // 窄屏里点任何一个导航项都该把抽屉收回去，否则新页面盖在抽屉底下看不见
  document.querySelectorAll('.sidebar a.sb-item, .sb-new').forEach(el => {
    el.addEventListener('click', () => {
      if (window.matchMedia('(max-width: 900px)').matches) sbSetDrawer(false);
    });
  });

  // 从窄屏转回桌面时，抽屉状态要清掉，否则 .sb-open 会残留在 body 上
  const mq = window.matchMedia('(max-width: 900px)');
  const onMq = () => { if (!mq.matches) sbSetDrawer(false); };
  if (mq.addEventListener) mq.addEventListener('change', onMq);
  else if (mq.addListener) mq.addListener(onMq);
}

// ---------- 折叠 ----------
function sbSetCollapsed(on) {
  document.body.classList.toggle('app--collapsed', !!on);
  try { localStorage.setItem(SB_COLLAPSE_KEY, on ? '1' : '0'); } catch (e) { /* ignore */ }
}

// ---------- 拖拽调宽 ----------
function sbApplyWidth(w) {
  document.documentElement.style.setProperty('--sidebar-w', w + 'px');
}

function sbClampWidth(w) {
  // 主区至少留 520px，否则窗口不大时拖宽会把内容挤到换行
  const room = Math.max(SB_MIN_WIDTH, window.innerWidth - 520);
  return Math.round(Math.min(Math.max(w, SB_MIN_WIDTH), Math.min(SB_MAX_WIDTH, room)));
}

function sbPersistWidth(w) {
  try { localStorage.setItem(SB_WIDTH_KEY, String(w)); } catch (e) { /* ignore */ }
}

function sbInitSidebar() {
  // 折叠态：侧栏始终保持折叠状态，跨页面一致
  let collapsed = false;
  try { collapsed = localStorage.getItem(SB_COLLAPSE_KEY) === '1'; } catch (e) { collapsed = false; }
  if (collapsed) document.body.classList.add('app--collapsed');

  const toggle = document.getElementById('sbToggle');
  const toggleAct = document.getElementById('sbToggleAct');
  if (toggle) toggle.addEventListener('click', () => sbSetCollapsed(true));
  if (toggleAct) toggleAct.addEventListener('click', () => sbSetCollapsed(false));
  document.addEventListener('keydown', e => {
    if (e.ctrlKey && e.shiftKey && (e.key === 'B' || e.key === 'b')) {
      e.preventDefault();
      sbSetCollapsed(!document.body.classList.contains('app--collapsed'));
    }
  });

  const resizer = document.getElementById('sbResizer');
  const sidebar = document.getElementById('sidebar');
  if (resizer && sidebar) {
    resizer.addEventListener('pointerdown', e => {
      if (document.body.classList.contains('app--collapsed')) return;
      e.preventDefault();
      const startX = e.clientX;
      const startW = Math.round(sidebar.getBoundingClientRect().width);
      resizer.classList.add('dragging');
      document.body.classList.add('is-resizing');
      resizer.setPointerCapture(e.pointerId);

      const onMove = ev => sbApplyWidth(sbClampWidth(startW + ev.clientX - startX));
      const onUp = () => {
        resizer.classList.remove('dragging');
        document.body.classList.remove('is-resizing');
        resizer.removeEventListener('pointermove', onMove);
        resizer.removeEventListener('pointerup', onUp);
        resizer.removeEventListener('pointercancel', onUp);
        sbPersistWidth(Math.round(sidebar.getBoundingClientRect().width));
      };
      resizer.addEventListener('pointermove', onMove);
      resizer.addEventListener('pointerup', onUp);
      resizer.addEventListener('pointercancel', onUp);
    });

    resizer.addEventListener('dblclick', () => {
      sbApplyWidth(SB_DEFAULT_WIDTH);
      sbPersistWidth(SB_DEFAULT_WIDTH);
    });
  }

  // 当前页高亮：页面在 body 上写 data-nav="library"，对应的 .sb-item[data-nav] 加 .on
  const nav = document.body.dataset.nav;
  if (nav) {
    document.querySelectorAll('.sb-item[data-nav]').forEach(el => {
      el.classList.toggle('on', el.dataset.nav === nav);
    });
  }

  // 「开启新对话」在独立页上就跳回对话页
  const newChat = document.getElementById('newChat');
  if (newChat && !newChat.dataset.bound) {
    newChat.dataset.bound = '1';
    newChat.addEventListener('click', () => { location.href = 'index.html'; });
  }

  sbInitDrawer();
  sbRenderHistory();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', sbInitSidebar);
} else {
  sbInitSidebar();
}

// 对话页会改动会话列表；从对话页回来时重新渲染一次（bfcache 恢复也会触发 pageshow）
window.addEventListener('pageshow', () => sbRenderHistory());