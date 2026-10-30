/* ==========================================================================
   图表取色工具（analysis.html / graph.html 共用同一份逻辑）
   --------------------------------------------------------------------------
   为什么要有这个文件：
     ECharts 的 color / lineStyle.color 等选项只认真实色值字符串，
     不解析 CSS 的 var(--x)。所以图表要跟设计系统联动，只能在 JS 里
     把变量的计算值读出来，再喂给 ECharts。

   为什么不用模块化 import：
     全站是纯静态页面、没有构建步骤。挂到 window 上最简单，
     两个页面各引一次 <script>，改一处两边都生效。

   变量读不到怎么办（比如 design-system.css 没加载）：
     每个变量都配了兜底值，兜底值与 design-system.css 里的浅色值一致，
     所以最坏情况和改造前的配色完全相同，不会出现"图表全黑/全白"。

   使用：
     const C = ChartColors.read();
     C.brand   → '#1a73e8'
     C.bgPage  → '#f3f5f8'
   ========================================================================== */
(function () {
  'use strict';

  /* 变量名 → 兜底值。兜底值必须与 design-system.css 浅色主题的值保持一致，
     否则 CSS 加载失败时图表会变形。 */
  var FALLBACK = {
    /* 空白与底色（图表本身用白底，画在卡片里） */
    '--bg-surface':   '#ffffff',
    '--bg-page':      '#f3f5f8',
    '--bg-surface-2': '#f8fafc',

    /* 边框与网格 */
    '--border-color': '#e3e7ee',
    '--border-strong': '#d5dbe3',
    '--border-focus': '#1a73e8',

    /* 文字 */
    '--text-primary':   '#1f2937',
    '--text-secondary': '#374151',
    '--text-muted':     '#5c6672',
    '--text-subtle':    '#9ca3af',

    /* 品牌与状态 */
    '--brand':       '#1a73e8',
    '--brand-hover': '#1667d0',
    '--success':     '#1a7f37',
    '--warning':     '#8a5a00',
    '--danger':      '#b3261e',

    /* 图表专用色 */
    '--chart-axis':    '#d5dbe5',
    '--chart-grid':    '#f1f4f8',
    '--chart-label':   '#5c6672',
    '--chart-label-2': '#9ca3af',
    '--chart-brand':  '#1a73e8',
    '--chart-accent': '#9333ea',
    '--chart-danger': '#dc2626',
    '--chart-c1':  '#1f77b4',
    '--chart-c2':  '#ff7f0e',
    '--chart-c3':  '#2ca02c',
    '--chart-c4':  '#d62728',
    '--chart-c5':  '#9467bd',
    '--chart-c6':  '#8c564b',
    '--chart-c7':  '#e377c2',
    '--chart-c8':  '#7f7f7f',
    '--chart-c9':  '#bcbd22',
    '--chart-c10': '#17becf',
    '--chart-ring': '#f5a623'
  };

  /* 一次读取全部变量，返回 {brand: '#1a73e8', ...} 形式的对象。
     key 去掉前面的 '--' 并转成小驼峰：--chart-c1 → chartC1 */
  function read() {
    var out = {};
    var cs = null;
    try {
      cs = getComputedStyle(document.documentElement);
    } catch (e) {
      cs = null;
    }
    for (var name in FALLBACK) {
      if (!Object.prototype.hasOwnProperty.call(FALLBACK, name)) continue;
      var v = '';
      if (cs) {
        v = cs.getPropertyValue(name);
        v = v ? v.trim() : '';
      }
      out[camel(name)] = v || FALLBACK[name];
    }
    return out;
  }

  /* --chart-c1 → chartC1 ; --bg-surface-2 → bgSurface2 */
  function camel(name) {
    return name.replace(/^--/, '').replace(/-([a-z0-9])/g, function (_, ch) {
      return ch.toUpperCase();
    });
  }

  /* 社区聚类配色数组，顺序固定为 c1..c10。
     顺序不能变：同一篇论文在两次渲染里必须同色。 */
  function communityPalette(C) {
    return [C.chartC1, C.chartC2, C.chartC3, C.chartC4, C.chartC5,
            C.chartC6, C.chartC7, C.chartC8, C.chartC9, C.chartC10];
  }

  window.ChartColors = {
    read: read,
    communityPalette: communityPalette,
    fallback: FALLBACK
  };
})();