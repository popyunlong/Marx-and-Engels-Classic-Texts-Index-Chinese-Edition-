/* 个人文库 PDF 阅读器。
 * 界面与站内主阅读器保持同构，数据始终通过 owner 校验后的 /api/mylib/* 私有接口读取。
 */
(function () {
  'use strict';

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"]/g, function (char) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[char];
    });
  }

  function mount(root) {
    var library = window.MarxLibrary || {};
    var api = library.api;
    if (typeof api !== 'function') return;

    var sid = parseInt(root.getAttribute('data-sid'), 10);
    var total = parseInt(root.getAttribute('data-pages'), 10) || 1;
    var title = root.getAttribute('data-title') || '';
    var img = root.querySelector('#mxPage');
    var imageWrap = root.querySelector('#mxPageWrap');
    var imageError = root.querySelector('#mxImageError');
    var label = root.querySelector('#mxPageLabel');
    var currentPageEl = root.querySelector('#mxCurrentPage');
    var currentSectionEl = root.querySelector('#mxCurrentSection');
    var prev = root.querySelector('#mxPrev');
    var next = root.querySelector('#mxNext');
    var tocPanel = root.querySelector('#mxTocPanel');
    var tocEl = root.querySelector('#mxToc');
    var tocCount = root.querySelector('#mxTocCount');
    var findToggle = root.querySelector('#mxFindToggle');
    var readerFind = root.querySelector('#mxReaderFind');
    var searchInput = root.querySelector('#mxBookSearchInput');
    var searchStatus = root.querySelector('#mxBookSearchStatus');
    var searchResults = root.querySelector('#mxBookSearchResults');
    var findPrev = root.querySelector('#mxFindPrevBtn');
    var findNext = root.querySelector('#mxFindNextBtn');
    var findClose = root.querySelector('#mxFindCloseBtn');
    var pageText = root.querySelector('#mxPageText');
    var pageTextMeta = root.querySelector('#mxPageTextMeta');
    var pageCite = root.querySelector('#mxPageCite');
    var pageCitation = root.querySelector('#mxPageCitation');
    var copyCitation = root.querySelector('#mxCopyCitation');
    var copyPageText = root.querySelector('#mxCopyPageText');
    var reportPage = root.querySelector('#mxReportPage');
    var imageRequest = 0;
    var pageTextRequest = 0;
    var currentPageData = null;
    var findMatches = [];
    var findIndex = -1;
    var findSeq = 0;
    var params = new URLSearchParams(window.location.search);
    // 导出汇编的“打开原文页”会携带安全编码后的命中词；只用于本页
    // 已经通过 owner 校验的文字层高亮，不触发额外检索或跨书访问。
    var findHighlight = String(params.get('h') || params.get('q') || '').slice(0, 500);
    var page = Math.min(Math.max(parseInt(params.get('page'), 10) || 1, 1), total);

    function pageLabel(data, fallbackPage) {
      return data && data.printed_page ? '中文版第 ' + data.printed_page + ' 页' : 'PDF 第 ' + fallbackPage + ' 页';
    }

    function copyText(value, button, doneLabel) {
      if (!value) return;
      var before = button ? button.textContent : '';
      var finished = function () {
        if (!button) return;
        button.textContent = doneLabel || '已复制';
        window.setTimeout(function () { button.textContent = before; }, 1200);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(value).then(finished).catch(function () {});
        return;
      }
      var area = document.createElement('textarea');
      area.value = value;
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      try { document.execCommand('copy'); finished(); } catch (error) { /* 旧浏览器静默降级 */ }
      area.remove();
    }

    function queryTerms(value) {
      return String(value || '').trim().split(/\s+/).filter(function (term) { return term.length > 0; })
        .sort(function (a, b) { return b.length - a.length; });
    }

    function highlightPlain(value, query) {
      var terms = queryTerms(query);
      if (!terms.length) return esc(value || '');
      var pattern = terms.map(function (term) {
        return term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      }).join('|');
      try {
        return esc(value || '').replace(new RegExp('(' + pattern + ')', 'gi'), '<mark>$1</mark>');
      } catch (error) {
        return esc(value || '');
      }
    }

    function searchContext(value) {
      return esc(value || '')
        .replace(/\[\[H\]\]/g, '<mark>')
        .replace(/\[\[\/H\]\]/g, '</mark>');
    }

    function updatePageMeta(data, requestedPage) {
      var readable = pageLabel(data, requestedPage);
      if (currentPageEl) currentPageEl.textContent = readable;
      if (currentSectionEl) currentSectionEl.textContent = data && data.section_title || '未识别';
      label.textContent = readable + ' · PDF 第 ' + requestedPage + ' / ' + total + ' 页';
      if (pageTextMeta) {
        pageTextMeta.textContent = '与上方同为 PDF 第 ' + requestedPage + ' 页 · ' + readable +
          (data && data.section_title ? ' · ' + data.section_title : '') + ' · 可选择复制';
      }
      if (pageCitation && data && data.citation) {
        pageCitation.textContent = data.citation;
        if (pageCite) pageCite.hidden = false;
      } else if (pageCite) {
        pageCite.hidden = true;
      }
    }

    function renderPageText() {
      if (!pageText || !currentPageData) return;
      var value = currentPageData.text || '本页未识别到文字，可继续查看上方原始页面。';
      pageText.innerHTML = highlightPlain(value, findHighlight);
    }

    function loadPageText() {
      if (!pageText) return;
      var requestedPage = page;
      var requestId = ++pageTextRequest;
      currentPageData = null;
      root._mylibPageContext = null;
      root.dispatchEvent(new CustomEvent('mylib:page-loading', {
        detail: { page: requestedPage }
      }));
      pageText.textContent = '正在加载当前页文本…';
      if (pageTextMeta) pageTextMeta.textContent = '与上方 PDF 第 ' + requestedPage + ' 页对应';
      if (pageCite) pageCite.hidden = true;
      api('/api/mylib/' + sid + '/page-text?page=' + requestedPage).then(function (data) {
        if (requestId !== pageTextRequest || requestedPage !== page) return;
        currentPageData = data;
        root._mylibPageContext = {
          page: requestedPage,
          data: data,
          text: data.text || '',
          citation: data.citation || '',
          section_title: data.section_title || ''
        };
        updatePageMeta(data, requestedPage);
        renderPageText();
        root.dispatchEvent(new CustomEvent('mylib:pagecontext', {
          detail: root._mylibPageContext
        }));
      }).catch(function (error) {
        if (requestId !== pageTextRequest || requestedPage !== page) return;
        pageText.textContent = error.message || '本页文字加载失败，请稍后重试。';
        if (pageTextMeta) pageTextMeta.textContent = '文字层暂不可用';
        root.dispatchEvent(new CustomEvent('mylib:pagecontext-error', {
          detail: { page: requestedPage, error: error.message || '本页文字加载失败' }
        }));
      });
    }

    function prefetchNeighbors() {
      [page + 1, page - 1].forEach(function (candidate) {
        if (candidate < 1 || candidate > total) return;
        var preload = new Image();
        preload.src = '/api/mylib/page-image?sid=' + sid + '&page=' + candidate;
      });
    }

    function syncActiveToc() {
      if (!tocEl) return;
      tocEl.querySelectorAll('.toc-link').forEach(function (button) {
        button.classList.toggle('active', parseInt(button.getAttribute('data-page'), 10) === page);
      });
    }

    function loadPageImage(requestedPage) {
      var requestId = ++imageRequest;
      var source = '/api/mylib/page-image?sid=' + sid + '&page=' + requestedPage;
      var loader = new Image();
      // 新图确认到达前隐藏旧图，杜绝“旧页图 + 新页文字”短暂并存。
      img.hidden = true;
      loader.decoding = 'async';
      loader.onload = function () {
        // 快速连续翻页时，旧页图可能晚于新页返回；只允许当前请求提交到可见 <img>。
        if (requestId !== imageRequest || requestedPage !== page) return;
        img.src = source;
        img.setAttribute('data-pdf-page', String(requestedPage));
        img.hidden = false;
        if (imageWrap) imageWrap.classList.remove('page-image-loading');
        if (imageError) imageError.classList.remove('show');
        window.setTimeout(prefetchNeighbors, 80);
      };
      loader.onerror = function () {
        if (requestId !== imageRequest || requestedPage !== page) return;
        img.hidden = true;
        if (imageWrap) imageWrap.classList.remove('page-image-loading');
        if (imageError) {
          imageError.textContent = 'PDF 第 ' + requestedPage + ' 页加载失败，请稍后重试。';
          imageError.classList.add('show');
        }
      };
      loader.src = source;
    }

    function paint() {
      if (imageWrap) imageWrap.classList.add('page-image-loading');
      if (imageError) { imageError.classList.remove('show'); imageError.textContent = ''; }
      loadPageImage(page);
      label.textContent = 'PDF 第 ' + page + ' / ' + total + ' 页';
      if (currentPageEl) currentPageEl.textContent = 'PDF 第 ' + page + ' 页';
      prev.disabled = page <= 1;
      next.disabled = page >= total;
      if (reportPage) {
        reportPage.disabled = false;
        reportPage.classList.remove('done');
        reportPage.textContent = '⚑ 一键报错';
      }
      loadPageText();
      try {
        var url = new URL(window.location.href);
        url.searchParams.set('page', page);
        window.history.replaceState(null, '', url.toString());
      } catch (error) { /* 老浏览器忽略 */ }
      if (window.MarxReadingHistory) {
        window.MarxReadingHistory.record({
          book: 'mylib:' + sid,
          kind: 'mylib',
          title: title,
          subtitle: 'PDF 第 ' + page + ' 页',
          url: '/mylib/' + sid + '?page=' + page
        });
      }
      syncActiveToc();
    }

    function go(value) {
      var target = Math.min(Math.max(parseInt(value, 10) || 1, 1), total);
      if (target === page && currentPageData) return;
      page = target;
      paint();
    }

    function openFind() {
      if (!readerFind) return;
      readerFind.hidden = false;
      if (searchInput) { searchInput.focus(); searchInput.select(); }
    }

    function closeFind() {
      if (readerFind) readerFind.hidden = true;
      findHighlight = '';
      findMatches = [];
      findIndex = -1;
      if (searchResults) searchResults.innerHTML = '';
      if (searchStatus) searchStatus.textContent = '';
      renderPageText();
    }

    function renderFindResults(hits) {
      if (!searchResults) return;
      if (!hits.length) {
        searchResults.innerHTML = '<div class="reader-find-empty">本书中未找到该词句。</div>';
        return;
      }
      searchResults.innerHTML = hits.map(function (hit, index) {
        var hitPage = parseInt((hit.pdf_pages || [1])[0], 10) || 1;
        var printed = (hit.printed_pages || [])[0];
        var readable = printed ? '第 ' + esc(printed) + ' 页' : 'PDF 第 ' + hitPage + ' 页';
        var section = hit.section_title ? ' · ' + esc(hit.section_title) : '';
        return '<div class="find-hit-wrap">' +
          '<button type="button" class="find-hit" data-find-index="' + index + '" data-search-page="' + hitPage + '">' +
          '<span class="find-hit-page">' + readable + '</span>' +
          '<span class="find-hit-snip">' + searchContext(hit.context) + section + '</span></button>' +
          (hit.citation ? '<div class="find-hit-cite"><span id="mxSearchCite' + index + '">' + esc(hit.citation) +
            '</span><button type="button" class="find-copy-cite" data-copy-cite="' + index + '">复制引文</button></div>' : '') +
          '</div>';
      }).join('');
    }

    function runFind() {
      if (!searchInput || !searchResults) return;
      var query = (searchInput.value || '').trim();
      findHighlight = query;
      if (query.replace(/\s+/g, '').length < 2) {
        findMatches = [];
        findIndex = -1;
        searchResults.innerHTML = '';
        searchStatus.textContent = query ? '请至少输入两个有效字符' : '';
        renderPageText();
        return;
      }
      var seq = ++findSeq;
      searchStatus.textContent = '查找中…';
      api('/api/mylib/' + sid + '/search?q=' + encodeURIComponent(query)).then(function (data) {
        if (seq !== findSeq) return;
        findMatches = Array.isArray(data.results) ? data.results : [];
        findIndex = -1;
        searchStatus.textContent = findMatches.length ? '找到 ' + findMatches.length + ' 处' : '0 处';
        renderFindResults(findMatches);
        renderPageText();
      }).catch(function (error) {
        if (seq !== findSeq) return;
        findMatches = [];
        searchResults.innerHTML = '';
        searchStatus.textContent = error.message || '查找失败';
      });
    }

    function gotoFindMatch(index) {
      if (!findMatches.length) return;
      findIndex = ((index % findMatches.length) + findMatches.length) % findMatches.length;
      var hit = findMatches[findIndex];
      var hitPage = parseInt((hit.pdf_pages || [1])[0], 10) || 1;
      if (searchResults) {
        searchResults.querySelectorAll('.find-hit').forEach(function (button, buttonIndex) {
          button.classList.toggle('active', buttonIndex === findIndex);
        });
        var active = searchResults.querySelector('.find-hit.active');
        if (active) active.scrollIntoView({ block: 'nearest' });
      }
      go(hitPage);
    }

    prev.addEventListener('click', function () { if (page > 1) go(page - 1); });
    next.addEventListener('click', function () { if (page < total) go(page + 1); });
    document.addEventListener('keydown', function (event) {
      if ((event.ctrlKey || event.metaKey) && (event.key === 'f' || event.key === 'F') && readerFind) {
        event.preventDefault();
        openFind();
        return;
      }
      if (event.target && (/input|textarea/i.test(event.target.tagName) || event.target.isContentEditable)) return;
      if (event.key === 'ArrowLeft' || event.key === 'PageUp') {
        event.preventDefault();
        if (page > 1) go(page - 1);
      }
      if (event.key === 'ArrowRight' || event.key === 'PageDown') {
        event.preventDefault();
        if (page < total) go(page + 1);
      }
      if (event.key === 'Escape' && tocPanel && tocPanel.open) tocPanel.open = false;
    });

    if (findToggle) findToggle.addEventListener('click', function () {
      if (readerFind && readerFind.hidden) openFind(); else closeFind();
    });
    if (findClose) findClose.addEventListener('click', closeFind);
    if (findNext) findNext.addEventListener('click', function () { gotoFindMatch(findIndex + 1); });
    if (findPrev) findPrev.addEventListener('click', function () { gotoFindMatch(findIndex - 1); });
    if (searchInput) {
      var findTimer;
      searchInput.addEventListener('input', function () {
        window.clearTimeout(findTimer);
        findTimer = window.setTimeout(runFind, 280);
      });
      searchInput.addEventListener('keydown', function (event) {
        if (event.key === 'Enter') {
          event.preventDefault();
          if (findMatches.length) gotoFindMatch(event.shiftKey ? findIndex - 1 : findIndex + 1);
          else runFind();
        } else if (event.key === 'Escape') {
          event.preventDefault();
          closeFind();
        }
      });
    }
    if (searchResults) {
      searchResults.addEventListener('click', function (event) {
        var copy = event.target.closest && event.target.closest('[data-copy-cite]');
        if (copy) {
          var cite = root.querySelector('#mxSearchCite' + copy.getAttribute('data-copy-cite'));
          copyText(cite && cite.textContent, copy, '已复制');
          return;
        }
        var hit = event.target.closest && event.target.closest('[data-find-index]');
        if (hit) gotoFindMatch(parseInt(hit.getAttribute('data-find-index'), 10) || 0);
      });
    }

    if (copyPageText) copyPageText.addEventListener('click', function () {
      copyText(currentPageData && currentPageData.text, copyPageText, '已复制本页');
    });
    if (copyCitation) copyCitation.addEventListener('click', function () {
      copyText(currentPageData && currentPageData.citation, copyCitation, '已复制引文');
    });
    if (reportPage) reportPage.addEventListener('click', function () {
      if (reportPage.disabled || reportPage.classList.contains('done')) return;
      reportPage.disabled = true;
      reportPage.textContent = '提交中…';
      api('/api/reader/report-page-error', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          reader: 'mylib',
          book: title,
          volume: '',
          page: pageLabel(currentPageData, page),
          source_ref: 'mylib:' + sid,
          citation: currentPageData && currentPageData.citation || ''
        })
      }).then(function () {
        reportPage.textContent = '✓ 已上报，感谢';
        reportPage.classList.add('done');
      }).catch(function () {
        reportPage.disabled = false;
        reportPage.textContent = '提交失败，请重试';
        window.setTimeout(function () {
          if (!reportPage.classList.contains('done')) reportPage.textContent = '⚑ 一键报错';
        }, 1800);
      });
    });

    if (tocEl) {
      api('/api/mylib/' + sid + '/toc').then(function (data) {
        var entries = Array.isArray(data.entries) ? data.entries : [];
        if (tocCount) tocCount.textContent = entries.length ? '· ' + entries.length + ' 条' : '';
        if (!entries.length) {
          tocEl.innerHTML = '<li class="toc-empty">本书没有通过质量校验的可靠目录。</li>';
          return;
        }
        tocEl.innerHTML = entries.map(function (entry) {
          var tocPage = parseInt(entry.pdf_page, 10) || 1;
          var level = Math.min(Math.max(parseInt(entry.level, 10) || 1, 1), 4);
          var readable = entry.printed_page ? '第 ' + esc(entry.printed_page) + ' 页' : 'PDF ' + tocPage;
          return '<li><button type="button" class="toc-link level-' + level + '" data-page="' + tocPage +
            '" data-title="' + esc(entry.title) + '"><span>' + esc(entry.title) + '</span><small>' +
            readable + '</small></button></li>';
        }).join('');
        syncActiveToc();
      }).catch(function () {
        tocEl.innerHTML = '<li class="toc-empty">目录暂时无法加载，请稍后重试。</li>';
      });
      tocEl.addEventListener('click', function (event) {
        var button = event.target.closest && event.target.closest('.toc-link');
        if (!button) return;
        if (currentSectionEl) currentSectionEl.textContent = button.getAttribute('data-title') || '未识别';
        go(parseInt(button.getAttribute('data-page'), 10) || 1);
        if (tocPanel) tocPanel.open = false;
      });
    }
    document.addEventListener('click', function (event) {
      root.querySelectorAll('.toc-popover[open]').forEach(function (details) {
        if (!details.contains(event.target)) details.open = false;
      });
    });

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
          var notePage = parseInt(note.page, 10);
          if (notePage) go(notePage);
        }
      });
      notesBtn.addEventListener('click', function (event) { event.preventDefault(); panel.toggle(); });
      window.__marxNotesPanel = panel;
    } else if (notesBtn) {
      notesBtn.hidden = true;
    }

    paint();
  }

  window.MarxLibraryReader = { mount: mount };
})();
