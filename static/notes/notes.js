/*!
 * MarxNotes — 会员专属「笔记 / 我的知识库」共享前端模块。
 * 后端持久化（/api/notes*，数据落数据盘 notes.sqlite3），按登录用户归属隔离。
 * 一份模块服务三处：viewer.html（扫描版阅读器）、wenku_reader.html（文库/流式阅读器）、
 * knowledge_base.html（我的知识库聚合页）。命名空间 mxn-*，与站内其它组件零冲突。
 *
 * 公共 API（window.MarxNotes）：
 *   api.list({book,q,limit,offset}) / api.create(payload) / api.update(id,{body,color})
 *   api.remove(id) / api.books()
 *   mountReaderPanel(config) → { open, close, toggle, refresh, noteFromSelection, noteForCurrentPage }
 *   mountKnowledgeBase(target)
 *   toast(msg, isErr)
 */
(function () {
  "use strict";
  if (window.MarxNotes) return;

  // ---- 配置：CSRF / uid 从本脚本标签的 data-* 读取（与 reading-history 同范式） ----
  var SELF = document.currentScript ||
    (function () { var s = document.querySelectorAll('script[data-marx-notes]'); return s[s.length - 1] || null; })();
  var CSRF = (SELF && SELF.getAttribute('data-csrf')) || window.__MARX_CSRF__ || '';
  var UID = (SELF && SELF.getAttribute('data-uid')) || window.__MARX_UID__ || '';

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function el(tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  }

  // ---- 轻提示 ----
  var _toastEl = null, _toastTimer = null;
  function toast(msg, isErr) {
    if (!_toastEl) { _toastEl = el('div', 'mxn-toast'); document.body.appendChild(_toastEl); }
    _toastEl.textContent = String(msg || '');
    _toastEl.className = 'mxn-toast mxn-open' + (isErr ? ' mxn-err' : '');
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(function () { _toastEl.className = 'mxn-toast' + (isErr ? ' mxn-err' : ''); }, 2600);
  }

  // ---- API 客户端 ----
  function errMsgForStatus(st) {
    if (st === 401) return '请先登录会员账号后再记笔记。';
    if (st === 403) return '当前账号暂未开放「笔记」权限。';
    if (st === 429) return '操作过于频繁，请稍后再试。';
    if (st === 404) return '笔记不存在或已被删除。';
    if (st === 400) return '提交内容有误，请检查后重试。';
    return '操作未成功，请稍后再试。';
  }
  async function apiFetch(url, opts) {
    opts = opts || {};
    var headers = { 'Accept': 'application/json' };
    var k; if (opts.headers) { for (k in opts.headers) headers[k] = opts.headers[k]; }
    if (opts.body != null) headers['Content-Type'] = 'application/json';
    if (CSRF) headers['X-CSRF-Token'] = CSRF;
    var res;
    try {
      res = await fetch(url, { method: opts.method || 'GET', credentials: 'same-origin', headers: headers, body: opts.body });
    } catch (e) { throw new Error('网络异常，请检查连接后重试。'); }
    var data = null, ct = res.headers.get('Content-Type') || '';
    if (ct.indexOf('application/json') >= 0) { try { data = await res.json(); } catch (e) { } }
    if (!res.ok) {
      var msg = (data && (data.description || data.error || data.message)) || errMsgForStatus(res.status);
      var err = new Error(msg); err.status = res.status; throw err;
    }
    return data || {};
  }
  var api = {
    list: function (o) {
      o = o || {};
      var q = [];
      if (o.book) q.push('book=' + encodeURIComponent(o.book));
      if (o.q) q.push('q=' + encodeURIComponent(o.q));
      if (o.limit) q.push('limit=' + encodeURIComponent(o.limit));
      if (o.offset) q.push('offset=' + encodeURIComponent(o.offset));
      return apiFetch('/api/notes' + (q.length ? '?' + q.join('&') : ''));
    },
    books: function () { return apiFetch('/api/notes/books'); },
    create: function (payload) { return apiFetch('/api/notes', { method: 'POST', body: JSON.stringify(payload || {}) }); },
    update: function (id, patch) { return apiFetch('/api/notes/' + encodeURIComponent(id), { method: 'PATCH', body: JSON.stringify(patch || {}) }); },
    remove: function (id) { return apiFetch('/api/notes/' + encodeURIComponent(id), { method: 'DELETE' }); }
  };

  // ---- 编辑/新建弹层 ----
  var _editor = null;
  function ensureEditor() {
    if (_editor) return _editor;
    var mask = el('div', 'mxn-editor-mask');
    mask.innerHTML =
      '<div class="mxn-editor" role="dialog" aria-modal="true">' +
      '<h3 class="mxn-editor-title"></h3>' +
      '<p class="mxn-editor-sub"></p>' +
      '<div class="mxn-editor-quote" hidden></div>' +
      '<textarea class="mxn-editor-textarea" placeholder="写下你的批注、心得或问题…"></textarea>' +
      '<div class="mxn-editor-actions">' +
      '<span class="mxn-editor-err"></span>' +
      '<button type="button" class="mxn-btn mxn-cancel">取消</button>' +
      '<button type="button" class="mxn-btn mxn-btn-primary mxn-save">保存</button>' +
      '</div></div>';
    document.body.appendChild(mask);
    var refs = {
      mask: mask,
      title: mask.querySelector('.mxn-editor-title'),
      sub: mask.querySelector('.mxn-editor-sub'),
      quote: mask.querySelector('.mxn-editor-quote'),
      textarea: mask.querySelector('.mxn-editor-textarea'),
      err: mask.querySelector('.mxn-editor-err'),
      save: mask.querySelector('.mxn-save'),
      cancel: mask.querySelector('.mxn-cancel')
    };
    function close() { refs.mask.className = 'mxn-editor-mask'; refs._onSave = null; }
    refs.cancel.addEventListener('click', close);
    mask.addEventListener('click', function (e) { if (e.target === mask) close(); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && mask.classList.contains('mxn-open')) close(); });
    refs.save.addEventListener('click', async function () {
      var body = refs.textarea.value.trim();
      if (!body) { refs.err.textContent = '请填写笔记内容。'; refs.textarea.focus(); return; }
      if (!refs._onSave) return;
      refs.err.textContent = ''; refs.save.disabled = true; refs.save.textContent = '保存中…';
      try {
        await refs._onSave(body);
        close();
      } catch (e) {
        refs.err.textContent = e.message || '保存失败。';
      } finally {
        refs.save.disabled = false; refs.save.textContent = '保存';
      }
    });
    refs.close = close;
    _editor = refs;
    return refs;
  }
  // opts: { title, sub, quote, body, onSave(body)->Promise }
  function openEditor(opts) {
    opts = opts || {};
    var r = ensureEditor();
    r.title.textContent = opts.title || '记笔记';
    r.sub.textContent = opts.sub || '';
    if (opts.quote) { r.quote.hidden = false; r.quote.textContent = opts.quote; }
    else { r.quote.hidden = true; r.quote.textContent = ''; }
    r.textarea.value = opts.body || '';
    r.err.textContent = '';
    r._onSave = opts.onSave;
    r.mask.className = 'mxn-editor-mask mxn-open';
    setTimeout(function () { r.textarea.focus(); }, 40);
  }

  // ---- 笔记卡片 ----
  // opts: { showBook, onJump(note), onEdit(note), onDelete(note) }
  function renderNoteCard(note, opts) {
    opts = opts || {};
    var card = el('div', 'mxn-card');
    var html = '';
    if (note.quote) html += '<div class="mxn-card-quote">' + esc(note.quote) + '</div>';
    if (note.body) html += '<div class="mxn-card-body">' + esc(note.body) + '</div>';
    html += '<div class="mxn-card-meta">';
    if (opts.showBook && note.book_title) {
      html += '<span class="mxn-tag">' + esc(note.book_title) + (note.volume_label ? ' · ' + esc(note.volume_label) : '') + '</span>';
    }
    if (note.page_label) html += '<span class="mxn-tag">' + esc(note.page_label) + '</span>';
    html += '<span>' + esc(note.updated_display || note.created_display || '') + '</span>';
    html += '<span class="mxn-card-actions">';
    if (note.source_url || opts.onJump) html += '<button type="button" class="mxn-btn mxn-act-jump">跳转原文</button>';
    html += '<button type="button" class="mxn-btn mxn-act-edit">编辑</button>';
    html += '<button type="button" class="mxn-btn mxn-btn-danger mxn-act-del">删除</button>';
    html += '</span></div>';
    card.innerHTML = html;
    var jb = card.querySelector('.mxn-act-jump');
    if (jb) jb.addEventListener('click', function () { if (opts.onJump) opts.onJump(note); });
    card.querySelector('.mxn-act-edit').addEventListener('click', function () { if (opts.onEdit) opts.onEdit(note, card); });
    card.querySelector('.mxn-act-del').addEventListener('click', function () { if (opts.onDelete) opts.onDelete(note, card); });
    return card;
  }

  function editNote(note, afterUpdate) {
    openEditor({
      title: '编辑笔记', sub: (note.book_title || '') + (note.volume_label ? ' · ' + note.volume_label : ''),
      quote: note.quote, body: note.body,
      onSave: async function (body) {
        var res = await api.update(note.id, { body: body });
        toast('已更新');
        if (afterUpdate) afterUpdate(res.note || Object.assign({}, note, { body: body }));
      }
    });
  }
  async function deleteNote(note, afterDelete) {
    if (!window.confirm('确定删除这条笔记吗？删除后不可恢复。')) return;
    try { await api.remove(note.id); toast('已删除'); if (afterDelete) afterDelete(); }
    catch (e) { toast(e.message || '删除失败', true); }
  }

  // ---- 阅读器内笔记抽屉 ----
  function mountReaderPanel(cfg) {
    cfg = cfg || {};
    var reader = cfg.reader || 'viewer';
    var backdrop = el('div', 'mxn-backdrop');
    var drawer = el('div', 'mxn-drawer');
    drawer.innerHTML =
      '<div class="mxn-drawer-head">' +
      '<div class="mxn-drawer-title">笔记<small></small></div>' +
      '<button type="button" class="mxn-close" aria-label="关闭">×</button>' +
      '</div>' +
      '<div class="mxn-toolbar">' +
      '<input type="search" class="mxn-search" placeholder="搜索本卷笔记…">' +
      '<button type="button" class="mxn-btn mxn-btn-primary mxn-new">＋ 本页笔记</button>' +
      '</div>' +
      '<div class="mxn-list"></div>';
    document.body.appendChild(backdrop);
    document.body.appendChild(drawer);
    var sub = drawer.querySelector('.mxn-drawer-title small');
    var listEl = drawer.querySelector('.mxn-list');
    var searchEl = drawer.querySelector('.mxn-search');
    var newBtn = drawer.querySelector('.mxn-new');
    var _notes = [], _q = '';

    function bookMeta() { try { return cfg.bookMeta ? (cfg.bookMeta() || {}) : {}; } catch (e) { return {}; } }
    function close() { drawer.classList.remove('mxn-open'); backdrop.classList.remove('mxn-open'); }
    function open() {
      var m = bookMeta();
      sub.textContent = (m.book_title || '') + (m.volume_label ? ' · ' + m.volume_label : '');
      drawer.classList.add('mxn-open'); backdrop.classList.add('mxn-open');
      refresh();
    }
    function toggle() { drawer.classList.contains('mxn-open') ? close() : open(); }
    backdrop.addEventListener('click', close);
    drawer.querySelector('.mxn-close').addEventListener('click', close);

    function doJump(note) { close(); try { if (cfg.jumpTo) cfg.jumpTo(note); } catch (e) { } }

    function paint() {
      listEl.innerHTML = '';
      var rows = _notes;
      if (!rows.length) {
        listEl.appendChild(el('div', 'mxn-empty',
          _q ? '没有匹配「<b>' + esc(_q) + '</b>」的笔记。'
             : '本卷还没有笔记。<br>选中原文或点「<b>＋ 本页笔记</b>」开始记录。'));
        return;
      }
      rows.forEach(function (note) {
        listEl.appendChild(renderNoteCard(note, {
          showBook: false,
          onJump: (note.source_url || cfg.jumpTo) ? doJump : null,
          onEdit: function (n) { editNote(n, function () { refresh(); }); },
          onDelete: function (n) { deleteNote(n, function () { refresh(); }); }
        }));
      });
    }
    async function refresh() {
      var m = bookMeta();
      if (!m.book_key) { _notes = []; paint(); return; }
      listEl.innerHTML = '<div class="mxn-empty">加载中…</div>';
      try {
        var res = await api.list({ book: m.book_key, q: _q, limit: 1000 });
        _notes = (res && res.notes) || [];
      } catch (e) { _notes = []; toast(e.message || '加载失败', true); }
      paint();
    }
    var _st;
    searchEl.addEventListener('input', function () {
      clearTimeout(_st); _q = searchEl.value.trim();
      _st = setTimeout(refresh, 220);
    });

    function createFrom(anchor) {
      var m = bookMeta();
      if (!m.book_key) { toast('无法确定当前书籍', true); return; }
      anchor = anchor || {};
      openEditor({
        title: anchor.quote ? '为所选原文记笔记' : '记本页笔记',
        sub: (m.book_title || '') + (m.volume_label ? ' · ' + m.volume_label : '') + (anchor.page_label ? ' · ' + anchor.page_label : ''),
        quote: anchor.quote || '',
        onSave: async function (body) {
          var res = await api.create({
            reader: reader,
            book_key: m.book_key, book_title: m.book_title || '', volume_label: m.volume_label || '',
            page: (anchor.page != null ? anchor.page : null), page_label: anchor.page_label || '',
            doc_path: anchor.doc_path || '', anchor_text: anchor.anchor_text || '',
            quote: anchor.quote || '', body: body, source_url: anchor.source_url || ''
          });
          toast('已保存到知识库');
          if (!drawer.classList.contains('mxn-open')) open(); else refresh();
          return res;
        }
      });
    }
    function noteForCurrentPage() {
      var loc = {}; try { loc = cfg.currentLocation ? (cfg.currentLocation() || {}) : {}; } catch (e) { }
      createFrom(loc);
    }
    function noteFromSelection() {
      var sel = null; try { sel = cfg.getSelection ? cfg.getSelection() : null; } catch (e) { }
      if (!sel || !sel.quote) { toast('请先在原文中选中要记录的文字'); return; }
      createFrom(sel);
    }
    newBtn.addEventListener('click', noteForCurrentPage);

    // 可选：由模块直接注入一个「笔记」开关按钮到指定容器（复用阅读器按钮样式）。
    if (cfg.buttonHost) {
      var btn = el('button', cfg.buttonClass || 'mxn-btn');
      btn.type = 'button';
      btn.innerHTML = cfg.buttonLabel || '笔记';
      btn.addEventListener('click', toggle);
      cfg.buttonHost.appendChild(btn);
    }

    // 可选：选区自动气泡——在指定容器（如扫描版 OCR 文本 #pageText）内选中文字，就地弹出「记笔记」，
    // 点它即用当前选区建笔记。cfg.selectionBubble 传选择器字符串或 {container}。
    // （wenku 阅读器另有自己的选区气泡，不用此项。）
    if (cfg.selectionBubble) {
      var sbRaw = cfg.selectionBubble.container || cfg.selectionBubble;
      var sbContainer = typeof sbRaw === 'string' ? document.querySelector(sbRaw) : sbRaw;
      if (sbContainer) {
        var selPop = el('div', 'mxn-sel-pop');
        selPop.innerHTML = '<button type="button" class="mxn-sel-btn">✎ 记笔记</button>';
        selPop.style.display = 'none';
        document.body.appendChild(selPop);
        var hideSelPop = function () { selPop.style.display = 'none'; };
        var showSelPop = function () {
          var sel = window.getSelection();
          if (!sel || sel.isCollapsed || !sel.rangeCount) { hideSelPop(); return; }
          var range = sel.getRangeAt(0);
          if (!sbContainer.contains(range.commonAncestorContainer)) { hideSelPop(); return; }
          if (!(sel.toString() || '').trim()) { hideSelPop(); return; }
          var r = range.getBoundingClientRect();
          if (!r || (!r.width && !r.height)) { hideSelPop(); return; }
          selPop.style.left = Math.max(8, Math.min(r.left, window.innerWidth - 130)) + 'px';
          selPop.style.top = Math.min(r.bottom + 8, window.innerHeight - 48) + 'px';
          selPop.style.display = 'block';
        };
        var sbBtn = selPop.querySelector('.mxn-sel-btn');
        sbBtn.addEventListener('mousedown', function (e) { e.preventDefault(); }); // 别因点按钮清掉选区
        sbBtn.addEventListener('click', function () { hideSelPop(); noteFromSelection(); });
        document.addEventListener('mouseup', function (e) { if (selPop.contains(e.target)) return; setTimeout(showSelPop, 10); });
        document.addEventListener('selectionchange', function () { var s = window.getSelection(); if (!s || s.isCollapsed) hideSelPop(); });
        window.addEventListener('scroll', hideSelPop, true);
        window.addEventListener('resize', hideSelPop);
      }
    }

    return { open: open, close: close, toggle: toggle, refresh: refresh, noteFromSelection: noteFromSelection, noteForCurrentPage: noteForCurrentPage, el: drawer };
  }

  // ---- 「我的知识库」聚合页 ----
  function download(name, text, mime) {
    var blob = new Blob([text], { type: mime || 'text/plain;charset=utf-8' });
    var url = URL.createObjectURL(blob);
    var a = el('a'); a.href = url; a.download = name; document.body.appendChild(a); a.click();
    setTimeout(function () { URL.revokeObjectURL(url); a.remove(); }, 400);
  }
  function notesToMarkdown(rows) {
    var byBook = {};
    rows.forEach(function (n) {
      var key = (n.book_title || '未命名') + (n.volume_label ? ' · ' + n.volume_label : '');
      (byBook[key] = byBook[key] || []).push(n);
    });
    var out = ['# 我的知识库 · 笔记导出', '', '导出时间：' + new Date().toLocaleString(), ''];
    Object.keys(byBook).forEach(function (book) {
      out.push('## ' + book, '');
      byBook[book].forEach(function (n) {
        if (n.page_label) out.push('### ' + n.page_label);
        if (n.quote) out.push('> ' + String(n.quote).replace(/\n/g, '\n> '));
        if (n.body) out.push('', n.body);
        out.push('', '_' + (n.updated_display || n.created_display || '') + '_', '', '---', '');
      });
    });
    return out.join('\n');
  }

  function mountKnowledgeBase(target) {
    var root = typeof target === 'string' ? document.querySelector(target) : target;
    if (!root) return;
    root.classList.add('mxn-kb');
    root.innerHTML =
      '<div class="mxn-kb-head">' +
      '<div><h1 class="mxn-kb-title">我的知识库</h1><p class="mxn-kb-sub">你在各阅读器里记下的笔记，都会汇聚到这里。</p></div>' +
      '<div class="mxn-kb-actions">' +
      '<button type="button" class="mxn-btn mxn-exp-md">导出 Markdown</button>' +
      '<button type="button" class="mxn-btn mxn-exp-json">导出备份(JSON)</button>' +
      '</div></div>' +
      '<div class="mxn-kb-toolbar">' +
      '<input type="search" class="mxn-search" placeholder="搜索全部笔记（原文摘录 / 笔记正文 / 书名）…">' +
      '<span class="mxn-kb-stat"></span>' +
      '</div>' +
      '<div class="mxn-kb-groups"><div class="mxn-kb-loading">加载中…</div></div>';
    var groupsEl = root.querySelector('.mxn-kb-groups');
    var statEl = root.querySelector('.mxn-kb-stat');
    var searchEl = root.querySelector('.mxn-search');
    var _all = [], _q = '';

    function groupAndPaint(rows) {
      groupsEl.innerHTML = '';
      if (!rows.length) {
        groupsEl.appendChild(el('div', 'mxn-kb-empty',
          _q ? '没有匹配「<b>' + esc(_q) + '</b>」的笔记。'
             : '还没有任何笔记。<br>打开任意阅读器，选中原文即可「记笔记」，它们会汇聚到这里。'));
        return;
      }
      var order = [], byKey = {};
      rows.forEach(function (n) {
        var k = n.book_key || '_';
        if (!byKey[k]) { byKey[k] = { key: k, title: n.book_title || '未命名', vol: n.volume_label || '', items: [] }; order.push(k); }
        byKey[k].items.push(n);
      });
      order.forEach(function (k, gi) {
        var g = byKey[k];
        var group = el('div', 'mxn-kb-group' + (gi === 0 ? ' mxn-open' : ''));
        var head = el('div', 'mxn-kb-group-head');
        head.innerHTML = '<span class="mxn-kb-caret">▶</span>' +
          '<span class="mxn-kb-group-title">' + esc(g.title) + (g.vol ? '<small>' + esc(g.vol) + '</small>' : '') + '</span>' +
          '<span class="mxn-kb-count">' + g.items.length + ' 条</span>';
        head.addEventListener('click', function () { group.classList.toggle('mxn-open'); });
        var bodyEl = el('div', 'mxn-kb-group-body');
        g.items.forEach(function (note) {
          bodyEl.appendChild(renderNoteCard(note, {
            showBook: false,
            onJump: note.source_url ? function (n) { window.location.href = n.source_url; } : null,
            onEdit: function (n) { editNote(n, function (updated) { var i = _all.findIndex(function (x) { return x.id === n.id; }); if (i >= 0) _all[i] = updated; applyFilter(); }); },
            onDelete: function (n) { deleteNote(n, function () { _all = _all.filter(function (x) { return x.id !== n.id; }); applyFilter(); }); }
          }));
        });
        group.appendChild(head); group.appendChild(bodyEl);
        groupsEl.appendChild(group);
      });
    }
    function applyFilter() {
      var rows = _all;
      if (_q) {
        var q = _q.toLowerCase();
        rows = _all.filter(function (n) {
          return (n.quote && n.quote.toLowerCase().indexOf(q) >= 0) ||
            (n.body && n.body.toLowerCase().indexOf(q) >= 0) ||
            (n.book_title && n.book_title.toLowerCase().indexOf(q) >= 0);
        });
      }
      statEl.textContent = '共 ' + _all.length + ' 条笔记' + (_q ? '，匹配 ' + rows.length + ' 条' : '');
      groupAndPaint(rows);
    }
    var _st;
    searchEl.addEventListener('input', function () { clearTimeout(_st); _q = searchEl.value.trim(); _st = setTimeout(applyFilter, 180); });
    root.querySelector('.mxn-exp-md').addEventListener('click', function () {
      if (!_all.length) { toast('还没有笔记可导出'); return; }
      download('我的知识库_' + Date.now() + '.md', notesToMarkdown(_all), 'text/markdown;charset=utf-8');
    });
    root.querySelector('.mxn-exp-json').addEventListener('click', function () {
      if (!_all.length) { toast('还没有笔记可导出'); return; }
      download('我的知识库备份_' + Date.now() + '.json', JSON.stringify({ exported_at: new Date().toISOString(), notes: _all }, null, 2), 'application/json');
    });

    (async function load() {
      try {
        var res = await api.list({ limit: 2000 });
        _all = (res && res.notes) || [];
        applyFilter();
      } catch (e) {
        groupsEl.innerHTML = '';
        groupsEl.appendChild(el('div', 'mxn-kb-empty', esc(e.message || '加载失败，请刷新重试。')));
      }
    })();
  }

  window.MarxNotes = {
    api: api,
    toast: toast,
    openEditor: openEditor,
    renderNoteCard: renderNoteCard,
    mountReaderPanel: mountReaderPanel,
    mountKnowledgeBase: mountKnowledgeBase,
    uid: UID
  };

  // 自动挂载知识库页（若存在容器）。
  document.addEventListener('DOMContentLoaded', function () {
    var kb = document.querySelector('[data-marx-kb]');
    if (kb) mountKnowledgeBase(kb);
  });
})();
