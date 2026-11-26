/* ★2026-08-13 #289 深色侧边栏 → ★2026-08-15 StyleKit Linear Style
   Sidebar: 256px, bg #111114, nav labels #a1a1aa,
   active item: #5e6ad2 left border + rgba(94,106,210,.2)→#1c1c22 bg.
   底部：数据日 + 择时（读 portal_dash，LW.api 冗余兜底） */
(function () {
  var DEFAULT_ITEMS = [
    { id: 'portal',   label: '门户',   ic: 'home',     url: '/' },
    { id: 'control',  label: '控制',   ic: 'cpu',      url: '/control' },
    { id: 'pitch',    label: '决策',   ic: 'target',   url: '/pitch' },
    { id: 'holdings', label: '持仓',   ic: 'bar',      url: '/holdings' },
    { id: 'factors',  label: '因子',   ic: 'molecule', url: '/factors' },
    { id: 'backtest', label: '回测',   ic: 'chart',    url: '/backtest' },
    { id: 'pitchtrack', label: '远期', ic: 'funnel', url: '/pitchtrack' }
  ];
  // ★P2 菜单收敛（2026-08-15）：主菜单 +「更多」分组（兜底值；优先 /api/live/nav → config/ui.yaml）
  var DEFAULT_MORE = [
    { id: 'report',  label: '日报', ic: 'doc',  url: '/report' },
    { id: 'data',    label: '数据', ic: 'db',   url: '/data' },
    { id: 'help',    label: '说明', ic: 'help', url: '/help' }
  ];

  window.LW = window.LW || {};
  LW.sidebar = {
    _items: null,
    _more: null,
    render: function (elId, opts) {
      var self = this;
      if (self._items) return self._render(elId, opts);
      if (LW.api) {
        // ★2026-11-11：菜单/文案走 /api/live/nav（config/ui.yaml），JS 仅保留兜底
        LW.api.get('/api/live/nav').then(function (nav) {
          if (nav && nav.main && nav.main.length) {
            self._items = nav.main;
            self._more = nav.more || [];
          }
          self._render(elId, opts);
        }).catch(function () { self._render(elId, opts); });
        return;
      }
      return self._render(elId, opts);
    },
    _render: function (elId, opts) {
      var el = document.getElementById(elId);
      if (!el) return;
      var IC = LW.icons || {};
      var active = (opts && opts.active) || 'portal';
      var html =
        '<div class="sb">' +
          '<div class="sb-brand" title="Deepseek HARNESS Quant">' +
            '<span class="sb-brand-text">Deepseek HARNESS <span style="font-weight:400">Quant</span></span>' +
          '</div>' +
          '<nav class="sb-nav">' +
            (this._items || DEFAULT_ITEMS).map(function (it) {
              var cls = it.id === active ? 'sb-item active' : 'sb-item';
              return '<a class="' + cls + '" href="' + it.url + '">' + (IC[it.ic] || '') + it.label + '</a>';
            }).join('') +
            /* ★2026-08-16 「更多」默认折叠：点击标签展开（grid-rows 动画），箭头旋转指示 */
            '<div class="sb-sub-label sb-more-toggle" id="' + elId + '-more-toggle" title="展开/收起更多">' +
              '<span>更多</span><span class="sb-more-chev">▸</span></div>' +
            '<div class="sb-more-body" id="' + elId + '-more-body" style="display:grid;grid-template-rows:0fr;transition:grid-template-rows .22s var(--ease-hover)">' +
              '<div style="overflow:hidden">' +
                (this._more || DEFAULT_MORE).map(function (it) {
                  var cls = it.id === active ? 'sb-item active sb-sub-item' : 'sb-item sb-sub-item';
                  return '<a class="' + cls + '" href="' + it.url + '">' + (IC[it.ic] || '') + it.label + '</a>';
                }).join('') +
              '</div>' +
            '</div>' +
          '</nav>' +
          '<div class="sb-search" id="' + elId + '-srch"></div>' +
          '<div class="sb-foot" id="' + elId + '-foot">' +
            '<span class="sb-mini">数据日 …</span>' +
            '<span class="sb-mini">择时 …</span>' +
          '</div>' +
        '</div>';
      el.innerHTML = html;
      // ★2026-08-16 「更多」默认折叠：点击标签展开/收起（箭头旋转指示，动画流畅）
      var moreToggle = document.getElementById(elId + '-more-toggle');
      if (moreToggle) {
        moreToggle.addEventListener('click', function () {
          var body = document.getElementById(elId + '-more-body');
          var chev = moreToggle.querySelector('.sb-more-chev');
          var open = moreToggle.classList.toggle('open');
          if (body) body.style.gridTemplateRows = open ? '1fr' : '0fr';
          if (chev) chev.style.transform = open ? 'rotate(90deg)' : '';
        });
      }
      // 单股搜索（#311：全站可搜——依赖 search.js，未加载则跳过）
      try { if (LW.search) LW.search.render(elId + '-srch', '搜索股票代码/名称'); } catch (e) {}
      // ★#312 顶部滚动信息栏（#166 动态 ticker 恢复——彭博式黑底滚动）
      // ★2026-08-16 只保留黑色行情滚动条（lt-bar）；轮播提示条（lt-rotator）已删除——用户要求
      try {
        var wrap = document.querySelector('.v2-wrap');
        if (wrap && !document.getElementById('lt-inject')) {
          var holder = document.createElement('div');
          holder.id = 'lt-inject';
          holder.innerHTML = '<div class="lt-bar" style="margin-bottom:6px"></div>';
          wrap.insertBefore(holder, wrap.firstChild);
          var s = document.createElement('script');
          s.src = '/live_ticker.js';
          document.head.appendChild(s);
        }
      } catch (e) {}
      // ★2026-08-16 构建模式徽章：/api/build_mode 返回 dev 时显示「DEV 开发版」（release/失败隐藏）
      LW.api.get('/api/build_mode').then(function (bm) {
        var badge = document.getElementById(elId + '-devbadge');
        if (!badge) return;
        if (bm && bm.mode === 'dev') {
          badge.style.display = 'inline-block';
          badge.title = bm.hint || '内部测试构建，非公测发布版';
        }
      }).catch(function () {});
      LW.api.get('/api/live/portal_dash').then(function (d) {
        var tm = (d.timing && d.timing.timing) || {};
        var dd = d.data_date || (d.brief && d.brief.date) || tm.date || '';
        var f = document.getElementById(elId + '-foot');
        if (!f) return;
        var sc = tm.score != null ? tm.score : '…';
        var lv = tm.level || '';
        f.innerHTML =
          '<span class="sb-mini">数据日 ' + dd + '</span>' +
          '<span class="sb-mini">择时 ' + sc + (lv ? ' · ' + lv : '') + '</span>';
      }).catch(function () {});
    }
  };
})();

/* ★2026-08-15 可调宽度侧栏：拖拽右侧手柄调宽（200-420px），宽度记忆 localStorage，
   --sb-w 联动 .sb 宽度 与 body 左偏移（app.css 同源变量） */
(function () {
  var KEY = 'quant.sb.w';
  var MIN = 200, MAX = 420, DEF = 256;
  function applyW(px) {
    var v = Math.max(MIN, Math.min(MAX, px));
    document.documentElement.style.setProperty('--sb-w', v + 'px');
    try { localStorage.setItem(KEY, String(v)); } catch (e) {}
    return v;
  }
  function initResize() {
    var sb = document.querySelector('.sb');
    if (!sb) return;
    var saved = null;
    try { saved = parseInt(localStorage.getItem(KEY), 10); } catch (e) {}
    if (saved && saved >= MIN && saved <= MAX) applyW(saved);
    var handle = document.createElement('div');
    handle.className = 'sb-resize';
    handle.title = '拖拽调整侧栏宽度';
    sb.appendChild(handle);
    handle.addEventListener('mousedown', function (ev) {
      ev.preventDefault();
      var startX = ev.clientX;
      var startW = sb.getBoundingClientRect().width;
      handle.classList.add('dragging');
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
      function move(e2) { applyW(startW + (e2.clientX - startX)); }
      function up() {
        handle.classList.remove('dragging');
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
        document.removeEventListener('mousemove', move);
        document.removeEventListener('mouseup', up);
      }
      document.addEventListener('mousemove', move);
      document.addEventListener('mouseup', up);
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initResize);
  else initResize();
})();

/* ★2026-08-15 侧栏折叠：折叠后右侧内容区全屏（body.sb-collapsed → padding-left:0 + 侧栏移出），
   brand 行折叠按钮 + 悬浮展开按钮，状态记忆 localStorage（quant.sb.collapsed）
   ★2026-08-16 重构：①幂等（同状态直接返回，不重复触发）②不覆盖 --sb-w（宽度记忆永不被破坏）
   ③内容区 padding 与侧栏 transform 同速过渡（动画同步流畅）④动画期间忽略再次点击（busy 锁） */
(function () {
  var COLL = 'quant.sb.collapsed';
  var busy = false;
  function setCollapsed(c) {
    c = !!c;
    if (busy) return;                                   // 动画中忽略重复触发
    if (document.body.classList.contains('sb-collapsed') === c) return;  // 幂等
    busy = true;
    document.body.classList.toggle('sb-collapsed', c);
    try { localStorage.setItem(COLL, c ? '1' : '0'); } catch (e) {}
    var open = document.getElementById('sb-toggle-open');
    if (open) open.style.display = c ? 'flex' : 'none';
    setTimeout(function () { busy = false; }, 260);     // 与 CSS transition .22s 对齐
  }
  function initFold() {
    var sb = document.querySelector('.sb');
    if (!sb) return;
    var brand = sb.querySelector('.sb-brand');
    if (brand) {
      var foldBtn = document.createElement('button');
      foldBtn.className = 'sb-fold-btn';
      foldBtn.title = '折叠侧栏（内容区全屏）';
      foldBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="M15 6l-6 6 6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
      foldBtn.addEventListener('click', function () { setCollapsed(true); });
      brand.appendChild(foldBtn);
    }
    var open = document.getElementById('sb-toggle-open');
    if (!open) {
      open = document.createElement('button');
      open.id = 'sb-toggle-open';
      open.className = 'sb-toggle-open';
      open.title = '展开侧栏';
      open.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none"><path d="M9 6l6 6-6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
      open.addEventListener('click', function () { setCollapsed(false); });
      document.body.appendChild(open);
    }
    var saved = null;
    try { saved = localStorage.getItem(COLL); } catch (e) {}
    // 初始恢复直接应用（不经过 busy 锁，避免首帧闪烁）
    busy = true;
    document.body.classList.toggle('sb-collapsed', saved === '1');
    open.style.display = saved === '1' ? 'flex' : 'none';
    setTimeout(function () { busy = false; }, 260);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initFold);
  else initFold();
})();
