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

  // 回答分段标题。后端 app/agent.py 已固定要求模型输出这四个标题，
  // 顺序也固定为 结论 → 依据 → 局限 → 下一步，所以这里按这四个认。
  // 「其他」不是模型给的标题，是四个标题之外的零散内容（开场白之类）的兜底桶。
  var SECTIONS = [
    { key: 'conclusion', label: '结论' },
    { key: 'basis', label: '依据' },
    { key: 'limits', label: '局限' },
    { key: 'next', label: '下一步' },
    { key: 'rest', label: '其他' },
  ];
  var LABEL_TO_KEY = {};
  SECTIONS.forEach(function (s) { LABEL_TO_KEY[s.label] = s.key; });

  // 段落标题：行内任意位置都认（2026-11-16 实测模型把【结论】【依据】挤在同一行，
  // 只认「整行只有一个标题」的写法会把两段全吞进「其他」里，结论区就成了空标签）。
  // 方括号里只收 1~8 个汉字，正文里的 [来源: …] 这类引用标记不会被误认成标题。
  var SECTION_TAG_RE = /[【\[]\s*([^】\]\s]{1,8}?)\s*[】\]]\s*[:：]?\s*/g;

  // 外露的两段：用户要的「首尾」，其他（含依据/局限/其他/协作）全部折叠。
  var VISIBLE_KEYS = ['conclusion', 'next'];
  // 证据卡片默认显示几条，其余收进「展开全部」。
  var EVIDENCE_PREVIEW = 3;

  function escapeHtml(s) {
    return str(s).replace(/[&<>"']/g, function (c) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c];
    });
  }

  function str(s) {
    return s == null ? '' : String(s);
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
   *   1) 认【标题】，行内任意位置都算标题起点，标题后的文字属于该段；
   *   2) 第一个标题出现之前的内容是「开场白」，并进结论（归到「其他」会被藏起来，
   *      而中文回答里"推荐 X 这篇"这种结论经常就写在开头）；
   *   3) 四个标题一个都没认出来 → conclusion 回落成整段原文；
   *   4) 认出来了但某段没内容 → 那一段留空，调用方据此不画空壳。
   */
  function splitSections(text) {
    var out = { conclusion: '', basis: '', limits: '', next: '', rest: '' };
    var buckets = {};
    var cur = null;
    str(text).split('\n').forEach(function (line) {
      var kept = '';
      var from = 0;
      var m;
      SECTION_TAG_RE.lastIndex = 0;
      while ((m = SECTION_TAG_RE.exec(line))) {
        var key = LABEL_TO_KEY[m[1]];
        if (!key) continue;                     // [来源: …] 之类的方括号词，当正文
        kept += line.slice(from, m.index);
        if (cur) buckets[cur].push(kept);
        else out.conclusion += kept + '\n';
        kept = '';
        cur = key;
        buckets[cur] = buckets[cur] || [];
        from = m.index + m[0].length;
      }
      kept += line.slice(from);
      if (cur) buckets[cur].push(kept);
      else out.conclusion += kept + '\n';
    });

    var hit = Object.keys(buckets).length > 0;
    if (!hit) {
      out.conclusion = str(text).trim();
      out.next = '';
      out.basis = out.limits = out.rest = '';
      return out;
    }
    SECTIONS.forEach(function (s) {
      out[s.key] = (buckets[s.key] || []).join('\n').trim();
    });
    out.conclusion = out.conclusion.trim();
    return out;
  }

  // ---------- 回答卡片：结构只建一次 ----------
  /**
   * 建一张回答卡片的骨架：
   *   .ans-lead   结论（外露）
   *   .ans-fold   依据 / 局限 / 其他 / 多智能体协作（默认收起）
   *   .ans-tail   下一步（外露）
   *   .ans-toggle 展开/收起按钮
   *
   * 2026-11-16 修的三处：
   *   1) 原来用 innerHTML 拼字符串，正文没转义。模型/论文原文里一个半角引号就能让
   *      <div class="ans-lead"> 被提前截断，结论区变成空标签，后续节点全成兄弟——
   *      用户看到的就是「依据」直接摊在外面，点展开也没反应。改成全 DOM 构建，
   *      文字一律 textContent，从根上不可能再被截断。
   *   2) 多智能体协作原来单独露在外面，现在并进同一个折叠区。
   *   3) 非流式与流式两条路径共用这一个构造函数，两页两路行为不会再走形。
   */
  function buildAnswerCard(el) {
    el.textContent = '';
    el.classList.add('ans');

    var leadBody = document.createElement('div');
    leadBody.className = 'ans-lead__body';
    var lead = document.createElement('div');
    lead.className = 'ans-lead';
    var leadTag = document.createElement('b');
    leadTag.className = 'ans-tag';
    leadTag.textContent = '结论';
    lead.appendChild(leadTag);
    lead.appendChild(leadBody);

    var fold = document.createElement('div');
    fold.className = 'ans-fold';
    fold.hidden = true;
    var foldBody = {};
    [['basis', '依据'], ['limits', '局限'], ['rest', '其他'], ['roles', '协作']].forEach(function (p) {
      var wrap = document.createElement('div');
      wrap.className = 'ans-part';
      wrap.setAttribute('data-part', p[0]);
      wrap.hidden = true;
      var tag = document.createElement('b');
      tag.className = 'ans-tag';
      tag.textContent = p[1];
      var body = document.createElement('div');
      body.className = 'ans-part__body';
      wrap.appendChild(tag);
      wrap.appendChild(body);
      fold.appendChild(wrap);
      foldBody[p[0]] = body;
    });

    var tail = document.createElement('div');
    tail.className = 'ans-tail ans-tail--pending';
    var tailBody = document.createElement('div');
    var tailTag = document.createElement('b');
    tailTag.className = 'ans-tag';
    tailTag.textContent = '下一步';
    tail.appendChild(tailTag);
    tail.appendChild(tailBody);
    tailBody.textContent = '正在生成…';

    var toggle = document.createElement('button');
    toggle.className = 'ans-toggle';
    toggle.type = 'button';
    toggle.setAttribute('aria-expanded', 'false');
    toggle.hidden = true;

    el.appendChild(lead);
    el.appendChild(fold);
    el.appendChild(tail);
    el.appendChild(toggle);

    return {
      el: el, lead: lead, leadBody: leadBody, fold: fold, foldBody: foldBody,
      tail: tail, tailBody: tailBody, toggle: toggle,
    };
  }

  /**
   * 打开/收起折叠区。显隐以 aria-expanded 为唯一真相源：
   * refreshFold 只在「有没有内容」变化时才动它，否则每次重画都会把用户的展开动作抹掉
   * （2026-11-17 实测：点开之后 refreshFold 又把 fold.hidden 置回 true，看起来「点了没反应」）。
   */
  function setFoldOpen(card, open) {
    card.fold.hidden = !open;
    card.toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
  }

  /** 折叠区里某一段现在算不算「有内容」（依据/局限/其他看正文，协作看有没有时间线节点）。 */
  function partHas(wrap, key, foldBody) {
    if (key === 'roles') return !!wrap.querySelector('.roles');
    var body = foldBody[key];
    var txt = body ? str(body.textContent).trim() : '';
    // 正文里可能只有表格：renderMarkdown 会把表格塞进子节点，textContent 在部分实现下为空，
    // 所以「有子节点」也要算有内容（同一魔法数在 refreshFold 与 setTailState 里必须一致）
    return !!(txt || (body && body.childNodes && body.childNodes.length));
  }

  /** 按当前内容刷新折叠区（空段落不占位，全空就整个收掉按钮；不碰展开状态）。 */
  function refreshFold(card) {
    var shown = [];
    var prev = card.__shown || [];
    ['basis', 'limits', 'rest', 'roles'].forEach(function (k) {
      var wrap = card.fold.querySelector('[data-part="' + k + '"]');
      if (!wrap) return;
      var has = partHas(wrap, k, card.foldBody);
      wrap.hidden = !has;
      if (has) shown.push(k);
    });
    // 从「无内容」变成「有内容」时回到收起状态；只是内容变多则保留用户当前的展开动作
    if (!prev.length && shown.length) setFoldOpen(card, false);
    card.__shown = shown;
    var open = card.toggle.getAttribute('aria-expanded') === 'true';
    card.toggle.hidden = shown.length === 0;
    card.fold.hidden = shown.length === 0 || !open;
    card.toggle.textContent = open && shown.length
      ? '收起依据与局限'
      : ('展开依据与局限（' + shown.length + ' 段）');
  }

  function bindToggle(card) {
    if (card.toggle.__bound) return;
    card.toggle.__bound = true;
    card.toggle.addEventListener('click', function () {
      setFoldOpen(card, card.toggle.getAttribute('aria-expanded') !== 'true');
      refreshFold(card);
    });
  }

  /**
   * 画一条回答（非流式：历史消息回放）。
   * 与流式版 streamAnswer 共用同一套 DOM 骨架，两页两路展示一致。
   */
  function renderAnswer(el, text, opts) {
    opts = opts || {};
    var card = buildAnswerCard(el);
    var sec = splitSections(text);

    card.leadBody.textContent = sec.conclusion;
    // 四段之外还有零散内容时才挂「其他」，避免空壳
    card.foldBody.basis.textContent = sec.basis;
    card.foldBody.limits.textContent = sec.limits;
    card.foldBody.rest.textContent = sec.rest;
    card.tailBody.textContent = sec.next;
    setTailState(card, sec.next, true);
    bindToggle(card);
    refreshFold(card);
    return card;
  }

  /** 下一步区的三种状态：正常 / 还在写 / 本次没有。 */
  function setTailState(card, next, finished) {
    var txt = str(next).trim();
    card.tail.classList.remove('ans-tail--pending', 'ans-tail--empty');
    if (txt) {
      card.tailBody.textContent = txt;
    } else if (finished) {
      card.tail.classList.add('ans-tail--empty');
      card.tailBody.textContent = '本次回答没有给出下一步建议';
    } else {
      card.tail.classList.add('ans-tail--pending');
      card.tailBody.textContent = '正在生成…';
    }
  }

  // ---------- 流式渲染（边收边折叠） ----------
  // 2026-11-16 用户反馈：「结论和下一步必须第一时间显示，所有对话中」。原来的做法是
  // 流式期间 botEl.textContent = 全文，结束才切段渲染——于是左边整片滚动时用户看到的
  // 都是【依据】那段，结论要么早就滚走了，要么（对比页那轮）压根没渲染出来。
  //
  // 这里改成：第一次拿到正文就把结构一次性建好（结论在上、折叠区在中、下一步在下），
  // 之后每个增量只改对应节点的文字。结构不重建，所以文字不会跳位置。
  // 增量渲染是节流的（STREAM_RENDER_MS），既不让每来一个字就排版一次，也不至于卡顿。

  var STREAM_RENDER_MS = 80;

  /** 把切出来的段落抄回流式状态对象（流式与收尾共用，免得两处口径走样）。 */
  function assign(dst, src) {
    ['conclusion', 'basis', 'limits', 'rest', 'next'].forEach(function (k) {
      dst[k] = str(src[k]).replace(/\s+$/, '');
    });
    return dst;
  }

  /** 流式口子：把某个分区的当前全文画进对应节点（只在内容真变了时才重排）。 */
  function paintStreamPart(cache, key, text, render) {
    var body = String(text || '').replace(/\s+$/, '');
    if (cache.last[key] === body) return;
    cache.last[key] = body;
    render(cache.el[key], body);
    cache.dirty = true;
  }

  /**
   * 边收边折叠的渲染器。
   *   用法：var s = streamAnswer(botEl); s.push(累计全文); s.finish(); s.stop();
   * 返回 {push, finish, stop, foldRoles, el, sec, card}。
   *
   * 与 renderAnswer 共用 buildAnswerCard 建的同一套骨架：结构只建一次，
   * 增量只改对应节点的文字，所以文字不会在用户眼皮底下跳位置。
   */
  function streamAnswer(el) {
    var card = buildAnswerCard(el);
    var cache = {
      el: {
        conclusion: card.leadBody, basis: card.foldBody.basis,
        limits: card.foldBody.limits, rest: card.foldBody.rest, next: card.tailBody,
      },
      last: {},
    };
    var sec = { conclusion: '', basis: '', limits: '', rest: '', next: '' };
    var lastText = '';
    var frozen = false;       // finish 之后不再接受增量
    var timer = null;

    function textRender(node, text) { node.textContent = text; }

    // 折叠区里可能有 Markdown 对比表，必须走 renderMarkdown
    function foldRender(node, text) { renderMarkdown(node, text); }

    var PAINT = [
      ['conclusion', textRender], ['basis', foldRender], ['limits', foldRender],
      ['rest', foldRender], ['next', textRender],
    ];

    function paint() {
      PAINT.forEach(function (p) { paintStreamPart(cache, p[0], sec[p[0]], p[1]); });
      // 下一步还没出来时先说清楚"还在写"——否则用户以为这一段被吞了
      setTailState(card, sec.next, frozen);
      refreshFold(card);
      return cache.last;
    }

    function push(text) {
      if (frozen) return;
      if (str(text) === lastText) return;
      lastText = str(text);
      assign(sec, splitSections(lastText));
      if (timer) return;                    // 已经排了下一帧，等它一起画
      timer = setTimeout(function () {
        timer = null;
        if (frozen) return;
        paint();
      }, STREAM_RENDER_MS);
    }

    /** 收口：把剩下的全画上，并把按钮文案与下一步状态定下来。 */
    function finish() {
      if (timer) { clearTimeout(timer); timer = null; }
      assign(sec, splitSections(lastText));
      frozen = true;
      paint();
    }

    function stop() {
      if (timer) { clearTimeout(timer); timer = null; }
      frozen = true;
    }

    /** 把「多智能体协作」时间线并进折叠区（2026-11-16 用户要求一起收起）。 */
    function foldRoles(items, status) {
      var body = card.foldBody.roles;
      if (!items || !items.length) { refreshFold(card); return null; }
      body.textContent = '';
      var node = buildRoles(items, status);
      body.appendChild(node);
      refreshFold(card);
      return node;
    }

    bindToggle(card);
    refreshFold(card);
    return { push: push, finish: finish, stop: stop, foldRoles: foldRoles, el: el, sec: sec, card: card };
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

    // 用全库清单回答的问题（「一共有多少篇」「推荐一篇 X 的论文」）本来就不吃检索片段，
    // 证据为 0 是正常结果而不是"没找到"——这时不画任何东西，免得写一句
    // 「证据不足 · 论文库里没有找到能回答这个问题的段落」自相矛盾。
    if (opts.catalog) return null;

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
        (isOpenable(it.source, it.page)
          ? '<a class="ev-open" target="_blank" rel="noopener" href="' +
            escapeHtml(paperHref(it.source, it.page, it.text)) + '">打开原文</a>'
          : '<span class="ev-open ev-open--off" title="这条来自论文目录，不对应某一页 PDF，没有原文可跳">目录条目</span>') +
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

  /**
   * 这条证据能不能跳去看原文。
   *
   * 只有「真论文文件」才给链接：后端对「库里有哪些论文」这类目录问题会造一条
   * source="论文库清单" 的伪证据（没有页码、没有对应 PDF），拿它去拼阅读页链接，
   * 阅读页按 paper=论文库清单 找不到 .pdf，pdf.js 就报 Missing PDF
   * （2026-11-17 用户报的那条）。目录类渠道一律不给入口。
   */
  function isOpenable(source, page) {
    var src = str(source).trim();
    if (!src) return false;
    if (/\.pdf$/i.test(src)) return true;
    // 上传入库的论文可能只存了 paper_id（<id>.pdf），来源名未必带后缀，但有页码就说明是正文片段
    return page != null && !/清单|目录|catalog/i.test(src);
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
  /** 画「多智能体协作」时间线本体（不挂到 DOM 上，由调用方决定放哪）。 */
  function buildRoles(items, status) {
    if (!items || items.length === 0) return null;
    var box = document.createElement('div');
    box.className = 'roles';

    var head = document.createElement('div');
    head.className = 'roles-head';
    var title = document.createElement('b');
    title.textContent = '多智能体协作';
    head.appendChild(title);
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
    return box;
  }

  /**
   * 把协作时间线插到 botEl 后面（保留给只想单独画时间线的调用方）。
   * 对话页与对比页现在都走 streamAnswer().foldRoles()，把它并进折叠区。
   */
  function renderRoles(botEl, items, status) {
    var box = buildRoles(items, status);
    if (!box) return null;
    botEl.insertAdjacentElement('afterend', box);
    return box;
  }

  window.ChatCore = {
    EVIDENCE_PREVIEW: EVIDENCE_PREVIEW,
    escapeHtml: escapeHtml,
    renderMarkdown: renderMarkdown,
    splitSections: splitSections,
    renderAnswer: renderAnswer,
    streamAnswer: streamAnswer,
    paintStreamPart: paintStreamPart,
    STREAM_RENDER_MS: STREAM_RENDER_MS,
    renderEvidence: renderEvidence,
    renderRoles: renderRoles,
    buildRoles: buildRoles,
    buildAnswerCard: buildAnswerCard,
    setTailState: setTailState,
    paperHref: paperHref,
    isOpenable: isOpenable,
  };
})();