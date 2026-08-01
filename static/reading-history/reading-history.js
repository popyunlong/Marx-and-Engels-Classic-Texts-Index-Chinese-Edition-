/* =============================================================================
 * 阅读历史 · 继续阅读（marx-reading-history）
 * -----------------------------------------------------------------------------
 * 纯前端、零后端：把「读到哪本书的哪一页」记在本设备 localStorage，按账号隔离，
 * 首页/文库等入口渲染成「继续阅读」标签，点一下即回到上次阅读的确切位置。
 * 与既有「历史检索记录」同一套设计范式（本地存储、按 uid 隔离、TTL + 配额安全丢弃、
 * 空则自动隐藏），不新增数据库、不触碰阅读热路径、可安全上线。
 *
 * 两类调用方：
 *   · 阅读器（viewer / wenku_reader）：MarxReadingHistory.record(entry) 记录当前位置。
 *   · 入口页（index / library / wenku / liushi）：MarxReadingHistory.mount(el) 渲染标签。
 *
 * uid 来源（按优先级）：本脚本标签 data-uid → window.__MARX_UID__ → 'guest'。
 * ========================================================================== */
(function () {
  'use strict';
  if (window.MarxReadingHistory) return;  // 幂等：多次引入只初始化一次

  var VERSION = 'v1';
  var MAX = 20;                              // 最多保留 20 本（去重后每书一条）
  var TTL_MS = 120 * 24 * 60 * 60 * 1000;   // 120 天后自动过期
  var WRITE_THROTTLE_MS = 1200;             // 记录写入节流，翻页/滚动连发只落最后一次

  // ---- uid（决定 localStorage key，实现按账号隔离）-------------------------
  var UID = '';
  try {
    var self = document.currentScript || document.querySelector('script[data-marx-rh]');
    UID = (self && self.getAttribute('data-uid')) || window.__MARX_UID__ || '';
  } catch (e) { UID = ''; }
  UID = String(UID == null ? '' : UID);

  function storageKey() { return 'marx-reading-history-' + VERSION + ':' + (UID || 'guest'); }

  // ---- 存取（全程 try/catch：隐私模式/配额满/被禁用都不抛错）---------------
  function load() {
    var list;
    try {
      var raw = localStorage.getItem(storageKey());
      list = raw ? JSON.parse(raw) : [];
    } catch (e) { list = []; }
    if (!Array.isArray(list)) return [];
    var cutoff = Date.now() - TTL_MS;
    return list.filter(function (it) {
      return it && it.id && it.url && typeof it.url === 'string' &&
        it.url.charAt(0) === '/' &&               // 仅允许站内根相对 URL（防注入外链）
        Number(it.ts || 0) >= cutoff;
    });
  }

  // 配额安全：QuotaExceededError 时逐条丢最旧后重试，绝不因存储失败拖垮页面。
  function persist(list) {
    var attempt = list.slice(0, MAX);
    while (attempt.length) {
      try { localStorage.setItem(storageKey(), JSON.stringify(attempt)); return attempt; }
      catch (e) { attempt = attempt.slice(0, attempt.length - 1); }
    }
    try { localStorage.removeItem(storageKey()); } catch (e) {}
    return [];
  }

  // ---- 记录当前阅读位置 ----------------------------------------------------
  // entry: {book, kind, title, subtitle, url}
  //   book     去重键（同一本书/卷只保留最新位置），如 source_file 或 book_key|vol
  //   kind     'viewer' | 'wenku' | 'liushi'（决定标签底色与文案）
  //   title    书名（+卷次），展示主文案
  //   subtitle 位置描述，如「第 123 页」「篇名」
  //   url      站内根相对续读 URL（必须以 / 开头）
  var _pending = null, _timer = null;
  function record(entry) {
    if (!entry || !entry.book || !entry.title) return;
    var url = String(entry.url || '');
    if (url.charAt(0) !== '/') return;  // 安全闸：只接受站内根相对 URL
    _pending = {
      book: String(entry.book),
      kind: entry.kind === 'wenku' || entry.kind === 'liushi' ? entry.kind : 'viewer',
      title: String(entry.title).slice(0, 120),
      subtitle: String(entry.subtitle || '').slice(0, 120),
      url: url
    };
    if (_timer) return;                 // 节流窗口内只保留最后一次
    _timer = setTimeout(flush, WRITE_THROTTLE_MS);
  }

  function flush() {
    _timer = null;
    var e = _pending; _pending = null;
    if (!e) return;
    var list = load().filter(function (it) { return it.book !== e.book; });  // 同书去重
    e.id = 'r' + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
    e.ts = Date.now();
    list.unshift(e);
    persist(list);
    renderMounted();
  }

  // 立即落盘（离开页面前调用，保证最后位置不丢）。
  function flushNow() {
    if (_timer) { clearTimeout(_timer); _timer = null; }
    flush();
  }
  // 页面隐藏/卸载时兜底落盘（快速翻几页就离开也能记住终点）。
  window.addEventListener('pagehide', flushNow);
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'hidden') flushNow();
  });

  function remove(id) { persist(load().filter(function (it) { return it.id !== id; })); renderMounted(); }
  function clear() { try { localStorage.removeItem(storageKey()); } catch (e) {} renderMounted(); }

  // ---- 渲染 ----------------------------------------------------------------
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function kindLabel(k) { return k === 'wenku' ? '文库' : k === 'liushi' ? '流式' : '阅读'; }
  function relTime(ts) {
    var t = Number(ts || 0); if (!t) return '';
    var d = Date.now() - t;
    if (d < 60000) return '刚刚';
    if (d < 3600000) return Math.floor(d / 60000) + ' 分钟前';
    if (d < 86400000) return Math.floor(d / 3600000) + ' 小时前';
    if (d < 7 * 86400000) return Math.floor(d / 86400000) + ' 天前';
    var x = new Date(t), p = function (n) { return n < 10 ? '0' + n : '' + n; };
    return x.getFullYear() + '-' + p(x.getMonth() + 1) + '-' + p(x.getDate());
  }

  var _styleInjected = false;
  function injectStyle() {
    if (_styleInjected) return; _styleInjected = true;
    var css =
      '.mrh-wrap{--mrh-accent:var(--accent,#8f1d1d);--mrh-panel:var(--panel,#fffdf8);' +
      '--mrh-line:var(--line,#e3d8c8);--mrh-text:var(--text,#241d17);--mrh-muted:var(--muted,#766a5d);}' +
      '.mrh-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:0 0 10px;}' +
      '.mrh-title{font-size:.98rem;font-weight:600;color:var(--mrh-accent);margin:0;}' +
      '.mrh-hint{font-size:.76rem;color:var(--mrh-muted);}' +
      '.mrh-clear{margin-left:auto;border:1px solid var(--mrh-line);background:transparent;color:var(--mrh-muted);' +
      'border-radius:7px;padding:3px 10px;font-size:.78rem;cursor:pointer;}' +
      '.mrh-clear:hover{border-color:var(--mrh-accent);color:var(--mrh-accent);}' +
      '.mrh-list{display:flex;flex-wrap:wrap;gap:9px;}' +
      '.mrh-item{position:relative;display:flex;align-items:center;gap:9px;max-width:340px;' +
      'border:1px solid var(--mrh-line);background:var(--mrh-panel);border-radius:11px;' +
      'padding:8px 30px 8px 11px;text-decoration:none;color:var(--mrh-text);' +
      'transition:border-color .15s,box-shadow .15s,transform .15s;}' +
      '.mrh-item:hover{border-color:var(--mrh-accent);box-shadow:0 3px 12px rgba(143,29,29,.10);transform:translateY(-1px);}' +
      '.mrh-badge{flex:0 0 auto;font-size:.68rem;font-weight:700;color:#fff;background:var(--mrh-accent);' +
      'border-radius:6px;padding:2px 7px;letter-spacing:.04em;}' +
      '.mrh-badge.liushi{background:#264653;}.mrh-badge.wenku{background:#6b4f2a;}' +
      '.mrh-main{min-width:0;display:flex;flex-direction:column;gap:2px;}' +
      '.mrh-name{font-size:.9rem;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}' +
      '.mrh-meta{font-size:.75rem;color:var(--mrh-muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}' +
      '.mrh-del{position:absolute;top:50%;right:6px;transform:translateY(-50%);border:0;background:transparent;' +
      'color:var(--mrh-muted);font-size:1.05rem;line-height:1;cursor:pointer;padding:2px 5px;border-radius:6px;}' +
      '.mrh-del:hover{color:var(--mrh-accent);background:rgba(143,29,29,.08);}' +
      '.mrh-empty{font-size:.82rem;color:var(--mrh-muted);}';
    try {
      var st = document.createElement('style');
      st.setAttribute('data-mrh-style', '');
      st.textContent = css;
      (document.head || document.documentElement).appendChild(st);
    } catch (e) {}
  }

  var _mounts = [];  // {el, opts}
  function mount(target, opts) {
    var el = typeof target === 'string' ? document.querySelector(target) : target;
    if (!el) return null;
    injectStyle();
    var m = { el: el, opts: opts || {} };
    _mounts.push(m);
    // 事件委托：删除单条（阻止冒泡以免触发续读跳转），清空整组。
    el.addEventListener('click', function (ev) {
      var del = ev.target.closest && ev.target.closest('[data-mrh-del]');
      if (del) { ev.preventDefault(); ev.stopPropagation(); remove(del.getAttribute('data-mrh-del')); return; }
      var clr = ev.target.closest && ev.target.closest('[data-mrh-clear]');
      if (clr) { ev.preventDefault(); clear(); }
    });
    renderOne(m);
    return m;
  }

  function renderOne(m) {
    var el = m.el, opts = m.opts;
    var list = load();
    if (!list.length) {
      el.innerHTML = opts.hideWhenEmpty === false ? '<div class="mrh-empty">暂无阅读记录，翻开任意一卷即可自动记录。</div>' : '';
      if (opts.hideWhenEmpty !== false) el.setAttribute('hidden', '');
      return;
    }
    el.removeAttribute('hidden');
    var titleText = opts.title || '继续阅读';
    var html = '<div class="mrh-wrap">';
    html += '<div class="mrh-head"><span class="mrh-title">' + esc(titleText) + '</span>' +
      '<span class="mrh-hint">仅存于本设备浏览器 · 点标签即回到上次阅读处</span>' +
      '<button type="button" class="mrh-clear" data-mrh-clear>清空</button></div>';
    html += '<div class="mrh-list">';
    html += list.map(function (it) {
      return '<a class="mrh-item" href="' + esc(it.url) + '" title="回到：' + esc(it.title) +
        (it.subtitle ? ' · ' + esc(it.subtitle) : '') + '">' +
        '<span class="mrh-badge ' + esc(it.kind) + '">' + esc(kindLabel(it.kind)) + '</span>' +
        '<span class="mrh-main"><span class="mrh-name">' + esc(it.title) + '</span>' +
        '<span class="mrh-meta">' + esc(it.subtitle || '') + (it.subtitle ? ' · ' : '') + esc(relTime(it.ts)) + '</span></span>' +
        '<button type="button" class="mrh-del" data-mrh-del="' + esc(it.id) + '" aria-label="删除这条阅读记录" title="删除">×</button>' +
        '</a>';
    }).join('');
    html += '</div></div>';
    el.innerHTML = html;
  }

  function renderMounted() { _mounts.forEach(renderOne); }

  window.MarxReadingHistory = {
    record: record,
    mount: mount,
    render: renderMounted,
    remove: remove,
    clear: clear,
    load: load
  };
})();
