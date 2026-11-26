/* 2026-08-13 #290 审批操作组件（v2 从"展示版"变"可用版"——buy/drop/undo）
   复用旧 pitch.html approve 成熟逻辑：POST /api/decide + 环境不适合二次确认 +
   类型降权提示 + toast 反馈。事件委托（#186 教训：不用内联 onclick 引号地狱）。
   用法：LW.decide.approve(code, action, name)  Promise；行内按钮 data-act="buy|drop|undo"。 */
(function () {
  'use strict';
  window.LW = window.LW || {};

  var TM = null;       // 择时环境（懒加载）
  var DOWN_LABELS = {}; // 类型降权提示
  var CODE_OTYPE = {};  // code  otype（降权确认用）
  var _doneLoaded = false;  // ★2026-08-14 审批状态预填只做一次

  function toast(msg) {
    var t = document.getElementById('lw-toast');
    if (!t) {
      t = document.createElement('div');
      t.id = 'lw-toast';
      t.style.cssText = 'position:fixed;top:20px;left:50%;transform:translateX(-50%);background:#111114;color:#ffffff;padding:10px 22px;border-radius:8px;font-size:13px;z-index:999;box-shadow:0 6px 20px rgba(0,0,0,.35);font-family:inherit';
      document.body.appendChild(t);
    }
    t.textContent = msg;
    t.style.display = 'block';
    clearTimeout(t._tm);
    t._tm = setTimeout(function () { t.style.display = 'none'; }, 2500);
  }

  /* 初始化环境数据（择时 + 类型降权——审批确认用） */
  function init() {
    if (TM) return;
    LW.api.get('/api/live/timing_dash').then(function (d) {
      TM = (d && d.timing) || {};
    }).catch(function () {});
    // 降权标签：读 /api/live/validation（#225 类型降权）
    // ★#374 字段名/结构修复：降权数据在 diagnosis.by_type（list），非 batch_winrates/down_labels（dict）
    LW.api.get('/api/live/validation').then(function (d) {
      var bt = (d && d.diagnosis && d.diagnosis.by_type) || [];
      var map = {};
      bt.forEach(function (x) {
        if (x && x.otype) map[x.otype] = { n: x.n, avg: x.avg, win: x.win };
      });
      DOWN_LABELS = map;
    }).catch(function () {});
    // ★2026-08-14 审计修复：审批状态持久化——页面加载时从 /api/decisions 预填 _done
    //   （原 _done 恒空：刷新后已审批卡片重新显示"买入/放弃"，且防重失效可重复审批）
    //   ★实测 /api/decisions 直接返回数组（非 {decisions:[...]}），两形态都兼容
    if (!_doneLoaded) {
      _doneLoaded = true;
      LW.api.get('/api/decisions').then(function (d) {
        var list = Array.isArray(d) ? d : ((d && d.decisions) || []);
        list.forEach(function (r) {
          if (r && r.code) {
            var a = (r.action || '').toLowerCase();
            if (a === 'buy' || a === 'drop') LW.decide._done[r.code] = a;
          }
        });
        LW.decide._emit('__loaded');
      }).catch(function () {});
    }
  }

  function confirmIf(msg) {
    return window.confirm(msg);
  }

  function approve(code, action, name, otype) {
    init();
    var d = LW.decide._done[code];
    if (action !== 'undo' && d) return Promise.resolve({ skipped: true });

    // 环境不适合买入  二次确认（择时结论传导到审批）
    if (action === 'buy' && TM && TM.level && String(TM.level).indexOf('不适合') >= 0) {
      if (!confirmIf('当前市场择时判定【' + TM.level + '】（' + (TM.score != null ? TM.score : '') + ' 分）——不适合买入。仍要记录这笔买入吗？（决策会保留环境标记，供复盘）')) {
        return Promise.resolve({ cancelled: true });
      }
    }
    // ★P2 L0 门控（2026-08-15 实盘归因：revalue/tech_sentiment 在下跌日放大亏损）：
    //   防御期（择时评分 <40）买入 revalue/tech_sentiment → 从严二次确认
    if (action === 'buy' && otype && TM && TM.score != null && +TM.score < 40 &&
        (String(otype).indexOf('revalue') >= 0 || String(otype).indexOf('tech_sentiment') >= 0 || String(otype).indexOf('价值重估') >= 0 || String(otype).indexOf('短线情绪') >= 0)) {
      if (!confirmIf('当前择时 ' + TM.score + ' 分（防御期）——【' + otype + '】实盘 T+1 偏弱且下跌日放大亏损（批次归因 -1.65%~-2.18%）。仍要买入吗？（审批从严）')) {
        return Promise.resolve({ cancelled: true });
      }
    }
    // 类型降权买入  二次确认（实盘 T+1 偏弱，审批从严）
    if (action === 'buy' && otype) {
      var dw = DOWN_LABELS[otype];
      if (dw && dw.n && dw.n >= 3) {
        var avg = (dw.avg != null ? dw.avg * 100 : null);
        if (!confirmIf('该股属【' + otype + '】类型——当前实盘 T+1 ' +
          (avg != null ? (avg >= 0 ? '+' : '') + avg.toFixed(2) + '%' : '偏弱') + '（' + dw.n + ' 样本），审批从严。仍要买入吗？')) {
          return Promise.resolve({ cancelled: true });
        }
      }
    }
    var note = action === 'buy' ? 'Pitch 审批买入' : (action === 'drop' ? 'Pitch 审批放弃' : 'Pitch 审批撤回');
    return fetch('/api/decide', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: code, action: action, note: note })
    }).then(function (r) { return r.json(); }).then(function (res) {
      if (res.ok) {
        if (action === 'undo') {
          delete LW.decide._done[code];
          toast(code + ' 审批已撤回');
        } else {
          LW.decide._done[code] = action;
          // ★2026-08-15 周一 UX：持仓满 5/5 时 buy 会进超限（被拒）而非入持仓——
          //   toast 明示，避免用户误以为已买到（决策页持仓条同步刷新显示名额/超限）
          toast(code + (action === 'buy'
            ? ' 买入已记录（已入远期池）· 持仓满 5 只将进入超限待处理，需先卖出腾名额'
            : ' 已放弃'));
        }
        if (LW.api && LW.api.clear) LW.api.clear();   // 清缓存，reload 时强制拉最新
        LW.decide._emit(code);
        return res;
      }
      toast((res.error || '保存失败'));
      return res;
    }).catch(function (e) {
      toast('服务器未连接：' + (e && e.message));
      return { error: String(e) };
    });
  }

  /* 审批后局部刷新（行内状态 + 监听器广播） */
  var _listeners = {};
  LW.decide = {
    _done: {},
    _listeners: _listeners,
    init: init,
    approve: approve,
    toast: toast,
    on: function (fn) { var k = Date.now() + '' + Math.random(); _listeners[k] = fn; return k; },
    _emit: function (code) {
      Object.keys(_listeners).forEach(function (k) {
        try { _listeners[k](code); } catch (e) {}
      });
    },
    /* 事件委托：容器内 .v2-btn-buy/.v2-btn-drop/.v2-btn-undo 点击  审批
       btn 需带 data-code / data-name / data-otype 属性 */
    /* 卖出（V7c：POST /api/portfolio/sell → 状态机 exit + 留痕；price=None 用最新收盘价） */
    sell: function (code, name) {
      if (!confirmIf('确认卖出 ' + code + (name ? ' ' + name : '') + '？\n（按最新收盘价成交，状态机 exit + 操作留痕）')) {
        return Promise.resolve({ cancelled: true });
      }
      return fetch('/api/portfolio/sell', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: code, price: null, reason: 'manual' })
      }).then(function (r) { return r.json(); }).then(function (res) {
        if (res.ok) {
          toast(code + ' 已卖出');
          if (LW.api && LW.api.clear) LW.api.clear();   // 清缓存，持仓页 reload 拉最新
          LW.decide._emit(code);
        } else {
          toast('卖出失败：' + (res.error || JSON.stringify(res).slice(0, 80)));
        }
        return res;
      }).catch(function (e) {
        toast('服务器未连接：' + (e && e.message));
        return { error: String(e) };
      });
    },
    bind: function (root) {
      root.addEventListener('click', function (ev) {
        var b = ev.target.closest('.v2-btn-buy, .v2-btn-drop, .v2-btn-undo, .v2-btn-sell');
        if (!b) return;
        ev.preventDefault();
        if (b.classList.contains('v2-btn-sell')) {
          LW.decide.sell(b.getAttribute('data-code'), b.getAttribute('data-name'));
          return;
        }
        var act = b.classList.contains('v2-btn-buy') ? 'buy' : (b.classList.contains('v2-btn-drop') ? 'drop' : 'undo');
        approve(b.getAttribute('data-code'), act, b.getAttribute('data-name'), b.getAttribute('data-otype'));
      });
    }
  };

  // ★2026-11-10 修复：页面加载即预填审批状态（原 init 只在 approve 内调用 →
  //   刷新后已买入/已放弃卡片仍显示按钮，且防重失效可重复提交）
  if (window.LW && window.LW.api) init();
})();
