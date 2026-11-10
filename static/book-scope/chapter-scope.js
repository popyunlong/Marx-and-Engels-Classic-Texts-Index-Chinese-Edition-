/* Enable lazy chapter selection only for the homepage BookScope instance. */
(function (global) {
  'use strict';
  var baseMount = global.BookScope.mount;
  global.BookScope.mount = function (container, tree, options) {
    options = options || {};
    if (!options.chapters) return baseMount(container, tree, options);
    var selected = {}, records = {}, sources = {}, version = '', control, reloaders = [];
    var changed = options.onChange || function () {};
    tree.forEach(function (group) { (group.books || []).forEach(function (book) {
      (book.chapter_sources || []).forEach(function (source) {
        sources[source.file] = { book: book.key, volume: source.volume, title: '已选篇章（展开目录查看）', ancestors: [] };
      });
    }); });
    function record(token) {
      var identity = token.slice(8), cut = identity.lastIndexOf('#toc-');
      return records[token] || sources[identity.slice(0, cut)] || { title: '已选篇章（目录可能已更新）', ancestors: [] };
    }
    function covered(token, base) {
      var row = record(token);
      return base.indexOf('book:' + row.book) >= 0 || base.indexOf('vol:' + row.book + ':' + row.volume) >= 0;
    }
    control = baseMount(container, tree, Object.assign({}, options, {
      onChange: function () {
        if (!control) return;
        Object.keys(selected).forEach(function (t) { if (covered(t, control.getTokens())) delete selected[t]; });
        paint(); changed();
      }
    }));
    var originalTokens = control.getTokens.bind(control), originalSet = control.setTokens.bind(control);
    var originalClear = control.clear.bind(control);
    var summary = document.createElement('div'); summary.className = 'bscope-chapter-summary';
    summary.setAttribute('aria-live', 'polite'); control.root.querySelector('.bscope-panel').prepend(summary);
    control.root.querySelector('.bscope-panel-hint').textContent = '可选择著作、卷册或篇章。父篇章含子节；选篇章会将该书／卷收窄到所选篇章。';
    function paint() {
      var tokens = Object.keys(selected), base = originalTokens();
      summary.textContent = tokens.length ? '已选 ' + tokens.length + ' 个篇章：' + tokens.map(function (t) {
        var r = record(t); return (r.book ? r.book + '第' + r.volume + '卷 · ' : '') + r.title;
      }).join('；') : '';
      control.root.querySelectorAll('.bscope-chapter-check').forEach(function (cb) {
        var inherited = record(cb.value).ancestors.some(function (p) { return selected[p]; });
        cb.checked = !!selected[cb.value] || covered(cb.value, base) || inherited; cb.disabled = inherited;
      });
      control.root.querySelectorAll('.bscope-book').forEach(function (row) {
        if (tokens.some(function (t) { return record(t).book === row.dataset.key; })) {
          var cb = row.querySelector('.bscope-book-cb'); cb.checked = true; cb.indeterminate = true;
        }
      });
      if (tokens.length) {
        control.root.querySelector('.bscope-trigger-label').textContent = '已指定篇章';
        var count = control.root.querySelector('.bscope-count'); count.hidden = false; count.textContent = String(tokens.length);
      }
    }
    tree.forEach(function (group) { (group.books || []).forEach(function (book) {
      if (book.personal || !(book.chapter_volumes || []).length) return;
      var bookRow = Array.from(control.root.querySelectorAll('.bscope-book')).find(function (el) { return el.dataset.key === book.key; });
      var box = document.createElement('details'); box.className = 'bscope-chapter-box';
      var heading = document.createElement('summary'); heading.textContent = '选篇章'; box.appendChild(heading);
      book.chapter_volumes.forEach(function (volume) {
        var details = document.createElement('details'), title = document.createElement('summary');
        title.textContent = '第' + volume + '卷 · 篇章目录'; details.appendChild(title); box.appendChild(details);
        var loaded = false, busy = false;
        reloaders.push(function () {
          loaded = false;
          while (details.childNodes.length > 1) details.lastChild.remove();
          if (details.open) details.dispatchEvent(new Event('toggle'));
        });
        details.addEventListener('toggle', async function () {
          if (!details.open || loaded || busy) return;
          busy = true;
          var status = document.createElement('p'); status.setAttribute('role', 'status');
          status.textContent = '正在加载目录…'; details.appendChild(status);
          try {
            var response = await (options.fetch || global.fetch)('/api/search/chapters?book=' + encodeURIComponent(book.key) + '&volume=' + volume);
            var data = await response.json();
            if (!response.ok || !data.ok) throw new Error(data.error || '目录暂不可用，请重新展开重试。');
            if (Object.keys(selected).length && version !== data.scope_version) throw new Error('已选篇章版本已变化。请清除选择后重新选择篇章。');
            version = data.scope_version; loaded = true;
            status.textContent = data.results.length ? '父篇章包含子节；可筛选本卷篇名。' : '本卷暂无结构化目录，可继续按整卷检索。';
            var filter = document.createElement('input'); filter.type = 'search'; filter.placeholder = '筛选本卷篇名';
            filter.setAttribute('aria-label', book.label + '第' + volume + '卷篇名'); details.appendChild(filter);
            var list = document.createElement('div'); list.className = 'bscope-chapter-list'; details.appendChild(list);
            var stack = [];
            data.results.forEach(function (item) {
              while (stack.length && stack[stack.length - 1].level >= item.level) stack.pop();
              var ancestors = stack.map(function (a) { return a.token; }).filter(Boolean); stack.push(item);
              var label = document.createElement('label'); label.className = 'bscope-chapter-row';
              label.style.paddingLeft = Math.min(4, item.level - 1) * 12 + 'px'; label.dataset.title = item.title;
              var cb = document.createElement('input'); cb.type = 'checkbox'; cb.disabled = !item.selectable;
              if (item.selectable) {
                cb.className = 'bscope-chapter-check'; cb.value = item.token;
                records[item.token] = { book: book.key, volume: volume, title: item.title, ancestors: ancestors };
              }
              var text = document.createElement('span'); text.textContent = item.title + (item.selectable ? '' : ' · ' + item.reason);
              label.append(cb, text); list.appendChild(label);
              cb.addEventListener('change', function () {
                var base = originalTokens();
                if (base.some(function (t) { return t.indexOf('book:mylib:') === 0; })) {
                  status.textContent = '请先取消个人文库选择，再选择公共篇章。'; paint(); return;
                }
                if (Object.keys(selected).length && version !== data.scope_version) {
                  status.textContent = '目录版本已变化，请清除选择后重选。'; paint(); return;
                }
                version = data.scope_version;
                if (cb.checked) {
                  Object.keys(selected).forEach(function (t) { if (record(t).ancestors.indexOf(item.token) >= 0) delete selected[t]; });
                  selected[item.token] = true;
                  originalSet(base.filter(function (t) { return t !== 'book:' + book.key && t !== 'vol:' + book.key + ':' + volume; }));
                } else { delete selected[item.token]; originalSet(base); }
                paint(); changed();
              });
            });
            filter.addEventListener('input', function () {
              list.querySelectorAll('.bscope-chapter-row').forEach(function (row) { row.hidden = row.dataset.title.indexOf(filter.value.trim()) < 0; });
            });
            paint();
          } catch (error) { status.textContent = error.message; }
          finally { busy = false; }
        });
      });
      bookRow.appendChild(box);
    }); });
    function clearChapters() {
      selected = {}; version = ''; originalClear(); paint();
      reloaders.forEach(function (reload) { reload(); });
    }
    control.root.querySelector('.bscope-clear').addEventListener('click', clearChapters);
    control.root.addEventListener('change', function (event) {
      var cb = event.target;
      if (cb.classList.contains('bscope-book-cb') && !cb.checked) {
        var book = cb.closest('.bscope-book').dataset.key;
        Object.keys(selected).forEach(function (t) { if (record(t).book === book) delete selected[t]; });
      }
    }, true);
    control.getTokens = function () { return originalTokens().concat(Object.keys(selected)); };
    control.getScopeVersion = function () { return Object.keys(selected).length ? version : null; };
    control.hasSelection = function () { return control.getTokens().length > 0; };
    control.setTokens = function (tokens, restoredVersion) {
      selected = {};
      (tokens || []).forEach(function (t) { if (t.indexOf('chapter:') === 0) selected[t] = true; });
      version = restoredVersion || '';
      originalSet((tokens || []).filter(function (t) { return t.indexOf('chapter:') !== 0; })); paint();
    };
    control.clear = clearChapters;
    return control;
  };
})(window);
