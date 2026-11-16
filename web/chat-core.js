/*
 * 对话页与专属对比页共用的一套渲染件（2026-11-16）。
 *
 * 为什么单独抽一个文件：这两个页面要「完全一致的回答展示」——只露【结论】和
 * 【下一步】，证据只露前 3 条，其余折叠。如果各写一份，改一次要改两处，
 * 迟早走形（局域图谱那轮就是"每轮都通过、用户看到的却是空白"）。
 *
 * 这里只放纯渲染函数，不碰会话存储与输入框：那些属于各页面自己的状态。
 * 所有函数都挂在 window.ChatCore 上，页面用普通 <script> 引入即可。
 */
(function () {
  'use strict';

  // 回答分段标题。后端 app/agent.py:84-91 已固定要求模型输出这四个标题，
  // 顺序也固定为 结论 → 依据 → 局限 → 下一步，所以这里按这四个认。
  var SECTIONS = [
    { key: 'conclusion', label: '结论' },
    { key: 'basis', label: '依据' },
    { key: 'limits', label: '局限' },
    { key: 'next', label: '下一步' },
  ];
  var LABEL_TO_KEY = {};
  SECTIONS.forEach(function (s) { LABEL_TO_KEY[s.label] = s.key; });

  // 默认外露的两段：用户要的「首尾」，其他隐藏。
  var VISIBLE_KEYS = ['conclusion', 'next'];
  // 证据卡片默认显示几条，其余收进「展开全部」。
  var EVIDENCE_PREVIEW = 3;

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c];
    });
  }

  // ---------- Markdown 表格 ----------
  function looksLikeRow(line) {
    return /^\s*\|.*\|\s*$/.test(line);
  }

  function buildTable(rows) {
    // 去掉 |---|---| 这种分隔行
    var body = rows.filter(function (r) { return !/^\s*\|[\s:|-]+\|\s*$/.test(r); });
    if (body.length < 2) return null;
    var splitCells = function (r) {
      return r.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(function (s) { return s.trim(); });
    };
    var table = document.createElement('table');
    table.className = 'cmp';
    body.forEach(function (row, idx) {
      var tr = document.createElement('tr');
      splitCells(row).forEach(function (text) {
        var cell = document.createElement(idx === 0 ? 'th' : 'td');
        cell.textContent = text;  // 用 textContent，不解析 HTML，防注入
        tr.appendChild(cell);
      });
      table.appendChild(tr);
    });
    // 外面套一层：手机上表格可以横向滚动，不把整页撑破
    var wrap = document.createElement('div');
    wrap.className = 'table-wrap';
    wrap.appendChild(table);
    return wrap;
  }

  /** 把一段带 Markdown 表格的纯文本画进 el（其余文字原样保留）。 */
  function renderMarkdown(el, text) {
    el.textContent = '';
    var lines = String(text || '').split('\n');
    var buffer = [];
    var flush = function () {
      if (!buffer.length) return;
      var block = document.createElement('div');
      block.textContent = buffer.join('\n');
      el.appendChild(block);
      buffer = [];
    };
    for (var i = 0; i < lines.length; i++) {
      if (looksLikeRow(lines[i])) {
        var rows = [];
        while (i < lines.length && looksLikeRow(lines[i])) { rows.push(lines[i]); i++; }
        i--;
        var table = buildTable(rows);
        if (table) { flush(); el.appendChild(table); }
        else { buffer.push.apply(buffer, rows); }
      } else {
        buffer.push(lines[i]);
      }
    }
    flush();
  }

  // ---------- 回答分段 ----------
  /**
   * 把回答切成 {conclusion, basis, limits, next, rest}。
   *
   * 容错规则（模型不一定听话，这里必须能优雅降级）：
   *   1) 只认段首的【标题】（允许前后有空白或 # 井号），正文里出现的方括号词不算；
   *   2) 四个标题一个都没认出来 → conclusion 回落成整段原文，其余为空；
   *   3) 认出来了但缺某段 → 那一段留空，调用方据此不画对应的折叠按钮。
   */
  function splitSections(text) {
    var out = { conclusion: '', basis: '', limits: '', next: '', rest: '' };
    var lines = String(text || '').split('\n');
    var cur = null;
    var buckets = {};
    var headRe = /^\s*(?:#{1,6}\s*)?[【\[]([^】\]]{1,8})[】\]]\s*:?\s*$/;
    lines.forEach(function (line) {
      var m = line.match(headRe);
      if (m && LABEL_TO_KEY[m[1].trim()]) {
        cur = LABEL_TO_KEY[m[1].trim()];
        buckets[cur] = buckets[cur] || [];
        return;
      }
      if (cur) buckets[cur].push(line);
      else out.rest += line + '\n';
    });
    var hit = Object.keys(buckets).length > 0;
    if (!hit) {
      out.conclusion = String(text || '').trim();
      return out;
    }
    SECTIONS.forEach(function (s) {
      out[s.key] = (buckets[s.key] || []).join('\n').trim();
    });
    out.rest = out.rest.trim();
    return out;
  }

  /**
   * 画一条回答：外露【结论】+【下一步】，中间用「展开」按钮收【依据】【局限】。
   *
   * 没有可折叠内容时（模型只给了结论），不画按钮——空按钮比没有按钮更让人困惑。
   * 【结论】也没有时，退化为原文首段，保证永远有东西可看。
   */
  function renderAnswer(el, text, opts) {
    opts = opts || {};
    el.textContent = '';
    el.classList.add('ans');

    var sec = splitSections(text);
    var hiddenParts = [];
    if (sec.basis) hiddenParts.push({ label: '依据', body: sec.basis });
    if (sec.limits) hiddenParts.push({ label: '局限', body: sec.limits });
    // 四个标题之外的零散内容（开场白之类）跟着折叠，不然它会顶在结论上面
    if (sec.rest) hiddenParts.push({ label: '其他', body: sec.rest });

    var leadHtml = '<div class="ans-lead"><b class="ans-tag">结论</b>' + escapeHtml(sec.conclusion) + '</div>';
    var tailHtml = sec.next
      ? '<div class="ans-tail"><b class="ans-tag">下一步</b>' + escapeHtml(sec.next) + '</div>'
      : '<div class="ans-tail ans-tail--empty">本次回答没有给出下一步建议</div>';

    var foldHtml = '';
    if (hiddenParts.length) {
      foldHtml =
        '<div class="ans-fold" hidden>' +
        hiddenParts.map(function (p) {
          return '<div class="ans-part"><b class="ans-tag">' + escapeHtml(p.label) + '</b>' +
            '<div class="ans-part__body"></div></div>';
        }).join('') +
        '</div>';
    }

    var toggleHtml = hiddenParts.length
      ? '<button class="ans-toggle" type="button" aria-expanded="false">展开依据与局限（' +
        hiddenParts.length + ' 段）</button>'
      : '';

    el.innerHTML = leadHtml + foldHtml + tailHtml + toggleHtml;

    // 折叠区里的正文逐块渲染（要解析表格，不能用 innerHTML 拼）
    var bodies = el.querySelectorAll('.ans-fold .ans-part__body');
    hiddenParts.forEach(function (p, i) {
      if (bodies[i]) renderMarkdown(bodies[i], p.body);
    });

    var fold = el.querySelector('.ans-fold');
    var btn = el.querySelector('.ans-toggle');
    if (btn && fold) {
      btn.addEventListener('click', function () {
        var open = fold.hidden;
        fold.hidden = !open;
        btn.setAttribute('aria-expanded', open ? 'true' : 'false');
        btn.textContent = open ? '收起依据与局限' : ('展开依据与局限（' + hiddenParts.length + ' 段）');
      });
    }
  }

  // ---------- 证据卡片 ----------
  /**
   * 画证据卡片。
   *   preview：默认显示几张（不传则用 EVIDENCE_PREVIEW=3），其余收进「展开全部」。
   *
   * score 有两种口径，后端用 score_type 区分（app/agent.py:1514）：
   *   rrf    —— RRF 融合分，由多路名次换算，恒为正，不是"相似度概率"
   *   rerank —— 交叉编码器的原始 logit，可为负，越大越相关
   * 二者不可比，标签必须分开写；否则用户看到"融合分 -0.0777"会以为融合分能是负的。
   */
  function renderEvidence(botEl, items, opts) {
    opts = opts || {};
    var preview = Number.isFinite(opts.preview) ? opts.preview : EVIDENCE_PREVIEW;

    var box = document.createElement('div');
    box.className = 'evidence';

    if (!items || items.length === 0) {
      var miss = document.createElement('div');
      miss.className = 'ev miss';
      miss.innerHTML = '<div class="ev-head"><b>证据不足</b><span class="ev-badge">未检索到原文</span></div>' +
        '<div class="ev-text open">论文库里没有找到能回答这个问题的段落，因此系统应当拒答，而不是编造。</div>';
      box.appendChild(miss);
      botEl.insertAdjacentElement('afterend', box);
      return box;
    }

    var cards = [];
    items.forEach(function (it) {
      var card = document.createElement('div');
      card.className = 'ev';
      var channels = (it.channels || []).join(' + ') || '未知';
      var isRerank = it.score_type === 'rerank';
      var scoreLabel = isRerank ? '重排分' : '融合分';
      var scoreTip = isRerank
        ? '交叉编码器对"问题—原文"打的原始分，可为负，越大越相关'
        : '多路召回按名次融合得到的分，恒为正，不是相似度概率';
      // 点一下翻到原文那一页，并让阅读页把这句话高亮出来（hl=1）。
      // 位置参数由 paperHref 统一拼，对比页/主页两处调用口径一致。
      card.innerHTML =
        '<div class="ev-head">' +
        '<b>证据 #' + escapeHtml(it.rank) + '</b>' +
        '<span>第 ' + (it.page == null ? '?' : escapeHtml(it.page)) + ' 页</span>' +
        '<span title="' + escapeHtml(scoreTip) + '">' + scoreLabel + ' ' + Number(it.score).toFixed(4) + '</span>' +
        '<span>命中通道 ' + escapeHtml(channels) + '</span>' +
        '<span class="ev-badge">' + escapeHtml(it.source) + '</span>' +
        '<a class="ev-open" target="_blank" rel="noopener" href="' +
        escapeHtml(paperHref(it.source, it.page, it.text)) + '">打开原文</a>' +
        '</div>' +
        '<div class="ev-text">' + escapeHtml(it.text).slice(0, 400) + (it.text.length > 400 ? '…' : '') + '</div>';
      // 点一下把长证据展开/收起
      card.classList.add('ev-clickable');
      card.addEventListener('click', function (e) {
        if (e && e.target && e.target.closest && e.target.closest('a')) return;  // 别拦「打开原文」
        card.classList.toggle('open');
      });
      cards.push(card);
    });

    var shown = Math.max(1, Math.min(preview, cards.length));
    cards.forEach(function (c, i) {
      if (i >= shown) c.classList.add('ev--more');
      box.appendChild(c);
    });

    if (cards.length > shown) {
      var more = document.createElement('button');
      more.className = 'ev-more';
      more.type = 'button';
      more.textContent = '展开全部 ' + cards.length + ' 条证据（还有 ' + (cards.length - shown) + ' 条）';
      more.addEventListener('click', function () {
        box.querySelectorAll('.ev--more').forEach(function (c) { c.classList.remove('ev--more'); });
        more.remove();
      });
      box.appendChild(more);
    }

    botEl.insertAdjacentElement('afterend', box);
    return box;
  }

  /** 阅读页链接：paper 必带，page 只在有页码时带，hl=1 让阅读页把命中段落高亮。 */
  function paperHref(source, page, text) {
    var p = '/ui/reader.html?paper=' + encodeURIComponent(source || '');
    if (page != null) p += '&page=' + encodeURIComponent(page);
    p += '&hl=1';
    // 证据片段的前 60 个字给阅读页做兜底定位（页面本身不一定能按页精确命中段落）
    var needle = String(text || '').replace(/\s+/g, ' ').trim().slice(0, 60);
    if (needle) p += '&q=' + encodeURIComponent(needle);
    return p;
  }

  // ---------- 角色时间线 ----------
  function renderRoles(botEl, items, status) {
    if (!items || items.length === 0) return null;
    var box = document.createElement('div');
    box.className = 'roles';

    var head = document.createElement('div');
    head.className = 'roles-head';
    head.innerHTML = '<b>多智能体协作</b>';
    if (status) {
      var st = document.createElement('span');
      st.className = 'role-status';
      st.textContent = status;
      head.appendChild(st);
    }
    box.appendChild(head);

    var list = document.createElement('div');
    list.className = 'role-list';
    items.forEach(function (it) {
      var row = document.createElement('div');
      row.className = 'role' + (it.ok === false ? ' fail' : '');
      var ms = Number(it.duration_ms || 0);
      row.innerHTML =
        '<div class="role-dot"><i></i><span></span></div>' +
        '<div class="role-body">' +
        '<div class="role-title"><b>' + escapeHtml(it.label || it.role) + '</b>' +
        (ms ? '<span class="role-ms">' + ms + ' ms</span>' : '') +
        '</div>' +
        '<div class="role-text">' + escapeHtml(it.summary || '') + '</div>' +
        '</div>';
      list.appendChild(row);
    });
    box.appendChild(list);
    botEl.insertAdjacentElement('afterend', box);
    return box;
  }

  window.ChatCore = {
    EVIDENCE_PREVIEW: EVIDENCE_PREVIEW,
    escapeHtml: escapeHtml,
    renderMarkdown: renderMarkdown,
    splitSections: splitSections,
    renderAnswer: renderAnswer,
    renderEvidence: renderEvidence,
    renderRoles: renderRoles,
    paperHref: paperHref,
  };
})();