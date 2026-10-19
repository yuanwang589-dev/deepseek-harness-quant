/* ui_v2/core/api.js — ★统一 API 客户端（UI v2 标准 #18 冗余+数据顺畅）
   功能：AbortController 真超时 + 失败重试 1 次 + 同页缓存（同 URL 只拉一次）+ 60s 轮询（活跃页）
   全局命名空间 LW.api，供所有页面复用——冗余容错（不白屏）的第一道防线。
   用法：
     LW.api.get('/api/live/factors').then(function(d){...})   // 自动缓存+重试
     LW.api.poll('/api/live/factors', 60, renderFn)           // 60s 轮询

   ★2026-10-15 P2 修复：原实现只有 fetch，无超时——后端卡住时 fetch 永远 pending，
   Promise 悬挂、轮询 tick 堆积、页面永远转圈。现用 AbortController 真中断：
   - 默认 8s；已知慢端点显式放宽（endpoints 30s / realtime 20s / wufu 20s / timing_dash 20s）
   - 超时中断后按原逻辑走"重试 1 次 → 缓存降级"链路
   - post() 同样带超时（默认 30s，写操作后端可能触发重算）
*/
(function (win) {
  'use strict';
  var cache = {};        // url -> {ts, data, status:'ok'|'err'}
  var inflight = {};     // url -> Promise（防并发重复请求）

  // ★已知慢端点显式超时（ms）：后端要做实时计算/扫全市场，8s 默认值不够
  var SLOW_TIMEOUTS = [
    ['/api/live/endpoints', 30000],
    ['/api/live/realtime', 20000],
    ['/api/live/wufu_rotation', 20000],
    ['/api/live/timing_dash', 20000]
  ];
  function timeoutFor(url) {
    for (var i = 0; i < SLOW_TIMEOUTS.length; i++) {
      if (url.indexOf(SLOW_TIMEOUTS[i][0]) === 0) return SLOW_TIMEOUTS[i][1];
    }
    return 8000;
  }

  // ★带 AbortController 的 fetch：到时真中断（原裸 fetch 永远 pending）
  function fetchWithTimeout(url, timeoutMs, opts) {
    var ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
    var timer = ctrl ? setTimeout(function () { ctrl.abort(); }, timeoutMs) : null;
    var o = Object.assign({ cache: 'no-store' }, opts || {});
    if (ctrl) o.signal = ctrl.signal;
    return fetch(url, o).then(function (r) {
      clearTimeout(timer);
      return r;
    }, function (err) {
      clearTimeout(timer);
      // 中断 → 转成明确的超时错误（便于日志区分网络错/超时）
      if (ctrl && ctrl.signal && ctrl.signal.aborted) {
        throw new Error('TIMEOUT ' + timeoutMs + 'ms ' + url);
      }
      throw err;
    });
  }

  function parseJson(r) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }

  function get(url, opts) {
    opts = opts || {};
    var timeout = opts.timeout || timeoutFor(url);
    var retry = opts.retry !== undefined ? opts.retry : 1;   // 默认重试 1 次
    var bypass = opts.bypass === true;   // 强制刷新（审批/卖出后即时更新用）
    var cached = cache[url];
    var now = Date.now();
    // 同页缓存：30s 内直接复用（页面内多个组件共用一个 API）
    if (!bypass && cached && now - cached.ts < 30000 && cached.status === 'ok') {
      return Promise.resolve(cached.data);
    }
    if (inflight[url]) { return inflight[url]; }

    var p = fetchWithTimeout(url, timeout)
      .then(parseJson)
      .then(function (data) {
        cache[url] = { ts: Date.now(), data: data, status: 'ok' };
        return data;
      })
      .catch(function (err) {
        // 重试一次（网络抖动/瞬时超时）
        if (retry > 0) {
          retry--;
          return fetchWithTimeout(url, timeout).then(parseJson).then(function (data) {
            cache[url] = { ts: Date.now(), data: data, status: 'ok' };
            return data;
          });
        }
        // 重试仍失败：有缓存则降级返回缓存（非白屏），无缓存则抛错
        if (cached) { cache[url].status = 'err'; return cached.data; }
        throw err;
      })
      .then(function (data) { delete inflight[url]; return data; })
      .catch(function (err) { delete inflight[url]; throw err; });
    inflight[url] = p;
    return p;
  }

  // 轮询：interval 秒拉一次（后台标签页自动降频 5min——省资源）
  function poll(url, intervalSec, renderFn) {
    var tick = function () {
      var hidden = document.hidden;
      var wait = hidden ? 300000 : (intervalSec * 1000);
      get(url).then(renderFn).catch(function () { /* 降级由 get 处理 */ });
      setTimeout(tick, wait);
    };
    tick();
  }

  // 清空缓存（审批/卖出后数据已变，下次 get 强制重拉最新）
  function clear() {
    cache = {};
    inflight = {};
  }

  // POST JSON（审批/卖出/手动更新等写操作——不带缓存、不重试；30s 超时兜底）
  function post(url, body, opts) {
    opts = opts || {};
    return fetchWithTimeout(url, opts.timeout || 30000, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    }).then(parseJson);
  }

  win.LW = win.LW || {};
  win.LW.api = { get: get, poll: poll, clear: clear, post: post };
})(window);
