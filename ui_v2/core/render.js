/* ui_v2/core/render.js — 渲染引擎（UI v2 标准 #18 冗余容错）
   功能：
     1. _f(v, d)   字段兜底——null/undefined/NaN  默认值 '—'（B2 教训：字段缺失不白屏）
     2. pct(v, d)  小数百分数（0.07  7%，None  '—'）（#271 教训：×100 高频 bug）
     3. color(v)   涨跌色（A 股习惯：涨红跌绿，var(--red)/var(--green)）
     4. mount(box, html) 安全挂载（box 不存在不报错）
   全局 LW.render。所有页面渲染必须走这里——统一兜底与配色。*/
(function (win) {
  'use strict';

  // HTML 转义（防存储型 XSS：数据字段拼进 innerHTML 前必须转义）
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function _f(v, d) {
    if (v === null || v === undefined) return d !== undefined ? d : '—';
    if (typeof v === 'number' && isNaN(v)) return d !== undefined ? d : '—';
    if (v === '') return d !== undefined ? d : '—';
    // ★2026-10-19 M-1 修复：字符串值统一转义，避免 code/name/note 等字段触发 XSS
    return typeof v === 'string' ? esc(v) : v;
  }

  // 小数  百分数（0.07  '7%'）；null  '—'
  function pct(v, digits) {
    digits = digits || 1;
    if (v === null || v === undefined || isNaN(v)) return '—';
    return (v * 100).toFixed(digits) + '%';
  }

  // 涨跌色（A 股：涨红跌绿）
  function color(v) {
    return v > 0 ? 'var(--red)' : v < 0 ? 'var(--green)' : 'var(--muted)';
  }

  function mount(boxId, html) {
    var box = document.getElementById(boxId);
    if (!box) return false;
    box.innerHTML = html;
    return true;
  }

  // 表格骨架（统一列头/空态/错误态）
  function table(cols, rowsHtml, emptyTxt) {
    var head = '<tr>' + cols.map(function (c) { return '<th>' + c + '</th>'; }).join('') + '</tr>';
    var body = rowsHtml || '<tr><td colspan="' + cols.length + '" class="mini" style="text-align:center;opacity:.6">' + (emptyTxt || '暂无数据') + '</td></tr>';
    return '<table style="width:100%;border-collapse:collapse;font-size:12px"><thead>' + head + '</thead><tbody>' + body + '</tbody></table>';
  }

  /* #289 spark 迷你走势图（规范：KPI 卡带 12 点 sparkline）
     svg 100x28 视口：折线 + 端点色（A股红涨绿跌）；点数 <2 显示占位 */
  function spark(series, color) {
    if (!series || series.length < 2) {
      return '<svg viewBox="0 0 100 28" style="width:100%;height:28px;display:block">' +
        '<rect width="100" height="28" fill="none"/>' +
        '<text x="50" y="18" text-anchor="middle" font-size="9" fill="#71717a">数据积累中</text></svg>';
    }
    var min = Math.min.apply(null, series);
    var max = Math.max.apply(null, series);
    var span = (max - min) || 1;
    var pts = series.map(function (v, i) {
      var x = (i / (series.length - 1)) * 96 + 2;
      var y = 25 - ((v - min) / span) * 20 - 2;
      return x.toFixed(1) + ',' + y.toFixed(1);
    });
    var last = series[series.length - 1];
    var col = color || (last >= series[0] ? '#eb5757' : '#27ae60'); // A股红涨绿跌
    var lastPt = pts[pts.length - 1].split(',');
    return '<svg viewBox="0 0 100 28" preserveAspectRatio="none" style="width:100%;height:28px;display:block">' +
      '<polyline points="' + pts.join(' ') + '" fill="none" stroke="' + col + '" stroke-width="1.5" opacity=".85"/>' +
      '<circle cx="' + lastPt[0] + '" cy="' + lastPt[1] + '" r="2" fill="' + col + '"/></svg>';
  }

  // ★#311 机会类型颜色类（每类不同色，全站统一——质量折价绿/价值重估红/低估值蓝等）
  var OTYPE_CLS = {
    reversal: 'pv-ot-reversal', value: 'pv-ot-value', breakout: 'pv-ot-breakout',
    revalue: 'pv-ot-revalue', event: 'pv-ot-event', quality_gap: 'pv-ot-quality_gap',
    pv_consensus: 'pv-ot-pv_consensus', tech_sentiment: 'pv-ot-tech_sentiment'
  };
  function otypeClass(otype) { return OTYPE_CLS[otype] || 'pv-ot-reversal'; }

  // ★#348 数据流通：otype/因子族/分类中文名从 /api/live/enums 动态下发（不写死）
  //   后端 registry + signal_family 动态构建，新类型/新因子自动出现，前端零改动
  var _enums = null;
  function loadEnums() {
    if (_enums) return Promise.resolve(_enums);
    return fetch('/api/live/enums').then(function (r) { return r.json(); }).then(function (d) {
      _enums = d || {};
      return _enums;
    }).catch(function () { _enums = {}; return _enums; });
  }
  function _stripEmoji(s) {
    return String(s == null ? '' : s)
      .replace(/[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}\u{FE00}-\u{FE0F}]/gu, '')
      .replace(/\s+/g, ' ').trim();
  }
  function otypeCN(otype) {
    if (!otype) return '—';
    var m = (_enums && _enums.otypes) || {};
    return m[otype] || otype;
  }
  function familyCN(family) {
    if (!family) return '—';
    var m = (_enums && _enums.families) || {};
    return _stripEmoji(m[family]) || family;
  }
  function categoryCN(cat) {
    if (!cat) return '—';
    var m = (_enums && _enums.categories) || {};
    return _stripEmoji(m[cat]) || cat;
  }
  // ★#427 FRC 排雷红旗中文名（r1_cfo_np_low → 现金流/净利<0.5 等，从 enums.risk_flags 动态下发）
  function riskFlagCN(flag) {
    if (!flag) return '—';
    var m = (_enums && _enums.risk_flags) || {};
    return m[flag] || flag;
  }
  // ★#348 择时四维颜色/权重 + 因子风格颜色（从 enums 动态下发，不写死 DIM_COLORS/STYLE_COLORS）
  function dimColor(key) {
    var m = (_enums && _enums.timing_dims) || {};
    var d = m[key];
    return (d && d.color) || '#5e6ad2';
  }
  function dimWeight(key) {
    var m = (_enums && _enums.timing_dims) || {};
    var d = m[key];
    return (d && d.weight) != null ? d.weight : null;
  }
  function styleColor(style) {
    var m = (_enums && _enums.style_colors) || {};
    return m[style] || '#a1a1aa';
  }
  // ★#351 短线四维打分颜色/权重（tech_pitch_v3 score_breakdown——前端不写死 sbDims）
  function techDimColor(key) {
    var m = (_enums && _enums.tech_dims) || {};
    var d = m[key];
    return (d && d.color) || '#5e6ad2';
  }
  function techDimWeight(key) {
    var m = (_enums && _enums.tech_dims) || {};
    var d = m[key];
    return (d && d.weight) != null ? d.weight : null;
  }
  function stripEmoji(s) { return _stripEmoji(s); }   // ★#351 全站统一 emoji 清洗（暴露给页面）

  // ★#401 KPI 数字 count-up 滚动（expo-out 缓动，2s；prefers-reduced-motion 降级直显）
  // ★#405 自动前缀/后缀提取：支持 "+5.67%"、"123.45 万"、"1,234 只" 等带符号/单位格式
  function countUp(el, val, opts) {
    if (!el) return;
    opts = opts || {};
    var raw = val == null ? '' : String(val);
    if (raw === '' || raw === '—' || raw === '-' || raw === 'N/A') { el.textContent = raw; return; }
    var num = parseFloat(raw.replace(/[^\d.\-]/g, ''));
    if (isNaN(num)) { el.textContent = raw; return; }
    var decimals = opts.decimals;
    if (decimals == null) {
      var m = raw.match(/\.(\d+)/);
      decimals = m ? m[1].length : 0;
    }
    // 自动提取符号前缀（+/-）与数字后的单位后缀（%、万、只、亿 等）
    var sign = (raw.charAt(0) === '+' || raw.charAt(0) === '-') ? raw.charAt(0) : '';
    var sfx = raw.replace(/^[+-]/, '').replace(/[\d,.]/g, '').replace(/\s+/g, '');
    var prefix = opts.prefix != null ? opts.prefix : sign;
    var suffix = opts.suffix != null ? opts.suffix : (sfx ? ' ' + sfx : '');
    if (typeof window !== 'undefined' && window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      el.textContent = prefix + num.toFixed(decimals) + suffix; return;
    }
    var dur = opts.duration || 1200, start = null;
    function step(ts) {
      if (start == null) start = ts;
      var t = Math.min(1, (ts - start) / dur);
      var eased = t === 1 ? 1 : 1 - Math.pow(2, -10 * t);   // expo-out
      el.textContent = prefix + (num * eased).toFixed(decimals) + suffix;
      if (t < 1) window.requestAnimationFrame(step);
    }
    window.requestAnimationFrame(step);
  }

  win.LW = win.LW || {};
  win.LW.render = { _f: _f, pct: pct, color: color, mount: mount, table: table, spark: spark,
    otypeClass: otypeClass, loadEnums: loadEnums, otypeCN: otypeCN, familyCN: familyCN, categoryCN: categoryCN,
    riskFlagCN: riskFlagCN,
    dimColor: dimColor, dimWeight: dimWeight, styleColor: styleColor, techDimColor: techDimColor,
    techDimWeight: techDimWeight, stripEmoji: stripEmoji, countUp: countUp };
})(window);
