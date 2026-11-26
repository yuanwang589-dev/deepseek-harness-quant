/* ui_v2/core/api.js — ★统一 API 客户端（UI v2 标准 #18 冗余+数据顺畅）
   功能：8s 超时 + 失败重试 1 次 + 同页缓存（同 URL 只拉一次）+ 60s 轮询（活跃页）
   全局命名空间 LW.api，供所有页面复用——冗余容错（不白屏）的第一道防线。
   用法：
     LW.api.get('/api/live/factors').then(function(d){...})   // 自动缓存+重试
     LW.api.poll('/api/live/factors', 60, renderFn)           // 60s 轮询
*/
(function (win) {
  'use strict';
  var cache = {};        // url -> {ts, data, status:'ok'|'err'}
  var inflight = {};     // url -> Promise（防并发重复请求）

  // 带超时的 fetch（★2026-11-10 修复：opts.timeout 此前只赋值、从未生效）
  function fetchT(url, opts, timeout) {
    if (!timeout) return fetch(url, opts);
    if (typeof AbortController === 'undefined') return fetch(url, opts);
    var ctrl = new AbortController();
    var tm = setTimeout(function () { ctrl.abort(); }, timeout);
    var o = Object.assign({}, opts, { signal: ctrl.signal });
    // ★2026-11-20 AbortError 转中文：原样透出 "signal is aborted without reason"，用户无法判断
    return fetch(url, o).finally(function () { clearTimeout(tm); }).catch(function (err) {
      if (err && err.name === 'AbortError') throw new Error('请求超时：' + url + '（>' + timeout + 'ms）');
      throw err;
    });
  }

  function get(url, opts) {
    opts = opts || {};
    var timeout = opts.timeout || 8000;
    var retry = opts.retry !== undefined ? opts.retry : 1;   // 默认重试 1 次
    var bypass = opts.bypass === true;   // 强制刷新（审批/卖出后即时更新用）
    var cached = cache[url];
    var now = Date.now();
    // 同页缓存：30s 内直接复用（页面内多个组件共用一个 API）
    if (!bypass && cached && now - cached.ts < 30000 && cached.status === 'ok') {
      return Promise.resolve(cached.data);
    }
    if (inflight[url]) { return inflight[url]; }

    var p = fetchT(url, { cache: 'no-store' }, timeout)
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (data) {
        cache[url] = { ts: Date.now(), data: data, status: 'ok' };
        return data;
      })
      .catch(function (err) {
        // 重试一次（网络抖动）
        if (retry > 0) {
          retry--;
          return fetchT(url, { cache: 'no-store' }, timeout).then(function (r) {
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.json();
          }).then(function (data) {
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

  // POST JSON（审批/卖出/手动更新等写操作——不带缓存、不重试）
  function post(url, body, opts) {
    opts = opts || {};
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
      cache: 'no-store'
    }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    });
  }

  win.LW = win.LW || {};
  win.LW.api = { get: get, poll: poll, clear: clear, post: post };
})(window);
