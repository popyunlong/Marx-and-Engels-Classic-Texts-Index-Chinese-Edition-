/* 「精选到书/卷」多选控件（首页引文/联想/研究、AI 随心问抽屉、两阅读器共用）。
 *
 * 数据 tree = [{ id, label, books:[{ key, label, volumes:[卷号...] }] }]（单卷本 volumes 为空）。
 * 选择态 sel = { 书库键: "all" | [卷号...] }；导出 token：整套→"book:<键>"，单卷→"vol:<键>:<卷号>"。
 * 后端 _resolve_search_scope / _standard_search_scope 直接吃这个 token 列表。
 *
 * 用法：const ctl = BookScope.mount(container, tree, { onChange, dark, persist, storageKey, initialTokens });
 *   默认「不跨访问记忆」：每次挂载都从空开始（= 全部著作）；仅当传 persist:true 时才读写 localStorage。
 *   ctl.getTokens() -> ["book:文集","vol:全集:5", ...]（无选择时为 []）
 *   ctl.count()     -> 已选著作数
 *   ctl.clear()     -> 清空选择
 */
(function (global) {
  'use strict';

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function loadSel(storageKey) {
    try {
      var raw = JSON.parse(localStorage.getItem(storageKey) || 'null');
      if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
        var out = {};
        Object.keys(raw).forEach(function (k) {
          var v = raw[k];
          if (v === 'all') out[k] = 'all';
          else if (Array.isArray(v)) {
            var nums = v.map(Number).filter(function (n) { return Number.isFinite(n); });
            if (nums.length) out[k] = nums.slice().sort(function (a, b) { return a - b; });
          }
        });
        return out;
      }
    } catch (_) {}
    return {};
  }

  function mount(container, tree, opts) {
    opts = opts || {};
    tree = Array.isArray(tree) ? tree : [];
    var storageKey = opts.storageKey || 'marx-book-scope-v1';
    var onChange = typeof opts.onChange === 'function' ? opts.onChange : function () {};
    // 默认不跨访问记忆：每次挂载从空开始（= 全部著作）；仅显式 persist:true 才读写 localStorage。
    var persistOn = opts.persist === true;
    // 书库键 → { label, volumes } 索引；顺带剔除 localStorage 里已不在库的键。
    var bookIndex = {};
    tree.forEach(function (g) { (g.books || []).forEach(function (b) { bookIndex[b.key] = b; }); });

    function fromTokens(tokens) {
      var out = {};
      (Array.isArray(tokens) ? tokens : []).forEach(function (token) {
        token = String(token || '');
        if (token.indexOf('book:') === 0) {
          var wholeKey = token.slice(5);
          if (bookIndex[wholeKey]) out[wholeKey] = 'all';
          return;
        }
        if (token.indexOf('vol:') !== 0) return;
        var value = token.slice(4);
        var split = value.lastIndexOf(':');
        if (split <= 0) return;
        var key = value.slice(0, split), volume = Number(value.slice(split + 1));
        var allowed = bookIndex[key] && Array.isArray(bookIndex[key].volumes)
          ? bookIndex[key].volumes.map(Number) : [];
        if (!Number.isFinite(volume) || allowed.indexOf(volume) < 0 || out[key] === 'all') return;
        var selected = Array.isArray(out[key]) ? out[key] : [];
        if (selected.indexOf(volume) < 0) selected.push(volume);
        selected.sort(function (a, b) { return a - b; });
        out[key] = selected.length === allowed.length ? 'all' : selected;
      });
      return out;
    }

    var hasInitial = Array.isArray(opts.initialTokens);
    var sel = hasInitial ? fromTokens(opts.initialTokens) : (persistOn ? loadSel(storageKey) : {});
    Object.keys(sel).forEach(function (k) { if (!bookIndex[k]) delete sel[k]; });

    var root = document.createElement('div');
    root.className = 'bscope';
    if (opts.dark) root.setAttribute('data-dark', '1');
    root.innerHTML =
      '<button type="button" class="bscope-trigger" aria-expanded="false">' +
        '<span class="bscope-trigger-label">指定著作</span>' +
        '<span class="bscope-count" hidden></span>' +
        '<span class="bscope-caret" aria-hidden="true">▾</span>' +
      '</button>' +
      '<div class="bscope-panel" hidden role="group" aria-label="精选到书或卷">' +
        '<div class="bscope-panel-head">' +
          '<span class="bscope-panel-hint">勾选一本或多本；多卷著作可展开选到某几卷。选了即只在所选范围内检索。</span>' +
          '<button type="button" class="bscope-clear">清除</button>' +
        '</div>' +
        '<div class="bscope-groups"></div>' +
      '</div>';
    container.appendChild(root);

    var trigger = root.querySelector('.bscope-trigger');
    var panel = root.querySelector('.bscope-panel');
    var countEl = root.querySelector('.bscope-count');
    var groupsEl = root.querySelector('.bscope-groups');
    var clearBtn = root.querySelector('.bscope-clear');
    var labelEl = root.querySelector('.bscope-trigger-label');

    function buildBookRow(b) {
      var row = document.createElement('div');
      row.className = 'bscope-book';
      row.dataset.key = b.key;
      var multi = Array.isArray(b.volumes) && b.volumes.length > 1;
      var html =
        '<label class="bscope-book-main">' +
          '<input type="checkbox" class="bscope-book-cb">' +
          '<span class="bscope-book-label">' + esc(b.label) + '</span>' +
        '</label>';
      if (multi) {
        html += '<button type="button" class="bscope-vol-toggle" aria-expanded="false">' +
          '选卷 <span class="bscope-vol-caret" aria-hidden="true">▸</span></button>';
      }
      row.innerHTML = html;
      if (multi) {
        var vwrap = document.createElement('div');
        vwrap.className = 'bscope-vols';
        vwrap.hidden = true;
        var vhtml = '<label class="bscope-vol bscope-vol-all"><input type="checkbox" class="bscope-vol-all-cb"><span>全部卷</span></label>';
        b.volumes.forEach(function (n) {
          vhtml += '<label class="bscope-vol"><input type="checkbox" class="bscope-vol-cb" value="' + n + '"><span>第' + n + '卷</span></label>';
        });
        vwrap.innerHTML = vhtml;
        row.appendChild(vwrap);
      }
      return row;
    }

    tree.forEach(function (g) {
      var books = g.books || [];
      if (!books.length) return;
      var gEl = document.createElement('div');
      gEl.className = 'bscope-group';
      gEl.innerHTML = '<div class="bscope-group-label">' + esc(g.label) + '</div>';
      books.forEach(function (b) { gEl.appendChild(buildBookRow(b)); });
      groupsEl.appendChild(gEl);
    });

    function persist() {
      if (!persistOn) return;
      try { localStorage.setItem(storageKey, JSON.stringify(sel)); } catch (_) {}
    }

    function paint() {
      root.querySelectorAll('.bscope-book').forEach(function (row) {
        var key = row.dataset.key;
        var v = sel[key];
        var b = bookIndex[key];
        var all = (b && b.volumes) ? b.volumes.map(Number) : [];
        var partial = Array.isArray(v) && all.length && v.length < all.length;
        var cb = row.querySelector('.bscope-book-cb');
        if (cb) { cb.checked = v !== undefined; cb.indeterminate = !!partial; }
        var allCb = row.querySelector('.bscope-vol-all-cb');
        if (allCb) allCb.checked = (v === 'all');
        row.querySelectorAll('.bscope-vol-cb').forEach(function (vc) {
          var n = Number(vc.value);
          vc.checked = (v === 'all') || (Array.isArray(v) && v.indexOf(n) >= 0);
        });
        row.classList.toggle('bscope-book-on', v !== undefined);
      });
      var c = Object.keys(sel).length;
      if (countEl) { countEl.textContent = String(c); countEl.hidden = c === 0; }
      trigger.classList.toggle('bscope-active', c > 0);
      if (labelEl) labelEl.textContent = c > 0 ? '已指定著作' : '指定著作';
    }

    groupsEl.addEventListener('change', function (e) {
      var t = e.target;
      var row = t.closest && t.closest('.bscope-book');
      if (!row) return;
      var key = row.dataset.key;
      var b = bookIndex[key];
      if (!b) return;
      if (t.classList.contains('bscope-book-cb')) {
        if (t.checked) sel[key] = 'all'; else delete sel[key];
      } else if (t.classList.contains('bscope-vol-all-cb')) {
        if (t.checked) sel[key] = 'all'; else delete sel[key];
      } else if (t.classList.contains('bscope-vol-cb')) {
        var all = (b.volumes || []).map(Number);
        var cur = (sel[key] === 'all') ? all.slice() : (Array.isArray(sel[key]) ? sel[key].slice() : []);
        var n = Number(t.value);
        var i = cur.indexOf(n);
        if (t.checked && i < 0) cur.push(n);
        else if (!t.checked && i >= 0) cur.splice(i, 1);
        cur.sort(function (a, b2) { return a - b2; });
        if (!cur.length) delete sel[key];
        else if (cur.length === all.length) sel[key] = 'all';
        else sel[key] = cur;
      } else return;
      persist(); paint(); onChange();
    });

    groupsEl.addEventListener('click', function (e) {
      var tog = e.target.closest && e.target.closest('.bscope-vol-toggle');
      if (!tog) return;
      var row = tog.closest('.bscope-book');
      var vols = row && row.querySelector('.bscope-vols');
      if (!vols) return;
      var open = vols.hidden;
      vols.hidden = !open;
      tog.setAttribute('aria-expanded', open ? 'true' : 'false');
      var caret = tog.querySelector('.bscope-vol-caret');
      if (caret) caret.textContent = open ? '▾' : '▸';
    });

    // 面板默认自触发器左缘向右展开。触发器若贴着容器右缘（首页「指定著作」就是靠右对齐），
    // 440px 宽的面板会越过视口右侧，把整页撑出横向滚动条、面板落在屏幕外。
    // 展开时按视口宽度把面板横向夹回可见区域（够宽则保持左对齐，不改动原观感）。
    function positionPanel() {
      panel.style.left = '';
      var vw = document.documentElement.clientWidth || window.innerWidth || 0;
      var pw = panel.offsetWidth;
      if (!vw || !pw) return;
      var gap = 8;
      var rootLeft = root.getBoundingClientRect().left;
      var maxLeft = Math.max(gap, vw - gap - pw);
      var want = Math.min(Math.max(rootLeft, gap), maxLeft);
      if (Math.abs(want - rootLeft) >= 1) panel.style.left = (want - rootLeft) + 'px';
    }

    function setOpen(open) {
      panel.hidden = !open;
      trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
      root.classList.toggle('bscope-open', open);
      if (open) positionPanel();
    }
    trigger.addEventListener('click', function () { setOpen(panel.hidden); });
    document.addEventListener('click', function (e) { if (!root.contains(e.target)) setOpen(false); });
    window.addEventListener('resize', function () { if (!panel.hidden) positionPanel(); });
    clearBtn.addEventListener('click', function () { sel = {}; persist(); paint(); onChange(); });

    // 初始：对已选「部分卷」的书展开卷面板，便于用户直接看到已选卷。
    root.querySelectorAll('.bscope-book').forEach(function (row) {
      if (Array.isArray(sel[row.dataset.key])) {
        var vols = row.querySelector('.bscope-vols');
        var tog = row.querySelector('.bscope-vol-toggle');
        if (vols) vols.hidden = false;
        if (tog) {
          tog.setAttribute('aria-expanded', 'true');
          var c = tog.querySelector('.bscope-vol-caret');
          if (c) c.textContent = '▾';
        }
      }
    });
    paint();

    return {
      root: root,
      getTokens: function () {
        var out = [];
        Object.keys(sel).forEach(function (k) {
          var v = sel[k];
          if (v === 'all') out.push('book:' + k);
          else if (Array.isArray(v)) v.forEach(function (n) { out.push('vol:' + k + ':' + n); });
        });
        return out;
      },
      count: function () { return Object.keys(sel).length; },
      setTokens: function (tokens) {
        sel = fromTokens(tokens);
        persist(); paint(); onChange();
      },
      hasSelection: function () { return Object.keys(sel).length > 0; },
      clear: function () { sel = {}; persist(); paint(); onChange(); }
    };
  }

  global.BookScope = { mount: mount };
})(window);
