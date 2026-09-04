/* 个人文库前端模块。
 *
 * 三个页面共用：/mylib（书目）、/mylib/upload（上传）、/mylib/<id>（阅读器）。
 * 与 notes.js 同规格：脚本标签上带 data-uid / data-csrf，靠 data-* 挂载点自动初始化。
 */
(function () {
  'use strict';

  var el = document.currentScript;
  var CSRF = (el && el.getAttribute('data-csrf')) || '';
  var UID = (el && el.getAttribute('data-uid')) || '';

  function api(path, opts) {
    opts = opts || {};
    var headers = opts.headers || {};
    if (opts.method && opts.method !== 'GET') headers['X-CSRF-Token'] = CSRF;
    return fetch(path, {
      method: opts.method || 'GET',
      headers: headers,
      body: opts.body,
      credentials: 'same-origin'
    }).then(function (r) {
      if (r.status === 401 || r.status === 403) {
        throw new Error('登录状态已失效或无权限，请刷新后重试。');
      }
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) throw new Error(j.description || j.error || ('请求失败（' + r.status + '）'));
        return j;
      });
    });
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  /* ---------------- 我的文库 ---------------- */
  var STATUS_VIEW = {
    ready: { tag: 'ok', text: '已上架', cover: '📖' },
    storing: { tag: 'wait', text: '安全入库中', cover: '⇧' },
    pending: { tag: 'wait', text: '审核中', cover: '⏳' },
    queued: { tag: 'wait', text: '待解析', cover: '⏳' },
    parsing: { tag: 'wait', text: '解析中', cover: '⚙️' },
    quality_review: { tag: 'wait', text: '数据复核中', cover: '🔎' },
    rejected: { tag: 'rej', text: '已退回', cover: '✕' },
    failed: { tag: 'rej', text: '解析失败', cover: '⚠' }
  };

  function bookCard(b) {
    var v = STATUS_VIEW[b.status] || { tag: 'ro', text: b.status, cover: '📄' };
    var tags = '<span class="mx-tag ' + v.tag + '">' + v.text + '</span>';
    if (b.status === 'ready') {
      tags += b.searchable
        ? '<span class="mx-tag ai">可检索 · 可引文</span>'
        : '<span class="mx-tag ro">仅可读</span>';
      if (b.toc_count) {
        var tocLabel = b.quality && b.quality.toc_label ? ' · ' + esc(b.quality.toc_label) : '';
        tags += '<span class="mx-tag ro">目录 ' + Number(b.toc_count) + ' 条' + tocLabel + '</span>';
      }
    }
    var note = '';
    if (b.status === 'rejected' && b.reject_reason) {
      note = '<div class="mx-bk-note">退回原因：' + esc(b.reject_reason) + '</div>';
    } else if (b.status === 'failed' && b.fail_reason) {
      note = '<div class="mx-bk-note">失败原因：' + esc(b.fail_reason) + '</div>';
    } else if (b.status === 'parsing') {
      note = '<div class="mx-bk-note">正在解析' +
        (b.progress_total ? '（' + b.progress_done + '/' + b.progress_total + ' 页）' : '') +
        '，完成后会邮件通知你。</div>';
    } else if (b.status === 'storing') {
      note = '<div class="mx-bk-note">文件已由服务器接收，正在后台安全写入存储节点；完成后会自动进入审核，无需重复提交。</div>';
    } else if (b.status === 'pending') {
      note = '<div class="mx-bk-note">已提交，等待管理员审核。</div>';
    } else if (b.status === 'quality_review') {
      var blockers = b.acceptance && Array.isArray(b.acceptance.blocking)
        ? b.acceptance.blocking.filter(Boolean) : [];
      note = '<div class="mx-bk-note">文件已完成解析；系统正在分别核对文字层、书目、目录、页码映射与原图。' +
        (blockers.length ? '<br>待核对：' + esc(blockers.join('；')) : '') +
        '<br>核验通过前不会进入检索，也不会误报为上传失败。</div>';
    } else if (b.status === 'ready' && !b.searchable) {
      note = '<div class="mx-bk-note">本书未建立可用文字层，仍可正常阅读与记笔记。</div>';
    }

    var acts = '';
    if (b.status === 'ready') {
      acts = '<a class="mx-btn pri sm" href="/mylib/' + b.id + '">阅读</a>' +
             '<button class="mx-btn sm" data-del="' + b.id + '">删除</button>';
    } else if (b.status === 'rejected' || b.status === 'failed') {
      acts = '<a class="mx-btn sm" href="/mylib/upload">重新上传</a>' +
             '<button class="mx-btn sm" data-del="' + b.id + '">删除</button>';
    } else {
      acts = '<button class="mx-btn sm" disabled>处理中</button>' +
             '<button class="mx-btn sm" data-del="' + b.id + '">撤回</button>';
    }

    return '<div class="mx-bk"><div class="mx-bk-top">' +
      '<div class="mx-bk-cover">' + v.cover + '</div>' +
      '<div style="flex:1;min-width:0">' +
      '<div class="mx-bk-title">' + esc(b.title) + '</div>' +
      '<div class="mx-bk-meta">' + esc(b.author || '—') +
      (b.pages ? ' · ' + b.pages + ' 页' : '') +
      (b.size_mb ? ' · ' + b.size_mb + ' MB' : '') + '</div>' +
      '<div class="mx-tags">' + tags + '</div>' +
      '</div></div>' + note +
      '<div class="mx-bk-acts">' + acts + '</div></div>';
  }

  function mountHome(root) {
    function render() {
      api('/api/mylib/books').then(function (d) {
        var books = d.books || [];
        var list = books.length
          ? '<div class="mx-books">' + books.map(bookCard).join('') + '</div>'
          : '<div class="mx-empty">个人文库还是空的。<br>点右上「＋ 上传新书」，' +
            '经管理员审核后即可在专属阅读器中阅读，并可在检索与 AI 问答中使用。</div>';
        root.innerHTML =
          '<div class="mx-head"><div><h2>我上传的书</h2>' +
          '<p class="mx-sub">仅你本人可见、可检索。每本需经管理员合规审核后上架。</p></div>' +
          '<a class="mx-btn pri" href="/mylib/upload">＋ 上传新书</a></div>' +
          '<div class="mx-quota">共 <b>' + books.length + '</b> 本 · 已上架 <b>' +
          (d.quota_used || 0) + '</b> · 册数额度 <b>' + (d.quota_used || 0) + ' / ' +
          (d.quota_max || 0) + '</b> · 单本上限 ' + (d.max_mb || 100) + ' MB</div>' + list;

        root.querySelectorAll('[data-del]').forEach(function (btn) {
          btn.addEventListener('click', function () {
            var id = btn.getAttribute('data-del');
            if (!window.confirm('确定删除这本书？\n\n删除后其检索索引与存储文件一并清除，不可撤销。')) return;
            btn.disabled = true;
            api('/api/mylib/' + id + '/delete', { method: 'POST' })
              .then(render)
              .catch(function (e) { btn.disabled = false; window.alert(e.message); });
          });
        });

        // 有书在解析中时轮询刷新，让进度可见（解析完成即停）
        if (books.some(function (b) {
          return b.status === 'storing' || b.status === 'parsing' || b.status === 'queued' ||
            b.status === 'quality_review';
        })) {
          clearTimeout(mountHome._t);
          mountHome._t = setTimeout(render, 5000);
        }
      }).catch(function (e) {
        root.innerHTML = '<div class="mx-empty">' + esc(e.message) + '</div>';
      });
    }
    render();
  }

  /* ---------------- 上传 ---------------- */
  function mountUpload(root) {
    var fileInput = root.querySelector('#mxFile');
    var info = root.querySelector('#mxFileInfo');
    var btn = root.querySelector('#mxSubmit');
    var err = root.querySelector('#mxErr');
    var ocrRow = root.querySelector('#mxOcrRow');
    var picked = null;

    root.querySelector('#mxDrop').addEventListener('click', function () { fileInput.click(); });

    fileInput.addEventListener('change', function () {
      var f = fileInput.files && fileInput.files[0];
      picked = null; ocrRow.hidden = true; info.innerHTML = '';
      if (!f) return;
      var mb = f.size / 1048576;
      var reader = new FileReader();
      reader.onload = function (e) {
        var head = new Uint8Array(e.target.result), sig = '';
        for (var i = 0; i < 5 && i < head.length; i++) sig += String.fromCharCode(head[i]);
        var okSig = sig === '%PDF-';
        var limit = parseInt(root.getAttribute('data-max-mb') || '100', 10);
        var okSize = mb <= limit;
        info.innerHTML = '<div class="mx-file"><div class="ico">PDF</div><div style="flex:1">' +
          '<b>' + esc(f.name) + '</b><div style="color:var(--muted);margin-top:3px">' +
          (okSig ? '<span style="color:var(--ok);font-weight:700">✓ 文件格式校验通过</span>'
                 : '<span style="color:var(--accent);font-weight:700">✕ 不是有效的 PDF 文件</span>') +
          ' · ' +
          (okSize ? mb.toFixed(2) + ' MB'
                  : '<span style="color:var(--accent);font-weight:700">' + mb.toFixed(2) +
                    ' MB，超过 ' + limit + 'MB 上限</span>') +
          '</div></div></div>';
        if (okSig && okSize) { picked = f; ocrRow.hidden = false; }
      };
      reader.readAsArrayBuffer(f.slice(0, 5));
    });

    btn.addEventListener('click', function () {
      err.textContent = '';
      var title = (root.querySelector('#mxTitle').value || '').trim();
      if (!title) { err.textContent = '请填写书名。'; return; }
      if (!picked) { err.textContent = '请选择一个有效的 PDF 文件。'; return; }
      if (!root.querySelector('#mxAttest').checked) { err.textContent = '请先勾选权利声明。'; return; }

      var fd = new FormData();
      fd.append('title', title);
      fd.append('author', (root.querySelector('#mxAuthor').value || '').trim());
      fd.append('license_attested', '1');
      if (root.querySelector('#mxOcr').checked) fd.append('ocr_consent', '1');
      fd.append('file', picked);

      btn.disabled = true;
      btn.textContent = '上传中…';
      api('/api/mylib/upload', { method: 'POST', body: fd })
        .then(function () {
          btn.textContent = '已接收，正在安全入库…';
          window.location.href = '/mylib';
        })
        .catch(function (e) {
          // 边缘代理若在大文件上传后先超时，源站仍可能已经完成登记。不要把 524 再次
          // 呈现成“提交失败”，先回到本人书目核对；服务端 SHA-256 幂等也会拦住重复记录。
          if (/524/.test(e.message || '')) {
            btn.textContent = '正在核对服务器记录…';
            err.textContent = '网络等待超时，但服务器可能已收到文件，正在返回个人文库核对，请勿重复提交。';
            window.setTimeout(function () { window.location.href = '/mylib'; }, 1800);
            return;
          }
          btn.disabled = false;
          btn.textContent = '提交审核';
          err.textContent = e.message;
        });
    });
  }

  /* ---------------- 阅读器 ---------------- */
  function mountReader(root) {
    var sid = parseInt(root.getAttribute('data-sid'), 10);
    var total = parseInt(root.getAttribute('data-pages'), 10) || 1;
    var title = root.getAttribute('data-title') || '';
    var img = root.querySelector('#mxPage');
    var label = root.querySelector('#mxPageLabel');
    var prev = root.querySelector('#mxPrev');
    var next = root.querySelector('#mxNext');
    var jump = root.querySelector('#mxJump');
    var tocCard = root.querySelector('#mxTocCard');
    var tocEl = root.querySelector('#mxToc');
    var tocCount = root.querySelector('#mxTocCount');
    var searchForm = root.querySelector('#mxBookSearchForm');
    var searchInput = root.querySelector('#mxBookSearchInput');
    var searchStatus = root.querySelector('#mxBookSearchStatus');
    var searchResults = root.querySelector('#mxBookSearchResults');
    var pageText = root.querySelector('#mxPageText');
    var pageTextMeta = root.querySelector('#mxPageTextMeta');
    var pageCitation = root.querySelector('#mxPageCitation');
    var copyPageText = root.querySelector('#mxCopyPageText');
    var pageTextRequest = 0;
    var currentPageData = null;

    var params = new URLSearchParams(window.location.search);
    var page = Math.min(Math.max(parseInt(params.get('page'), 10) || 1, 1), total);

    function pageLabel(data, fallbackPage) {
      return data && data.printed_page ? '第 ' + data.printed_page + ' 页' : 'PDF 第 ' + fallbackPage + ' 页';
    }

    function loadPageText() {
      if (!pageText) return;
      var requestedPage = page;
      var requestId = ++pageTextRequest;
      currentPageData = null;
      pageText.textContent = '正在加载本页文字…';
      if (pageTextMeta) pageTextMeta.textContent = '与上方 PDF 第 ' + requestedPage + ' 页对应';
      if (pageCitation) { pageCitation.hidden = true; pageCitation.textContent = ''; }
      api('/api/mylib/' + sid + '/page-text?page=' + requestedPage).then(function (data) {
        if (requestId !== pageTextRequest || requestedPage !== page) return;
        currentPageData = data;
        pageText.textContent = data.text || '本页未识别到文字，可继续查看上方原始页面。';
        if (pageTextMeta) {
          pageTextMeta.textContent = pageLabel(data, requestedPage) +
            (data.section_title ? ' · ' + data.section_title : '') + ' · 可选择复制';
        }
        if (pageCitation && data.citation) {
          pageCitation.textContent = data.citation;
          pageCitation.hidden = false;
        }
      }).catch(function (err) {
        if (requestId !== pageTextRequest || requestedPage !== page) return;
        pageText.textContent = err.message || '本页文字加载失败，请稍后重试。';
        if (pageTextMeta) pageTextMeta.textContent = '文字层暂不可用';
      });
    }

    function copyText(value, button, doneLabel) {
      if (!value) return;
      var finished = function () {
        if (!button) return;
        var before = button.textContent;
        button.textContent = doneLabel || '已复制';
        window.setTimeout(function () { button.textContent = before; }, 1200);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(value).then(finished).catch(function () {});
        return;
      }
      var area = document.createElement('textarea');
      area.value = value; area.style.position = 'fixed'; area.style.opacity = '0';
      document.body.appendChild(area); area.select();
      try { document.execCommand('copy'); finished(); } catch (e) { /* 忽略旧浏览器失败 */ }
      area.remove();
    }

    if (copyPageText && pageText) {
      copyPageText.addEventListener('click', function () {
        copyText(currentPageData && currentPageData.text, copyPageText, '已复制本页');
      });
    }

    function paint() {
      img.src = '/api/mylib/page-image?sid=' + sid + '&page=' + page;
      label.textContent = '第 ' + page + ' / ' + total + ' 页';
      prev.disabled = page <= 1;
      next.disabled = page >= total;
      if (jump) jump.value = page;
      loadPageText();
      try {
        var u = new URL(window.location.href);
        u.searchParams.set('page', page);
        window.history.replaceState(null, '', u.toString());
      } catch (e) { /* 老浏览器忽略 */ }
      // 「继续阅读」：与其它阅读器同一套纯前端历史
      if (window.MarxReadingHistory) {
        window.MarxReadingHistory.record({
          book: 'mylib:' + sid,
          kind: 'mylib',
          title: title,
          subtitle: '第 ' + page + ' 页',
          url: '/mylib/' + sid + '?page=' + page
        });
      }
    }

    function go(n) {
      page = Math.min(Math.max(n, 1), total);
      paint();
    }

    function searchContext(value) {
      return esc(value || '')
        .replace(/\[\[H\]\]/g, '<mark>')
        .replace(/\[\[\/H\]\]/g, '</mark>');
    }

    // 阅读器内直接“查找本书”：结果与引文来自同一份私有索引，点击即跳到命中 PDF 页。
    if (searchForm && searchInput && searchResults) {
      searchForm.addEventListener('submit', function (e) {
        e.preventDefault();
        var query = (searchInput.value || '').trim();
        if (query.replace(/\s+/g, '').length < 2) {
          searchStatus.textContent = '请至少输入两个有效字符';
          return;
        }
        var submit = searchForm.querySelector('button[type="submit"]');
        submit.disabled = true;
        searchStatus.textContent = '正在检索本书…';
        searchResults.hidden = true;
        api('/api/mylib/' + sid + '/search?q=' + encodeURIComponent(query)).then(function (data) {
          var hits = Array.isArray(data.results) ? data.results : [];
          searchStatus.textContent = hits.length ? '找到 ' + hits.length + ' 处命中' : '本书中未找到匹配原文';
          if (!hits.length) {
            searchResults.innerHTML = '';
            return;
          }
          searchResults.innerHTML = hits.map(function (hit, index) {
            var p = parseInt((hit.pdf_pages || [1])[0], 10) || 1;
            var printed = (hit.printed_pages || [])[0];
            var pageText = printed ? '原书第 ' + esc(printed) + ' 页' : 'PDF 第 ' + p + ' 页';
            var section = hit.section_title ? '<div class="mx-search-section">' + esc(hit.section_title) + '</div>' : '';
            var citation = hit.citation || '';
            return '<article class="mx-search-hit" data-search-page="' + p + '">' +
              '<div class="mx-search-hit-head"><b>' + pageText + '</b><span>跳转 →</span></div>' +
              section + '<div class="mx-search-context">' + searchContext(hit.context) + '</div>' +
              '<div class="mx-search-cite" id="mxSearchCite' + index + '">' + esc(citation) + '</div>' +
              '<button type="button" class="mx-copy-cite" data-copy-cite="' + index + '">复制引文</button>' +
              '</article>';
          }).join('');
          searchResults.hidden = false;
        }).catch(function (err) {
          searchStatus.textContent = err.message || '检索失败，请稍后重试';
          searchResults.innerHTML = '';
        }).then(function () {
          submit.disabled = false;
        });
      });

      searchResults.addEventListener('click', function (e) {
        var copy = e.target.closest && e.target.closest('[data-copy-cite]');
        if (copy) {
          e.stopPropagation();
          var cite = root.querySelector('#mxSearchCite' + copy.getAttribute('data-copy-cite'));
          var value = cite ? cite.textContent : '';
          if (navigator.clipboard && value) {
            navigator.clipboard.writeText(value).then(function () {
              copy.textContent = '已复制';
              window.setTimeout(function () { copy.textContent = '复制引文'; }, 1200);
            }).catch(function () { copy.textContent = '复制失败'; });
          }
          return;
        }
        var hit = e.target.closest && e.target.closest('[data-search-page]');
        if (hit) {
          go(parseInt(hit.getAttribute('data-search-page'), 10) || 1);
          if (window.innerWidth < 900) root.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
      });
    }

    prev.addEventListener('click', function () { go(page - 1); });
    next.addEventListener('click', function () { go(page + 1); });
    if (jump) {
      jump.addEventListener('change', function () { go(parseInt(jump.value, 10) || 1); });
    }
    document.addEventListener('keydown', function (e) {
      if (e.target && /input|textarea/i.test(e.target.tagName)) return;
      if (e.key === 'ArrowLeft') go(page - 1);
      if (e.key === 'ArrowRight') go(page + 1);
    });
    img.addEventListener('error', function () {
      label.textContent = '第 ' + page + ' 页加载失败，请稍后重试';
    });

    // 目录与检索共用个人索引里的 toc_entries：点击目录直接跳到同一个私有阅读器页。
    // API 仍会复核 owner + ready，浏览器里改 sid 不能看到别人的书目。
    if (tocCard && tocEl) {
      api('/api/mylib/' + sid + '/toc').then(function (data) {
        var entries = Array.isArray(data.entries) ? data.entries : [];
        if (!entries.length) return;
        tocCard.hidden = false;
        if (tocCount) tocCount.textContent = entries.length + ' 条';
        tocEl.innerHTML = entries.map(function (entry) {
          var p = parseInt(entry.pdf_page, 10) || 1;
          var level = Math.min(Math.max(parseInt(entry.level, 10) || 1, 1), 6);
          var pageText = entry.printed_page ? '第 ' + esc(entry.printed_page) + ' 页' : 'PDF ' + p;
          return '<button type="button" class="mx-toc-item lv' + level + '" data-toc-page="' + p + '">' +
            '<span>' + esc(entry.title) + '</span><small>' + pageText + '</small></button>';
        }).join('');
        tocEl.querySelectorAll('[data-toc-page]').forEach(function (button) {
          button.addEventListener('click', function () {
            go(parseInt(button.getAttribute('data-toc-page'), 10) || 1);
            if (window.innerWidth < 900) root.scrollIntoView({ behavior: 'smooth', block: 'start' });
          });
        });
      }).catch(function () { /* 没有目录或迁移中时保持隐藏，不影响逐页阅读 */ });
    }

    // 笔记：与扫描版/流式/文库三个阅读器共用同一个「我的知识库」（契约见 notes.js mountReaderPanel）
    var notesBtn = root.querySelector('#mxNoteBtn');
    if (notesBtn && window.MarxNotes && window.MarxNotes.mountReaderPanel) {
      var srcUrl = function () { return '/mylib/' + sid + '?page=' + page; };
      var panel = window.MarxNotes.mountReaderPanel({
        reader: 'mylib',
        bookMeta: function () {
          return { book_key: 'mylib:' + sid, book_title: title, volume_label: '' };
        },
        currentLocation: function () {
          return {
            page: page,
            page_label: pageLabel(currentPageData, page),
            source_url: srcUrl(),
            anchor_text: currentPageData && currentPageData.section_title || ''
          };
        },
        getSelection: function () {
          if (!pageText) return null;
          var selection = window.getSelection();
          if (!selection || selection.isCollapsed || !selection.rangeCount) return null;
          var range = selection.getRangeAt(0);
          if (!pageText.contains(range.commonAncestorContainer)) return null;
          var quote = (selection.toString() || '').trim();
          if (!quote) return null;
          return {
            quote: quote.slice(0, 6000),
            page: page,
            page_label: pageLabel(currentPageData, page),
            source_url: srcUrl(),
            anchor_text: currentPageData && currentPageData.section_title || ''
          };
        },
        selectionBubble: pageText ? { container: pageText } : null,
        jumpTo: function (note) {
          var p = parseInt(note.page, 10);
          if (p) go(p);
        }
      });
      notesBtn.addEventListener('click', function (e) { e.preventDefault(); panel.toggle(); });
      window.__marxNotesPanel = panel;
    } else if (notesBtn) {
      notesBtn.hidden = true;
    }

    paint();
  }

  /* ---------------- 自动挂载 ---------------- */
  function boot() {
    var home = document.querySelector('[data-mylib-home]');
    if (home) mountHome(home);
    var up = document.querySelector('[data-mylib-upload]');
    if (up) mountUpload(up);
    var rd = document.querySelector('[data-mylib-reader]');
    if (rd) {
      if (window.MarxLibraryReader && window.MarxLibraryReader.mount) {
        window.MarxLibraryReader.mount(rd);
      } else {
        mountReader(rd);  // 新阅读器脚本不可用时保留旧实现作为降级兜底
      }
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }

  window.MarxLibrary = { api: api, uid: UID };
})();
