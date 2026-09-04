/* 个人文库阅读器 AI 导读。
 * 页级讲解只向服务端提交 submission id + 页码，由服务端重新做 owner 校验并装配文字；
 * 接地问答强制 scope=book:mylib:<id>，不允许退回公共语料或其他用户文库。
 */
(function () {
  'use strict';

  var root = document.querySelector('[data-mylib-ai]');
  var readerRoot = document.querySelector('[data-mylib-reader]');
  if (!root || !readerRoot) return;

  var sid = Math.max(0, parseInt(root.getAttribute('data-sid'), 10) || 0);
  var title = root.getAttribute('data-title') || '当前书籍';
  var searchable = root.getAttribute('data-searchable') === '1';
  var aiAccess = root.getAttribute('data-ai-access') === '1';
  var searchAccess = root.getAttribute('data-search-access') === '1';
  var webAccess = root.getAttribute('data-web-access') === '1';
  var csrf = root.getAttribute('data-csrf') || '';
  // 会话按私有书隔离，避免上一本文库的问答历史污染当前导读。
  var AI_THREAD_KEY = 'marx-ai-thread-v2:mylib:' + sid;
  var AI_PROVIDER_KEY = 'marx-ai-provider-v1';

  function readJsonAttr(name, fallback) {
    try { return JSON.parse(root.getAttribute(name) || '') || fallback; }
    catch (_) { return fallback; }
  }

  var runtime = readJsonAttr('data-runtime', {});
  var quota = readJsonAttr('data-quota', {});
  var currentContext = readerRoot._mylibPageContext || null;
  var selectedText = '';
  var streaming = false;
  var messages = loadThread();

  var statusEl = root.querySelector('#mxAiStatus');
  var badgeEl = root.querySelector('#mxAiModelBadge');
  var providerEl = root.querySelector('#mxAiProvider');
  var quotaEl = root.querySelector('#mxAiQuota');
  var quotaBar = root.querySelector('#mxAiQuotaBar');
  var quotaText = root.querySelector('#mxAiQuotaText');
  var selectedEl = root.querySelector('#mxAiSelectedText');
  var explainPageBtn = root.querySelector('#mxAiExplainPage');
  var explainSelectionBtn = root.querySelector('#mxAiExplainSelection');
  var clearSelectionBtn = root.querySelector('#mxAiClearSelection');
  var messagesEl = root.querySelector('#mxAiMessages');
  var promptEl = root.querySelector('#mxAiPrompt');
  var groundEl = root.querySelector('#mxAiGroundBook');
  var clearBtn = root.querySelector('#mxAiClear');
  var sendBtn = root.querySelector('#mxAiSend');
  var usePageTextBtn = document.querySelector('#mxUsePageText');
  var pageTextEl = document.querySelector('#mxPageText');

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function safeUrl(value) {
    try {
      var url = new URL(String(value || ''), window.location.origin);
      if (url.protocol !== 'http:' && url.protocol !== 'https:') return '';
      return url.href;
    } catch (_) { return ''; }
  }

  function inlineMarkdown(value) {
    return String(value || '')
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  }

  function markdown(value) {
    // 先整体转义，再只开放导读需要的少量 Markdown，避免模型输出任意 HTML。
    var lines = esc(value || '').replace(/\r\n?/g, '\n').split('\n');
    var out = [];
    var paragraph = [];
    var listType = '';

    function flushParagraph() {
      if (!paragraph.length) return;
      out.push('<p>' + inlineMarkdown(paragraph.join('<br>')) + '</p>');
      paragraph = [];
    }
    function closeList() {
      if (!listType) return;
      out.push('</' + listType + '>');
      listType = '';
    }
    function openList(type) {
      flushParagraph();
      if (listType === type) return;
      closeList();
      listType = type;
      out.push('<' + type + '>');
    }

    lines.forEach(function (line) {
      var text = line.trim();
      var match;
      if (!text) { flushParagraph(); closeList(); return; }
      if ((match = text.match(/^#{1,3}\s+(.+)$/))) {
        flushParagraph(); closeList();
        out.push('<h3>' + inlineMarkdown(match[1]) + '</h3>');
      } else if ((match = text.match(/^[-*]\s+(.+)$/))) {
        openList('ul'); out.push('<li>' + inlineMarkdown(match[1]) + '</li>');
      } else if ((match = text.match(/^\d+[.)]\s+(.+)$/))) {
        openList('ol'); out.push('<li>' + inlineMarkdown(match[1]) + '</li>');
      } else if ((match = text.match(/^&gt;\s*(.+)$/))) {
        flushParagraph(); closeList();
        out.push('<blockquote>' + inlineMarkdown(match[1]) + '</blockquote>');
      } else {
        closeList(); paragraph.push(text);
      }
    });
    flushParagraph(); closeList();
    return out.join('');
  }

  function loadThread() {
    try {
      var parsed = JSON.parse(localStorage.getItem(AI_THREAD_KEY) || '[]');
      if (!Array.isArray(parsed)) return [];
      return parsed.filter(function (item) {
        return item && !item.pending && (item.role === 'user' || item.role === 'assistant') && typeof item.content === 'string';
      }).slice(-40);
    } catch (_) { return []; }
  }

  function saveThread() {
    try { localStorage.setItem(AI_THREAD_KEY, JSON.stringify(messages.slice(-40))); }
    catch (_) { /* 私密模式或存储已满时仅不跨页保存 */ }
  }

  function renderCitations(items, scope) {
    if (!Array.isArray(items) || !items.length) return '';
    var scopeLabel = scope && scope.label ? ' · ' + esc(scope.label) : '';
    var body = items.map(function (item) {
      var href = safeUrl(item.viewer_url || item.source_url || '');
      var citation = esc(item.citation || item.title || '原文出处');
      var context = esc(item.context || item.snippet || '');
      return '<span class="mx-ai-citation"><b>' + citation + '</b>' +
        (context ? '<br>' + context : '') +
        (href ? '<br><a href="' + esc(href) + '" target="_blank" rel="noopener">打开原文页 →</a>' : '') +
        '</span>';
    }).join('');
    return '<details class="mx-ai-citations"><summary>引用原文（' + items.length + '）' + scopeLabel + '</summary>' + body + '</details>';
  }

  function renderWarnings(items) {
    if (!Array.isArray(items) || !items.length) return '';
    return '<div class="mx-ai-citation">' + items.map(esc).join('<br>') + '</div>';
  }

  function renderMessages() {
    if (!messages.length) {
      var empty = !aiAccess
        ? '当前账号尚未开放 AI 导读。阅读、目录、文字复制和笔记仍可正常使用。'
        : !searchable
          ? '本书尚无 OCR 文字层，暂不能进行可靠的 AI 导读；系统不会改用公共语料冒充本书内容。'
          : '可先选择“解释本页”，或在左侧文字层划选一段文字后解释。自由提问默认只检索当前私有书籍。';
      messagesEl.innerHTML = '<div class="mx-ai-empty">' + esc(empty) + '</div>';
      return;
    }
    messagesEl.innerHTML = messages.map(function (message) {
      if (message.role === 'user') return '<div class="mx-ai-msg user">' + esc(message.content) + '</div>';
      var body = message.pending && !message.content
        ? '<p style="color:#897a6a">' + esc(message.progress || '正在思考…') + '</p>'
        : markdown(message.content);
      return '<div class="mx-ai-msg assistant"><span class="mx-ai-msg-title">AI 导读</span>' +
        '<div class="mx-ai-msg-body">' + body + '</div>' +
        renderWarnings(message.warnings) + renderCitations(message.citations, message.groundingScope) +
        '</div>';
    }).join('');
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function updateQuota(next) {
    if (next) quota = next;
    if (!quota || !Object.keys(quota).length) { quotaEl.hidden = true; return; }
    quotaEl.hidden = false;
    quotaText.textContent = quota.message || (quota.unlimited ? 'AI 额度不限' : 'AI 额度');
    var ratio = quota.unlimited ? 1 : Math.max(0, Math.min(1, Number(quota.ratio) || 0));
    quotaBar.style.width = Math.round(ratio * 100) + '%';
  }

  function currentProvider() {
    return webAccess && providerEl && providerEl.value === 'zhipu' ? 'zhipu' : 'deepseek';
  }

  function refreshAvailability() {
    var enabled = !!(aiAccess && runtime && runtime.enabled && searchable && !streaming);
    var pageReady = !!(enabled && currentContext && currentContext.text);
    explainPageBtn.disabled = !pageReady;
    explainSelectionBtn.disabled = !(pageReady && selectedText);
    clearSelectionBtn.disabled = !selectedText;
    sendBtn.disabled = !enabled;
    promptEl.disabled = !enabled;
    if (usePageTextBtn) usePageTextBtn.disabled = !pageReady;

    if (!aiAccess) {
      badgeEl.textContent = '未开放';
      statusEl.textContent = '当前账号尚未开放 AI 导读。';
    } else if (!searchable) {
      badgeEl.textContent = '等待文字层';
      statusEl.textContent = '本书没有可用文字层，AI 导读已暂停。';
    } else if (!runtime || !runtime.enabled) {
      badgeEl.textContent = '服务暂不可用';
      statusEl.textContent = 'AI 服务暂未配置或正在维护。';
    } else if (!currentContext) {
      badgeEl.textContent = runtime.model || 'AI 导读';
      statusEl.textContent = '正在加载当前页文字…';
    } else {
      var model = currentProvider() === 'zhipu' ? (runtime.zhipu_model || '智谱') : (runtime.model || 'AI 导读');
      badgeEl.textContent = model;
      statusEl.textContent = '已接入 PDF 第 ' + currentContext.page + ' 页；提问范围严格限定当前私有书籍。';
    }
  }

  function renderSelection() {
    if (selectedText) {
      selectedEl.textContent = selectedText;
      selectedEl.classList.add('has-selection');
    } else {
      selectedEl.textContent = '尚未选择文字。可在左侧文字层划选后解释。';
      selectedEl.classList.remove('has-selection');
    }
    refreshAvailability();
  }

  function clearSelection() {
    selectedText = '';
    var selection = window.getSelection();
    if (selection) selection.removeAllRanges();
    renderSelection();
  }

  function captureSelection() {
    if (!pageTextEl) return;
    var selection = window.getSelection();
    if (!selection || selection.isCollapsed || !selection.rangeCount) return;
    var range = selection.getRangeAt(0);
    if (!pageTextEl.contains(range.commonAncestorContainer)) return;
    var text = (selection.toString() || '').trim();
    if (!text) return;
    selectedText = text.slice(0, 6000);
    renderSelection();
  }

  function apiFetch(path, options) {
    options = options || {};
    options.credentials = 'same-origin';
    options.headers = options.headers || {};
    options.headers['X-CSRF-Token'] = csrf;
    return fetch(path, options).then(function (response) {
      if (response.ok) return response;
      return response.text().then(function (raw) {
        var message = '';
        try {
          var data = JSON.parse(raw || '{}');
          message = data.error || data.description || '';
        } catch (_) { /* 非 JSON 网关错误 */ }
        throw new Error(message || '请求失败（' + response.status + '）');
      });
    });
  }

  function historyForRequest() {
    return messages.filter(function (item) { return !item.pending; }).slice(-12).map(function (item) {
      return { role: item.role, content: item.content };
    });
  }

  function parseSseResult(raw) {
    var result = null;
    String(raw || '').split('\n\n').forEach(function (block) {
      var parts = block.split('\n').filter(function (line) { return line.indexOf('data:') === 0; })
        .map(function (line) { return line.slice(5).trim(); });
      if (!parts.length) return;
      try { result = JSON.parse(parts.join('')); } catch (_) { /* 忽略心跳与半包 */ }
    });
    return result;
  }

  function readSseResultStream(response, onProgress) {
    if (!response.body || !response.body.getReader) {
      return response.text().then(function (raw) {
        var fallback = parseSseResult(raw);
        if (!fallback) throw new Error('AI 返回内容不完整。');
        return fallback;
      });
    }
    var reader = response.body.getReader();
    var decoder = new TextDecoder();
    var buffer = '';
    var result = null;
    function consume() {
      return reader.read().then(function (chunk) {
        buffer += decoder.decode(chunk.value || new Uint8Array(), { stream: !chunk.done });
        var blocks = buffer.split('\n\n');
        buffer = chunk.done ? '' : (blocks.pop() || '');
        blocks.forEach(function (block) {
          var lines = block.split('\n');
          var eventLine = lines.find(function (line) { return line.indexOf('event:') === 0; }) || '';
          var eventName = eventLine.slice(6).trim();
          var parts = lines.filter(function (line) { return line.indexOf('data:') === 0; })
            .map(function (line) { return line.slice(5).trim(); });
          if (!parts.length) return;
          var data;
          try { data = JSON.parse(parts.join('')); } catch (_) { return; }
          if (eventName === 'progress') {
            if (onProgress) onProgress(data);
          } else if (eventName === 'error') {
            throw new Error(data.error || 'AI 请求失败。');
          } else if (eventName === 'done' || !eventName) {
            result = data;
          }
        });
        if (chunk.done) {
          if (!result) throw new Error('AI 返回内容不完整。');
          return result;
        }
        return consume();
      });
    }
    return consume();
  }

  function applyAnswerData(answer, data) {
    answer.content = answer.content || data.answer_markdown || '';
    answer.warnings = data.warnings || [];
    answer.citations = data.citations || data.sources || [];
    answer.groundingScope = data.grounding_scope || null;
    if (data.ai_token_quota) updateQuota(data.ai_token_quota);
  }

  function streamPageAnswer(payload, answer) {
    return apiFetch('/api/ai/pdf-chat-stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function (response) {
      if (!response.body || !response.body.getReader) {
        return response.text().then(function (raw) {
          var data = parseSseResult(raw);
          if (!data || data.ok === false) throw new Error(data && data.error || 'AI 返回内容不完整。');
          applyAnswerData(answer, data);
        });
      }
      var reader = response.body.getReader();
      var decoder = new TextDecoder();
      var buffer = '';
      var doneSeen = false;
      function consume() {
        return reader.read().then(function (chunk) {
          if (chunk.done) {
            if (!doneSeen && !answer.content) throw new Error('AI 返回内容不完整。');
            return;
          }
          buffer += decoder.decode(chunk.value, { stream: true });
          var blocks = buffer.split('\n\n');
          buffer = blocks.pop() || '';
          blocks.forEach(function (block) {
            var eventLine = block.split('\n').find(function (line) { return line.indexOf('event:') === 0; }) || '';
            var eventName = eventLine.slice(6).trim();
            var dataLine = block.split('\n').find(function (line) { return line.indexOf('data:') === 0; });
            if (!dataLine) return;
            var data;
            try { data = JSON.parse(dataLine.slice(5).trim()); } catch (_) { return; }
            if (eventName === 'delta') {
              answer.content += data.text || '';
              saveThread(); renderMessages();
            } else if (eventName === 'done') {
              if (data.ok === false) throw new Error(data.error || 'AI 服务繁忙，请稍后重试。');
              doneSeen = true; applyAnswerData(answer, data); saveThread(); renderMessages();
            } else if (eventName === 'error') {
              throw new Error(data.error || 'AI 请求失败。');
            }
          });
          return consume();
        });
      }
      return consume();
    });
  }

  function groundedBookAnswer(question, history, answer) {
    return apiFetch('/api/ai/search-chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        question: question,
        messages: history,
        provider: currentProvider(),
        grounding: true,
        scope: ['book:mylib:' + sid]
      })
    }).then(function (response) {
      var ctype = (response.headers.get('content-type') || '').toLowerCase();
      if (ctype.indexOf('text/event-stream') >= 0) {
        return readSseResultStream(response, function (progress) {
          var elapsed = Number(progress.elapsed_seconds || 0);
          answer.progress = String(progress.message || '正在检索本书原文') + (elapsed ? '（' + elapsed + '秒）' : '');
          renderMessages();
        }).then(function (data) {
          if (!data || data.ok === false) throw new Error(data && data.error || '本书原文检索失败。');
          applyAnswerData(answer, data);
        });
      }
      return response.text().then(function (raw) {
        var data = JSON.parse(raw || '{}');
        if (!data || data.ok === false) throw new Error(data && data.error || '本书原文检索失败。');
        applyAnswerData(answer, data);
      });
    });
  }

  function submit(question, options) {
    options = options || {};
    question = String(question || '').trim();
    if (!question || streaming || !aiAccess || !runtime.enabled || !searchable) return;
    var useGrounding = !!(options.allowGrounding && groundEl && groundEl.checked && searchAccess);
    if (!useGrounding && (!currentContext || !currentContext.text)) return;
    var history = options.freshContext ? [] : historyForRequest();
    messages.push({ role: 'user', content: question });
    var answer = { role: 'assistant', content: '', warnings: [], citations: [], pending: true };
    messages.push(answer);
    streaming = true;
    promptEl.value = '';
    saveThread(); renderMessages(); refreshAvailability();

    var task;
    if (useGrounding) {
      task = groundedBookAnswer(question, history, answer);
    } else {
      task = streamPageAnswer({
        question: question,
        messages: history,
        source_file: '',
        personal_submission_id: sid,
        page: currentContext.page,
        selected_text: options.selectedText || '',
        web_enabled: false,
        quick_mode: !!options.quickMode,
        provider: currentProvider()
      }, answer);
    }
    task.catch(function (error) {
      answer.content = '请求失败：' + (error.message || '未知错误');
      answer.warnings = [];
      answer.citations = [];
    }).then(function () {
      delete answer.pending;
      streaming = false;
      saveThread(); renderMessages(); refreshAvailability();
    });
  }

  function configureProvider() {
    if (!webAccess || !runtime.zhipu_enabled) return;
    providerEl.hidden = false;
    providerEl.innerHTML = '<option value="deepseek">DeepSeek（本页上下文）</option>' +
      '<option value="zhipu">智谱 ' + esc(runtime.zhipu_model || 'GLM') + '（可联网补充）</option>';
    try {
      var saved = localStorage.getItem(AI_PROVIDER_KEY);
      if (saved === 'zhipu' || saved === 'deepseek') providerEl.value = saved;
    } catch (_) { /* 忽略 */ }
    providerEl.addEventListener('change', function () {
      try { localStorage.setItem(AI_PROVIDER_KEY, providerEl.value); } catch (_) { /* 忽略 */ }
      refreshAvailability();
    });
  }

  readerRoot.addEventListener('mylib:page-loading', function () {
    currentContext = null;
    clearSelection();
    refreshAvailability();
  });
  readerRoot.addEventListener('mylib:pagecontext', function (event) {
    currentContext = event.detail || null;
    refreshAvailability();
  });
  readerRoot.addEventListener('mylib:pagecontext-error', function (event) {
    currentContext = null;
    statusEl.textContent = event.detail && event.detail.error || '当前页文字加载失败。';
    refreshAvailability();
  });
  if (pageTextEl) {
    pageTextEl.addEventListener('mouseup', function () { window.setTimeout(captureSelection, 0); });
    pageTextEl.addEventListener('keyup', function () { window.setTimeout(captureSelection, 0); });
  }
  explainPageBtn.addEventListener('click', function () {
    submit('请严格依据本页原文，说明本页主旨、论证脉络、关键概念和原文依据。', {
      quickMode: true, freshContext: true
    });
  });
  explainSelectionBtn.addEventListener('click', function () {
    if (selectedText) submit('请逐句解释我选择的文字，并结合本页上下文说明其论证作用和原文依据。', {
      selectedText: selectedText, quickMode: true, freshContext: true
    });
  });
  clearSelectionBtn.addEventListener('click', clearSelection);
  if (usePageTextBtn) usePageTextBtn.addEventListener('click', function () {
    submit('请严格依据本页原文，说明本页主旨、论证脉络、关键概念和原文依据。', {
      quickMode: true, freshContext: true
    });
  });
  sendBtn.addEventListener('click', function () { submit(promptEl.value, { allowGrounding: true }); });
  promptEl.addEventListener('keydown', function (event) {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
      event.preventDefault(); submit(promptEl.value, { allowGrounding: true });
    }
  });
  clearBtn.addEventListener('click', function () {
    if (streaming) return;
    messages = []; saveThread(); renderMessages();
  });
  window.addEventListener('storage', function (event) {
    if (event.key !== AI_THREAD_KEY || streaming) return;
    messages = loadThread(); renderMessages();
  });

  configureProvider();
  updateQuota();
  renderSelection();
  renderMessages();
})();
