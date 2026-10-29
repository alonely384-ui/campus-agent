/* 划词翻译浮钮 —— 阅读器里已跑通的那套交互，抽出来给其它页面复用。
 *
 * 用法见 quick-translate.css 顶部注释。
 */
(function () {
  'use strict';

  // 后端 /translate/selection 的 text 字段卡 max_length=6000，超了回 422。
  var MAX_CHARS = 6000;
  var MIN_CHARS = 4;   // 选一两个字多半是误触，不值得弹按钮

  var btn = null;
  var card = null;
  var containers = [];
  var activeText = '';

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  // 极简 Markdown：只处理段落和加粗。译文里出现表格/公式的概率很低，
  // 为它引一个解析库不划算。
  function renderMd(md) {
    var esc = escapeHtml(md);
    return esc
      .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
      .split(/\n{2,}/)
      .filter(function (p) { return p.trim(); })
      .map(function (p) { return '<p>' + p.replace(/\n/g, '<br>') + '</p>'; })
      .join('');
  }

  function ensureDom() {
    if (btn) return;
    btn = document.createElement('button');
    btn.id = 'qtBtn';
    btn.type = 'button';
    btn.textContent = '译';
    btn.hidden = true;
    document.body.appendChild(btn);

    card = document.createElement('div');
    card.id = 'qtCard';
    card.hidden = true;
    document.body.appendChild(card);

    btn.addEventListener('click', function (ev) {
      ev.preventDefault();
      ev.stopPropagation();
      if (card.hidden) {
        translateActive(btn.getBoundingClientRect());
      } else {
        hideCard();
      }
    });

    card.addEventListener('mousedown', function (ev) {
      // 卡片内部点击不能触发「点空白关闭」
      ev.stopPropagation();
    });
    card.addEventListener('click', function (ev) {
      ev.stopPropagation();
      if (ev.target && ev.target.dataset && ev.target.dataset.qtClose !== undefined) {
        hideCard();
      }
    });

    document.addEventListener('mousedown', function (ev) {
      if (card && !card.hidden && !card.contains(ev.target) && ev.target !== btn) {
        hideCard();
      }
    });

    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape') {
        if (card && !card.hidden) { hideCard(); return; }
        if (btn && !btn.hidden) { btn.hidden = true; }
      }
    });

    window.addEventListener('scroll', function () {
      btn.hidden = true;
    }, true);

    window.addEventListener('resize', function () {
      btn.hidden = true;
      hideCard();
    });
  }

  function hideCard() {
    if (card) card.hidden = true;
  }

  function insideContainer(node) {
    for (var i = 0; i < containers.length; i++) {
      if (containers[i] && containers[i].contains(node)) return true;
    }
    return false;
  }

  function selectedText() {
    var sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return '';
    var range = sel.getRangeAt(0);
    if (!insideContainer(range.commonAncestorContainer)) return '';
    return sel.toString().replace(/\s+/g, ' ').trim();
  }

  function onMouseUp(ev) {
    // 浮钮/卡片自己的点击不参与选区判定
    if ((btn && btn.contains(ev.target)) || (card && card.contains(ev.target))) return;

    var text = selectedText();
    if (text.length < MIN_CHARS) {
      btn.hidden = true;
      return;
    }
    activeText = text.slice(0, MAX_CHARS);
    btn.style.left = clamp(ev.clientX - 10, 8, window.innerWidth - 56) + 'px';
    btn.style.top = clamp(ev.clientY + 14, 60, window.innerHeight - 44) + 'px';
    btn.hidden = false;
  }

  function placeCard(rect) {
    var w = Math.min(460, window.innerWidth - 24);
    var left = clamp(rect.left, 12, window.innerWidth - w - 12);
    var top = rect.bottom + 8;
    var maxH = Math.min(420, window.innerHeight - 80);
    // 下方放不下就翻到上方
    if (top + 200 > window.innerHeight) {
      top = Math.max(12, rect.top - maxH - 8);
    }
    card.style.left = left + 'px';
    card.style.top = top + 'px';
    card.style.maxHeight = maxH + 'px';
  }

  function translateActive(rect) {
    if (!activeText) return;
    placeCard(rect);
    card.hidden = false;
    card.innerHTML =
      '<div class="qt-head"><span>译文</span><span class="qt-pin">' + activeText.length + ' 字符</span>' +
      '<button class="qt-close" type="button" data-qt-close="" title="关闭">×</button></div>' +
      '<div class="qt-src">' + escapeHtml(activeText.slice(0, 300)) +
      (activeText.length > 300 ? '…' : '') + '</div>' +
      '<div class="qt-out busy">翻译中…</div>';

    var body = card.querySelector('.qt-out');
    fetch('/translate/selection', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: activeText })
    }).then(function (r) {
      if (!r.ok) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          throw new Error(j.detail || ('HTTP ' + r.status));
        });
      }
      return r.json();
    }).then(function (d) {
      var zh = (d.markdown || '').trim();
      if (!zh) throw new Error('模型没有返回译文');
      body.className = 'qt-out';
      body.innerHTML = renderMd(zh);
    }).catch(function (e) {
      body.className = 'qt-out err';
      body.textContent = '翻译失败：' + String((e && e.message) || e);
    });
  }

  function attach() {
    var nodes = Array.prototype.slice.call(arguments).filter(Boolean);
    if (!nodes.length) return;
    ensureDom();
    nodes.forEach(function (n) {
      if (containers.indexOf(n) === -1) containers.push(n);
    });
    if (!attach._bound) {
      document.addEventListener('mouseup', onMouseUp);
      attach._bound = true;
    }
  }

  window.QuickTranslate = { attach: attach, close: hideCard };
})();