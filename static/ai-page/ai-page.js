/* 「AI 研究对话」全屏页 —— 自包含脚本（命名空间 aip*，与全站抽屉零冲突）。
 *
 * 浏览器 IndexedDB 保存本机副本；会员明确勾选后同步到独立的个人文库服务器。
 * 每条提问可选两档深度：
 *   · 快速问答 → /api/ai/search-chat（多轮、可选检索引文库接地）；扣「随心问」token 额度。
 *   · 研究综述 → /api/search/associative?mode=research（一次性 5000 字以上长文 + 最多 30 条按需引文）；
 *     扣「研究型检索」每周次数额度。综述作为一条 assistant 气泡内联进会话，后续追问带它做上下文。
 * 对话/Markdown/引文渲染逻辑与抽屉一致（此处为自包含拷贝，避免改动全站抽屉带来回归）。
 */
(function () {
  "use strict";
  var root = document.getElementById("aip-root");
  if (!root) return;

  var CSRF = root.getAttribute("data-csrf") || "";
  var $ = function (sel) { return root.querySelector(sel); };

  // 元素
  var modelBadge = $("#aipModelBadge");
  var lockEl = $("#aipLock");
  var controlsEl = $("#aipControls");
  var depthTabs = Array.prototype.slice.call(root.querySelectorAll(".aip-depth-tab"));
  var providerRow = $("#aipProviderRow");
  var modelSelect = $("#aipModel");
  var zhipuOpt = $("#aipModelZhipuOpt");
  var groundingRow = $("#aipGroundingRow");
  var groundingToggle = $("#aipGrounding");
  var scopeRow = $("#aipScopeRow");
  var scopeChips = $("#aipScopeChips");
  var bookScopeMount = $("#aipBookScopeMount");
  var bookScopeCtl = null;
  var storageWarningEl = $("#aipStorageWarning");
  var researchNote = $("#aipResearchNote");
  var researchQuotaEl = $("#aipResearchQuota");
  var tokenQuotaWrap = $("#aipTokenQuota");
  var tokenQuotaFill = $("#aipTokenQuotaFill");
  var tokenQuotaText = $("#aipTokenQuotaText");
  var messagesEl = $("#aipMessages");
  var composerEl = $("#aipComposer");
  var promptEl = $("#aipPrompt");
  var sendBtn = $("#aipSend");
  var stopBtn = $("#aipStop");
  var clearBtn = $("#aipClear");
  // 本地会话记录侧栏
  var sessionsEl = $("#aipSessions");
  var sessionsListEl = $("#aipSessionsList");
  var newChatBtn = $("#aipNewChat");
  var sessionsToggle = $("#aipSessionsToggle");
  var sessionsClose = $("#aipSessionsClose");
  var sessionsScrim = $("#aipSessionsScrim");
  var cloudConsentRow = $("#aipCloudConsentRow");
  var cloudConsentEl = $("#aipCloudConsent");
  var cloudConsentHint = $("#aipCloudConsentHint");
  var cloudPrivacyEl = $("#aipCloudPrivacy");
  var cloudStatusEl = $("#aipCloudStatus");
  var storageUsageEl = $("#aipStorageUsage");
  var storageUsageText = $("#aipStorageUsageText");
  var storageUsageFill = $("#aipStorageUsageFill");
  var cleanupDateEl = $("#aipCleanupDate");
  var cleanupOldBtn = $("#aipCleanupOld");
  var exportStartEl = $("#aipExportStart");
  var exportEndEl = $("#aipExportEnd");
  var exportRangeBtn = $("#aipExportRange");
  var sessionsFootEl = $("#aipSessionsFoot");

  // 共用记忆键（与抽屉一致，体验连贯）
  var AI_THREAD_KEY = "marx-ai-thread-v1";
  var AI_SESSIONS_KEY = "marx-ai-sessions-v2";   // 多会话记录，按账号 uid 隔离
  var AI_DB_NAME = "marx-ai-conversations-v1";   // v3 主存储：IndexedDB，每条会话独立写入
  var AI_DB_VERSION = 1;
  var AI_DB_SESSION_STORE = "sessions";
  var AI_DB_META_STORE = "meta";
  var AI_SYNC_KEY = "marx-ai-sessions-sync-v3";  // Safari 等无 BroadcastChannel 时的轻量通知
  var AI_BACKUP_INDEX_KEY = "marx-ai-session-backups-v4"; // 同步写入的正文应急副本索引
  var AI_BACKUP_PREFIX = "marx-ai-session-backup-v4:";
  var AI_CLOUD_DELETE_OUTBOX_KEY = "marx-ai-cloud-delete-outbox-v1";
  var AI_GROUNDING_KEY = "marx-ai-grounding-v1";
  var AI_SCOPE_KEY = "marx-ai-scope-v2";
  var AI_DEPTH_KEY = "marx-ai-depth-v1";       // 本页专属
  var CITE_FORMAT_KEY = "marx-citation-format-v1";
  var CITE_FORMAT_LABELS = { gb2015: 1, zgshkx: 1, mkszyj: 1 };

  var HISTORY_CHAR_CAP = 4000;   // 送入「快速问答」历史的单条上限（研究综述很长，防 token 暴涨）

  var hasChatAttr = root.getAttribute("data-chat-access") === "1";
  var hasResearchAttr = root.getAttribute("data-research-access") === "1";

  var aiUid = (root.getAttribute("data-uid") || "").trim();   // 会话记录按此隔离；游客为空串
  var sessionsData = { current: null, sessions: [] };          // 本 uid 的会话集
  var sessionDb = null;
  var sessionDbFailed = false;
  var sessionWriteQueue = Promise.resolve();
  var sessionRefreshTimer = null;
  var sessionRefreshPending = false;
  var sessionMessageSeq = 0;
  var sessionTabId = "t" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
  var sessionSyncChannel = null;
  var sessionsReady = false;
  var sessionsLoadPromise = null;
  var cloudSync = {
    loaded: false, available: false, eligible: false, enabled: false, busy: false,
    retentionDays: 30, warningDays: 5, recoveryDays: 7, serverNow: 0,
    canCreate: false, canUpdateExisting: false, membershipExpired: false,
    graceActive: false, graceUntil: 0,
  };
  var cloudWriteQueue = Promise.resolve();
  var config = null;
  var configLoading = false;
  var messages = [];            // [{role, content, sources, citations, warnings, groundingScope, kind, pending}]
  var aiTokenQuota = {};
  var aiCredits = { chat: 0, research: 0 };
  var researchQuota = {};
  var streaming = false;
  var currentAbort = null;      // 当前在飞请求的 AbortController；「停止回答」即中断它
  var depth = "quick";

  // ===================== 工具 =====================
  function esc(s) {
    return String(s).replace(/[&<>]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]; });
  }
  function escAttr(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function apiFetch(url, options) {
    options = options || {};
    var headers = Object.assign({}, options.headers || {});
    if (CSRF) headers["X-CSRF-Token"] = CSRF;
    return fetch(url, Object.assign({}, options, { headers: headers, credentials: "same-origin" }));
  }
  function parseSseResult(raw) {
    var result = null;
    var blocks = String(raw || "").split("\n\n");
    for (var bi = 0; bi < blocks.length; bi++) {
      var lines = blocks[bi].split("\n");
      var dataParts = [];
      for (var li = 0; li < lines.length; li++) {
        if (lines[li].indexOf("data:") === 0) dataParts.push(lines[li].slice(5).trim());
      }
      if (!dataParts.length) continue;
      try { result = JSON.parse(dataParts.join("")); } catch (_) {}
    }
    return result;
  }
  async function readSseResultStream(resp, onProgress) {
    if (!resp.body) {
      var fallback = parseSseResult(await resp.text());
      if (!fallback) throw new Error("回答生成超时或服务繁忙，请稍后重试。");
      return fallback;
    }
    var reader = resp.body.getReader();
    var decoder = new TextDecoder();
    var buffer = "";
    var result = null;
    while (true) {
      var chunk = await reader.read();
      buffer += decoder.decode(chunk.value || new Uint8Array(), { stream: !chunk.done });
      var blocks = buffer.split("\n\n");
      buffer = chunk.done ? "" : (blocks.pop() || "");
      for (var bi = 0; bi < blocks.length; bi++) {
        var lines = blocks[bi].split("\n");
        var eventName = "";
        var dataParts = [];
        for (var li = 0; li < lines.length; li++) {
          if (lines[li].indexOf("event:") === 0) eventName = lines[li].slice(6).trim();
          if (lines[li].indexOf("data:") === 0) dataParts.push(lines[li].slice(5).trim());
        }
        if (!dataParts.length) continue;
        var data = null;
        try { data = JSON.parse(dataParts.join("")); } catch (_) { continue; }
        if (eventName === "progress") {
          if (onProgress) onProgress(data);
        } else if (eventName === "error") {
          throw new Error(data.error || "AI 请求失败");
        } else if (eventName === "done" || !eventName) {
          result = data;
        }
      }
      if (chunk.done) break;
    }
    if (!result) throw new Error("回答生成超时或服务繁忙，请稍后重试。");
    return result;
  }
  function parseJsonResponse(resp) {
    return resp.text().then(function (raw) {
      var text = String(raw || "").trim();
      try {
        return JSON.parse(text);
      } catch (_) {
        var isHtml = /^<!doctype\b/i.test(text) || /^<html\b/i.test(text);
        if (isHtml || [520, 521, 522, 523, 524].indexOf(resp.status) >= 0) {
          throw new Error("服务器响应超时，请稍后重试；本次未生成内容。");
        }
        throw new Error(resp.ok ? "服务器返回格式异常，请稍后重试。" : ("请求失败（HTTP " + resp.status + "），请稍后重试。"));
      }
    });
  }

  // ===================== 引文格式 =====================
  function citeFormat() {
    try { var v = localStorage.getItem(CITE_FORMAT_KEY); return CITE_FORMAT_LABELS[v] ? v : "gb2015"; }
    catch (_) { return "gb2015"; }
  }
  function pickCite(obj) {
    var m = obj && obj.citations;
    var f = citeFormat();
    if (m && m[f]) return m[f];
    return (obj && obj.citation) || "";
  }
  // 引用格式可选（与检索页 citeFormatBar 同一套共享键 marx-citation-format-v1）：
  // 把多格式出处串序列化进 data-citations，切换时就地重写文本（保留展开态、无需整树重渲染）。
  var CITE_FORMAT_OPTIONS = [
    ["gb2015", "国标 GB/T 7714—2015"],
    ["zgshkx", "《中国社会科学》"],
    ["mkszyj", "《马克思主义研究》"],
  ];
  function citeDataAttr(obj) {
    var m = obj && obj.citations;
    if ((!m || !Object.keys(m).length) && obj && obj.citation) {
      m = { gb2015: obj.citation, zgshkx: obj.citation, mkszyj: obj.citation };
    }
    return m ? escAttr(JSON.stringify(m)) : "";
  }
  function citeFormatSelectHtml() {
    var cur = citeFormat();
    return '<label class="aip-cite-fmt"><span class="aip-cite-fmt-label">引用格式</span>' +
      '<select class="aip-cite-fmt-select" aria-label="选择引用格式">' +
      CITE_FORMAT_OPTIONS.map(function (o) {
        return '<option value="' + o[0] + '"' + (o[0] === cur ? " selected" : "") + ">" + esc(o[1]) + "</option>";
      }).join("") + "</select></label>";
  }
  function applyCiteFormat(fmt) {
    if (!CITE_FORMAT_LABELS[fmt]) return;
    try { localStorage.setItem(CITE_FORMAT_KEY, fmt); } catch (_) {}
    if (!messagesEl) return;
    var nodes = messagesEl.querySelectorAll("[data-citations]");
    for (var i = 0; i < nodes.length; i++) {
      var m; try { m = JSON.parse(nodes[i].getAttribute("data-citations") || "{}"); } catch (_) { m = {}; }
      if (m && m[fmt]) nodes[i].textContent = m[fmt];
    }
    var sels = messagesEl.querySelectorAll(".aip-cite-fmt-select");
    for (var j = 0; j < sels.length; j++) sels[j].value = fmt;
  }

  // ===================== Markdown 渲染（与抽屉一致的拷贝） =====================
  function safeMarkdownUrl(url) {
    var raw = String(url || "").trim();
    if (/^(https?:\/\/|mailto:|\/|#)/i.test(raw)) return raw;
    return "";
  }
  function renderMarkdownLink(label, url, stash) {
    var safeUrl = safeMarkdownUrl(url);
    var safeLabel = esc(label || url || "");
    if (!safeUrl) return safeLabel;
    var isExternal = /^(https?:\/\/|mailto:)/i.test(safeUrl);
    var target = isExternal ? ' target="_blank" rel="noopener noreferrer"' : "";
    return stash('<a href="' + escAttr(safeUrl) + '"' + target + ">" + safeLabel + "</a>");
  }
  function renderInlineMarkdown(text) {
    var placeholders = [];
    var stash = function (html) {
      var key = " MD" + placeholders.length + " ";
      placeholders.push(html);
      return key;
    };
    var source = String(text || "");
    source = source
      .replace(/`([^`]+?)`/g, function (_, code) { return stash("<code>" + esc(code) + "</code>"); })
      .replace(/!\[([^\]\n]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g, function (_, label, url) { return renderMarkdownLink(label || "图片链接", url, stash); })
      .replace(/\[([^\]\n]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g, function (_, label, url) { return renderMarkdownLink(label, url, stash); })
      .replace(/<((?:https?:\/\/|mailto:)[^>\s]+)>/gi, function (_, url) { return renderMarkdownLink(url, url, stash); })
      .replace(/(^|[\s（(])((?:https?:\/\/)[^\s<>()]+[^\s<>().,;:!?，。；：！？）)])/gi, function (_, prefix, url) { return prefix + renderMarkdownLink(url, url, stash); });
    return esc(source)
      .replace(/~~(.+?)~~/g, "<del>$1</del>")
      .replace(/\*\*([^*]+?)\*\*/g, "<strong>$1</strong>")
      .replace(/__([^_]+?)__/g, "<strong>$1</strong>")
      .replace(/\*\*/g, "")  // 去掉未成对残留的 **（模型偶发不闭合），避免字面显示 **
      .replace(/(^|[\s（(])\*([^*\n]+?)\*(?=$|[\s，。；：、,.!?;:)）])/g, "$1<em>$2</em>")
      .replace(/(^|[\s（(])_([^_\n]+?)_(?=$|[\s，。；：、,.!?;:)）])/g, "$1<em>$2</em>")
      .replace(/ MD(\d+) /g, function (_, index) { return placeholders[Number(index)] || ""; });
  }
  function flushMarkdownParagraph(lines, html) {
    if (!lines.length) return;
    // 分节标题修正：模型（尤其 flash）常把「一、xxx」这类小节标题写成独立的一整行**加粗**，
    // 而非 markdown ### 标题，导致标题不突出。这里把「整行仅为一条加粗、且长度像标题」的段落
    // 提升为 <h3>，使其获得与 ### 一致的醒目版式（左红条 + 底色）。正文中的行内加粗不受影响。
    if (lines.length === 1) {
      var solo = String(lines[0]).trim();
      var mh = solo.match(/^\*\*(.+?)\*\*[：:]?$/);
      if (mh && mh[1].length <= 42 && mh[1].indexOf("**") < 0) {
        var htext = mh[1].replace(/^#{1,6}\s*/, "");  // 剥掉叠加的 ### 前缀，避免标题里残留字面 #
        html.push("<h3>" + renderInlineMarkdown(htext) + "</h3>");
        lines.length = 0;
        return;
      }
    }
    html.push("<p>" + lines.map(renderInlineMarkdown).join("<br>") + "</p>");
    lines.length = 0;
  }
  function renderMarkdownListItem(item) {
    var task = String(item || "").match(/^\[([ xX])\]\s+(.+)$/);
    if (task) {
      var checked = task[1].toLowerCase() === "x" ? " checked" : "";
      return '<li class="task-list-item"><input class="task-checkbox" type="checkbox" disabled' + checked + ">" + renderInlineMarkdown(task[2]) + "</li>";
    }
    return "<li>" + renderInlineMarkdown(item) + "</li>";
  }
  function flushMarkdownList(list, html) {
    if (!list.items.length) return;
    var tag = list.type === "ol" ? "ol" : "ul";
    html.push("<" + tag + ">" + list.items.map(renderMarkdownListItem).join("") + "</" + tag + ">");
    list.items = [];
    list.type = null;
  }
  function flushMarkdownQuote(quote, html) {
    if (!quote.length) return;
    // 引用块内原文一律不加粗：引用块本身已有醒目版式；模型有时给引文里部分词句加粗、且 ** 常不成对，
    // 造成「半句加粗半句不加粗」甚至残留字面 **。这里剥掉引用块内的加粗标记，令逐字原文整齐呈现。
    var clean = quote.map(function (line) { return String(line).replace(/\*\*/g, "").replace(/__/g, ""); });
    html.push("<blockquote>" + clean.map(renderInlineMarkdown).join("<br>") + "</blockquote>");
    quote.length = 0;
  }
  function flushMarkdownBlocks(paragraph, list, quote, html) {
    flushMarkdownParagraph(paragraph, html);
    flushMarkdownList(list, html);
    flushMarkdownQuote(quote, html);
  }
  function splitMarkdownTableRow(line) {
    var source = String(line || "").trim();
    if (!source || source.indexOf("|") < 0) return null;
    var text = source;
    if (text.charAt(0) === "|") text = text.slice(1);
    if (text.charAt(text.length - 1) === "|") text = text.slice(0, -1);
    var cells = [], cell = "", escaped = false;
    for (var i = 0; i < text.length; i++) {
      var char = text.charAt(i);
      if (char === "|" && !escaped) { cells.push(cell.trim().replace(/\\\|/g, "|")); cell = ""; continue; }
      if (char === "\\" && !escaped) { escaped = true; cell += char; continue; }
      escaped = false; cell += char;
    }
    cells.push(cell.trim().replace(/\\\|/g, "|"));
    return cells;
  }
  function isMarkdownTableSeparator(line) {
    var cells = splitMarkdownTableRow(line);
    return !!cells && cells.length > 1 && cells.every(function (cell) { return /^:?-{3,}:?$/.test(cell.replace(/\s+/g, "")); });
  }
  function normalizeMarkdownTableCells(cells, columnCount) {
    return Array.from({ length: columnCount }, function (_, index) { return cells[index] || ""; });
  }
  function markdownTableAlignments(separatorLine, columnCount) {
    var cells = splitMarkdownTableRow(separatorLine) || [];
    return normalizeMarkdownTableCells(cells, columnCount).map(function (cell) {
      var compact = cell.replace(/\s+/g, "");
      if (compact.charAt(0) === ":" && compact.charAt(compact.length - 1) === ":") return "center";
      if (compact.charAt(compact.length - 1) === ":") return "right";
      return "left";
    });
  }
  function renderMarkdownTable(headerCells, separatorLine, bodyRows) {
    var columnCount = Math.max(headerCells.length, Math.max.apply(null, bodyRows.map(function (row) { return row.length; }).concat([0])));
    var alignments = markdownTableAlignments(separatorLine, columnCount);
    var header = normalizeMarkdownTableCells(headerCells, columnCount)
      .map(function (cell, index) { return '<th scope="col" style="text-align:' + alignments[index] + '">' + renderInlineMarkdown(cell) + "</th>"; })
      .join("");
    var rows = bodyRows.map(function (row) {
      return "<tr>" + normalizeMarkdownTableCells(row, columnCount).map(function (cell, index) { return '<td style="text-align:' + alignments[index] + '">' + renderInlineMarkdown(cell) + "</td>"; }).join("") + "</tr>";
    }).join("");
    return '<div class="markdown-table-wrap"><table><thead><tr>' + header + "</tr></thead><tbody>" + rows + "</tbody></table></div>";
  }
  function renderMarkdownCodeBlock(code, language) {
    var lang = String(language || "").trim();
    var label = lang ? '<span class="code-lang">' + esc(lang) + "</span>" : "";
    return "<pre>" + label + "<code>" + esc(code).replace(/\n$/, "") + "</code></pre>";
  }
  // 显示时精简引用夹注：保留与检索条目严格对应的 [N] 编号（研究/快速都要），仅把模型偶发写出的
  // 「（[1]《…》第X页）」这类冗长出处夹注缩成纯编号 [1]——书名/卷次/页码不占正文篇幅，准确出处由下方
  // 「引用原文」卡片给出。仅改显示、不动 message.content，故后端据 [N] 的精确高亮/深链不受影响。
  function stripInlineCitations(md) {
    var s = String(md || "");
    s = s.replace(/（\s*(\[\d+\])[^）]*）/g, "$1");   // 全角括注（以 [编号] 起头）→ 只留 [编号]
    s = s.replace(/\(\s*(\[\d+\])[^)]*\)/g, "$1");     // 半角括注（以 [编号] 起头）→ 只留 [编号]
    return s;
  }
  function renderBasicMarkdown(markdown) {
    var lines = String(markdown || "").replace(/\r\n?/g, "\n").split("\n");
    var html = [];
    var paragraph = [];
    var list = { type: null, items: [] };
    var quote = [];
    for (var lineIndex = 0; lineIndex < lines.length; lineIndex += 1) {
      var line = lines[lineIndex];
      var trimmed = line.trim();
      if (!trimmed) { flushMarkdownBlocks(paragraph, list, quote, html); continue; }
      var fence = trimmed.match(/^(`{3,}|~{3,})\s*([A-Za-z0-9_+.#-]*)?.*$/);
      if (fence) {
        flushMarkdownBlocks(paragraph, list, quote, html);
        var fenceMarker = fence[1][0];
        var fenceLength = fence[1].length;
        var codeLines = [];
        lineIndex += 1;
        while (lineIndex < lines.length) {
          var candidate = lines[lineIndex].trim();
          if (candidate.indexOf(fenceMarker.repeat(fenceLength)) === 0) break;
          codeLines.push(lines[lineIndex]);
          lineIndex += 1;
        }
        html.push(renderMarkdownCodeBlock(codeLines.join("\n"), fence[2] || ""));
        continue;
      }
      var headerCells = splitMarkdownTableRow(trimmed);
      var separatorLine = lines[lineIndex + 1] || "";
      if (headerCells && isMarkdownTableSeparator(separatorLine)) {
        flushMarkdownBlocks(paragraph, list, quote, html);
        var bodyRows = [];
        lineIndex += 2;
        while (lineIndex < lines.length) {
          var rowText = lines[lineIndex].trim();
          var rowCells = rowText ? splitMarkdownTableRow(rowText) : null;
          if (!rowCells || isMarkdownTableSeparator(rowText)) break;
          bodyRows.push(rowCells);
          lineIndex += 1;
        }
        html.push(renderMarkdownTable(headerCells, separatorLine, bodyRows));
        lineIndex -= 1;
        continue;
      }
      // ATX 标题：兼容「**### 标题**」这类被整行加粗包裹的写法（flash 偶发把 ### 与 ** 叠用），
      // 先剥掉整行 ** 包裹再判定，命中即渲染纯净标题、不残留字面 ### 或 **；未命中则原样继续。
      var atxLine = trimmed.replace(/^\*\*(.+?)\*\*$/, "$1");
      var heading = atxLine.match(/^(#{1,6})\s+(.+)$/);
      if (heading) {
        flushMarkdownBlocks(paragraph, list, quote, html);
        // 快速问答与研究综述共用同一套标题映射（排版逻辑一致，仅配色不同）：# / ## / ### 统一落到醒目的
        // h3（左条小标题），#### 及更深落 h4——聊天答案不设一/二级大标题。
        var level = heading[1].length <= 3 ? 3 : 4;
        var htext = heading[2].replace(/\s*#+\s*$/, "");  // 去掉「### 标题 ###」闭合式的尾随 #
        html.push("<h" + level + ">" + renderInlineMarkdown(htext) + "</h" + level + ">");
        continue;
      }
      if (/^([-*_])\s*\1\s*\1(?:\s*\1)*$/.test(trimmed)) {
        flushMarkdownBlocks(paragraph, list, quote, html);
        html.push("<hr>");
        continue;
      }
      var quoted = trimmed.match(/^[>＞]\s*(.+)$/) || trimmed.match(/^<\s*(.+)$/);
      if (quoted) {
        flushMarkdownParagraph(paragraph, html);
        flushMarkdownList(list, html);
        quote.push(quoted[1]);
        continue;
      }
      var unordered = trimmed.match(/^[-*+]\s+(.+)$/);
      var ordered = trimmed.match(/^\d+[.)]\s+(.+)$/);
      if (unordered || ordered) {
        flushMarkdownParagraph(paragraph, html);
        flushMarkdownQuote(quote, html);
        var type = ordered ? "ol" : "ul";
        if (list.type && list.type !== type) flushMarkdownList(list, html);
        list.type = type;
        list.items.push((ordered || unordered)[1]);
        continue;
      }
      flushMarkdownList(list, html);
      flushMarkdownQuote(quote, html);
      paragraph.push(trimmed);
    }
    flushMarkdownBlocks(paragraph, list, quote, html);
    return html.join("") || "<p></p>";
  }

  // ===================== 来源 / 警告 / 引文 =====================
  function renderSources(sources) {
    if (!Array.isArray(sources) || !sources.length) return "";
    return '<div class="sources">' + sources.map(function (source, index) {
      return '<div class="source-item"><a href="' + escAttr(source.link || "#") + '" target="_blank" rel="noopener">' +
        (index + 1) + ". " + esc(source.title || "未命名来源") + '</a><div class="source-meta">' +
        esc(source.site || "未知站点") + (source.date ? " · " + esc(source.date) : "") + "</div></div>";
    }).join("") + "</div>";
  }
  function renderWarnings(warnings) {
    if (!Array.isArray(warnings) || !warnings.length) return "";
    return '<ul class="warning-list">' + warnings.map(function (item) { return "<li>" + esc(item) + "</li>"; }).join("") + "</ul>";
  }
  function ctxHtml(text) {
    return esc(String(text || "")).replace(/\[\[H\]\]/g, "<mark>").replace(/\[\[\/H\]\]/g, "</mark>");
  }
  function renderEvidenceLinks(evidence) {
    if (!Array.isArray(evidence) || !evidence.length) return "";
    var links = [];
    for (var i = 0; i < evidence.length; i++) {
      var ev = evidence[i] || {};
      var kindLabel = ev.kind === "paraphrase" ? "转述出处" : "逐字引文";
      var pageLabel = ev.printed_page ? ("第 " + ev.printed_page + " 页") : (ev.pdf_page ? ("第 " + ev.pdf_page + " 页（PDF）") : "");
      var citationText = pickCite(ev) || (kindLabel + (pageLabel ? " · " + pageLabel : " · 页码待核验"));
      links.push('<div class="aip-cite-evidence"><div' + (citeDataAttr(ev) ? ' data-citations="' + citeDataAttr(ev) + '"' : '') + '>' + esc(citationText) + '</div>' +
        (ev.context ? '<div class="ai-citation-ctx">' + ctxHtml(ev.context) + '</div>' : '') +
        (ev.viewer_url ? '<a class="ai-citation-open" href="' + escAttr(ev.viewer_url) + '" target="_blank" rel="noopener">' +
        esc(kindLabel + (pageLabel ? " · " + pageLabel : "")) + ' →</a>' : '') + '</div>');
    }
    return links.join("");
  }
  function renderCitations(message) {
    var citations = message.citations;
    if (!Array.isArray(citations) || !citations.length) return "";
    var isResearch = message.kind === "research";
    var pendingPages = citations.filter(function (c) { return c.location_status === "unresolved" || c.location_status === "partial"; }).length;
    var scope = message.groundingScope;
    var scopeNote = (scope && scope.applied && scope.label)
      ? '<span class="ai-citations-scope">· 检索范围：' + esc(scope.label) + (scope.manual ? "（手动指定）" : "（智能判断）") + "</span>"
      : "";
    var title = isResearch
      ? '引用原文 · 综述所据（' + citations.length + "）" + scopeNote
      : '引用原文 · 来自引文库（' + citations.length + "）" + scopeNote;
    if (pendingPages) title += " · " + pendingPages + " 条页码待核验";
    // 引用条目默认折叠：标题做成开合按钮，点击展开全部条目（快速问答与研究综述通用）。
    // 每次 renderMessages 重绘会恢复默认折叠态（历史里不存开合状态，保持默认收起）。
    var cardsHtml = citations.map(function (c, i) {
      var idx = c.grounding_index || c.review_index || (i + 1);
      var cite = esc(pickCite(c));
      var dataAttr = citeDataAttr(c);   // 供「引用格式」切换时就地重写此条出处串
      var ctx = ctxHtml(c.context);
      var evLinks = renderEvidenceLinks(c.evidence);
      if (evLinks) ctx = "";
      var workTitle = c.work_title ? ("篇目：《" + esc(c.work_title) + "》") : "篇目：未核验";
      var workAuthors = Array.isArray(c.work_authors) && c.work_authors.length
        ? ("责任者：" + esc(c.work_authors.join("、"))) : "责任者：未核验";
      var provenance = '<div class="aip-cite-provenance">' + workTitle + " · " + workAuthors +
        (c.provenance_verified ? ' <span class="aip-cite-verified">已核验</span>' : "") + "</div>";
      // 研究综述引文条对齐检索页 .rv-cite：金色 [N] 序号 + 「综述已引用」「名目索引」徽标。
      var badges = "";
      if (isResearch) {
        if (c.review_quoted) badges += '<span class="aip-cite-badge used">综述已引用</span>';
        if (c.subject_label) badges += '<span class="aip-cite-badge subj">名目索引 · ' + esc(c.subject_label) + "</span>";
      }
      var citeSpan = '<span class="aip-cite-idx">[' + idx + "]</span> " + badges +
        "<span" + (dataAttr ? ' data-citations="' + dataAttr + '"' : "") + ">" + cite + "</span>";
      var openLink = (!evLinks && c.viewer_url)
        ? '<a class="ai-citation-open" href="' + escAttr(c.viewer_url) + '" target="_blank" rel="noopener">打开原文页 →</a>' : "";
      var actions = '<div class="aip-cite-actions">' +
        '<button type="button" class="aip-cite-copy" data-copy-cite>复制引文</button>' + openLink + "</div>";
      return '<div class="ai-citation-item"><div class="ai-citation-cite">' + citeSpan + "</div>" + provenance +
        (ctx ? '<div class="ai-citation-ctx">' + ctx + "</div>" : "") + evLinks + actions + "</div>";
    }).join("");
    return '<div class="ai-citations aip-cites-collapsed">' +
      '<div class="ai-citations-head">' +
        '<button type="button" class="ai-citations-toggle" aria-expanded="false">' +
          '<span class="aip-cite-caret" aria-hidden="true">▸</span>' +
          '<span class="ai-citations-title">' + title + "</span>" +
          '<span class="aip-cite-hint">展开</span>' +
        "</button>" +
      "</div>" +
      '<div class="ai-citations-body">' + cardsHtml + "</div>" +
    "</div>";
  }

  // ===================== 对话渲染 =====================
  function emptyStateHtml() {
    if (config && !anyAccess()) {
      return '<div class="chat-empty">' + esc(lockedMessage()) + "</div>";
    }
    return '<div class="chat-empty">在下方输入你的问题，开始一段可连续追问的研究对话。' +
      '<span class="aip-eg">例如：「谈谈马克思对异化劳动的分析」，或切换「研究综述」深挖一个专题。</span></div>';
  }
  function quoteDisplayMap(text) {
    var normalized = "", starts = [], ends = [], offset = 0;
    Array.from(String(text || "")).forEach(function (ch) {
      var folded = ch.normalize("NFKC").replace(/[“”「」]/g, '"').replace(/[‘’『』]/g, "'");
      Array.from(folded).forEach(function (c) {
        if (!/\s/.test(c)) { normalized += c; for (var j = 0; j < c.length; j++) { starts.push(offset); ends.push(offset + ch.length); } }
      });
      offset += ch.length;
    });
    return { text: normalized, starts: starts, ends: ends };
  }
  function verifiedQuoteRanges(text, message, quoteBlock) {
    var refs = new Set(), ranges = [], map = quoteDisplayMap(text);
    String(text).replace(/\[(\d+(?:\s*[,，、]\s*\d+)*)\]/g, function (_, group) {
      group.match(/\d+/g).forEach(function (id) { refs.add(Number(id)); }); return _;
    });
    (message.citations || []).forEach(function (citation) {
      var index = citation.grounding_index || citation.review_index;
      if (!refs.has(Number(index))) return;
      (citation.evidence || []).forEach(function (ev) {
        if (ev.kind !== "quote" || !ev.quote || !(ev.text_verified === true || ev.location_status === "verified")) return;
        var needle = quoteDisplayMap(ev.quote).text;
        if (needle.length < 4) return;
        var at = map.text.indexOf(needle);
        while (at >= 0) {
          var left = map.starts[at], right = map.ends[at + needle.length - 1];
          if (needle.length >= 20 || quoteBlock || (/[“「『"']/.test(text.slice(left-1,left)) && /[”」』"']/.test(text.slice(right,right+1)))) {
            if (/[“「『"']/.test(text.slice(left-1,left)) && /[”」』"']/.test(text.slice(right,right+1))) { left--; right++; }
            ranges.push([left, right]);
          }
          at = map.text.indexOf(needle, at + needle.length);
        }
      });
    });
    ranges.sort(function (a, b) { return a[0] - b[0] || b[1] - a[1]; });
    var merged = [];
    ranges.forEach(function (r) {
      var last = merged[merged.length - 1];
      if (last && r[0] <= last[1]) last[1] = Math.max(last[1], r[1]); else merged.push(r.slice());
    });
    return merged;
  }
  function quoteDisplayContent(message) {
    // History is immutable: repair display/export boundaries only from saved
    // verified evidence. Never infer an absent card or a new source number.
    var fenced = false;
    return String(message.content || "").split("\n").map(function (line) {
      if (/^\s*(?:```|~~~)/.test(line)) { fenced = !fenced; return line; }
      if (fenced || /^\s*(?:>|#)/.test(line)) return line;
      var protectedRanges = [], stack = [], openAt = 0, pairs = {"“":"”", "「":"」", "『":"』", '"':'"'};
      for (var i = 0; i < line.length; i++) {
        var ch = line[i];
        if (stack.length && ch === stack[stack.length-1]) {
          stack.pop(); if (!stack.length) protectedRanges.push([openAt,i+1]);
        } else if (pairs[ch]) { if (!stack.length) openAt=i; stack.push(pairs[ch]); }
      }
      var literalRanges = [];
      line.replace(/`[^`]*`|!?\[[^\]]*\]\([^)]*\)/g,function (m,offset) { literalRanges.push([offset,offset+m.length]); return m; });
      var ranges = verifiedQuoteRanges(line, message, false).filter(function (r) {
        if (literalRanges.some(function (p) { return r[0] < p[1] && r[1] > p[0]; })) return false;
        return !protectedRanges.some(function (p) {
          return r[0] < p[1] && r[1] > p[0] && !(r[0] < p[0] && r[1] > p[1]);
        });
      });
      for (var j=ranges.length-1;j>=0;j--) { var r=ranges[j]; line=line.slice(0,r[0])+"“"+line.slice(r[0],r[1])+"”"+line.slice(r[1]); }
      return line;
    }).join("\n");
  }
  function renderAnswerMarkdown(message, exporting) {
    message = Object.assign({}, message, { content: quoteDisplayContent(message) });
    var html = exporting ? renderExportMarkdown(message.content || "") : renderBasicMarkdown(stripInlineCitations(message.content));
    try {
      var root = document.createElement("div"); root.innerHTML = html;
      root.querySelectorAll("p,li,blockquote,td").forEach(function (block) {
        if (block.querySelector("p,li,blockquote,td")) return;
        var ranges = verifiedQuoteRanges(block.textContent, message, block.tagName === "BLOCKQUOTE");
        if (!ranges.length) return;
        var walker = document.createTreeWalker(block, 4), nodes = [], pos = 0, node;
        while ((node = walker.nextNode())) {
          nodes.push({node:node, start:pos, end:pos + node.textContent.length}); pos += node.textContent.length;
        }
        nodes.forEach(function (item) {
          if (item.node.parentElement.closest("code,pre,a,.aip-direct-quote")) return;
          var spans = ranges.map(function (r) { return [Math.max(r[0], item.start)-item.start, Math.min(r[1], item.end)-item.start]; }).filter(function (r) { return r[0] < r[1]; });
          if (!spans.length) return;
          var fragment = document.createDocumentFragment(), text = item.node.textContent, cursor = 0;
          spans.forEach(function (r) {
            fragment.appendChild(document.createTextNode(text.slice(cursor, r[0])));
            var span = document.createElement("span"); span.className = "aip-direct-quote";
            span.textContent = text.slice(r[0], r[1]); fragment.appendChild(span); cursor = r[1];
          });
          fragment.appendChild(document.createTextNode(text.slice(cursor))); item.node.replaceWith(fragment);
        });
      });
      return root.innerHTML;
    } catch (_) { return html; }
  }
  function numberMessageCitations(message) {
    if (!message || message.role !== "assistant" || !Array.isArray(message.citations) || !message.citations.length) return message;
    var cards = {}, ids = [], order = [], map = {};
    message.citations.forEach(function (card, i) {
      var id = Number(card.grounding_index || card.review_index || i + 1);
      if (!cards[id]) ids.push(id);
      cards[id] = card;
    });
    // Keep Markdown links, fenced/inline code and reference definitions literal.
    var tokens = /(^```[^\n]*\n[\s\S]*?^```[^\n]*$|^~~~[^\n]*\n[\s\S]*?^~~~[^\n]*$|`[^`\n]*`|!?\[[^\]\n]*\]\([^\n)]*\)|^\[\d+\]:[^\n]*)|(\[\d+(?:\s*[,，、]\s*\d+)*\])/gm;
    var content = quoteDisplayContent(message);
    content.replace(tokens, function (token, literal, ref) {
      if (ref) ref.match(/\d+/g).forEach(function (value) {
        var id = Number(value);
        if (cards[id] && order.indexOf(id) < 0) order.push(id);
      });
      return token;
    });
    ids.forEach(function (id) { if (order.indexOf(id) < 0) order.push(id); });
    order.forEach(function (id, i) { map[id] = i + 1; });
    return Object.assign({}, message, {
      content: content.replace(tokens, function (token, literal, ref) {
        return ref ? ref.replace(/\d+/g, function (id) { return map[Number(id)] || id; }) : token;
      }),
      citations: order.map(function (id) {
        var card = Object.assign({}, cards[id]);
        if (card.grounding_index !== undefined || card.review_index === undefined) card.grounding_index = map[id];
        if (card.review_index !== undefined) card.review_index = map[id];
        return card;
      })
    });
  }
  function renderMessages() {
    if (!messages.length) { messagesEl.innerHTML = emptyStateHtml(); return; }
    messagesEl.innerHTML = messages.map(function (message, messageIndex) {
      message = numberMessageCitations(message);
      if (message.role === "user") return '<div class="msg user">' + esc(message.content) + "</div>";
      var isResearch = message.kind === "research";
      if (message.pending) {
        var pendMsg = message.progress || (isResearch ? "正在检索原著并生成研究综述（较慢，请稍候）" : "AI 正在思考");
        return '<div class="msg assistant pending' + (isResearch ? " research" : "") + '" aria-live="polite">' +
          '<span class="msg-title">' + esc(pendMsg) + '</span>' +
          '<span class="typing-dots" aria-label="生成中"><span></span><span></span><span></span></span></div>';
      }
      var kindPill = '<span class="msg-kind">' + (isResearch ? "研究综述" : "快速问答") + "</span>";
      var citeTools = Array.isArray(message.citations) && message.citations.length ? citeFormatSelectHtml() : "";
      var exportTools = '<span class="aip-answer-tools">' + citeTools +
        '<span class="aip-answer-export" aria-label="下载本条 AI 回复为 Word">' +
          '<button type="button" data-export-word="footnote" data-message-index="' + messageIndex + '">a.导出脚注版word</button>' +
          '<button type="button" data-export-word="endnote" data-message-index="' + messageIndex + '">b.导出尾注版word</button>' +
        '</span></span>';
      return '<div class="msg assistant' + (isResearch ? " research" : "") + '">' +
        '<div class="msg-head"><span class="msg-title">' + kindPill + "AI 回答</span>" + exportTools + "</div>" +
        '<div class="msg-body' + (isResearch ? " aip-essay" : "") + '">' + renderAnswerMarkdown(message, false) + "</div>" +
        renderWarnings(message.warnings) + renderCitations(message) + renderSources(message.sources) + "</div>";
    }).join("");
  }

  // ===================== 本地导出（HTML / ZIP / 学术排版 DOCX） =====================
  // 所有文件都在浏览器内由当前已加载的会话副本生成，不把正文发送到任何导出接口。
  function pad2(n) { return String(n).padStart(2, "0"); }
  function localDateValue(ts) {
    var d = new Date(Number(ts || nowTs()));
    return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
  }
  function localDateTime(ts) {
    var d = new Date(Number(ts || nowTs()));
    return localDateValue(d.getTime()) + " " + pad2(d.getHours()) + ":" + pad2(d.getMinutes());
  }
  function safeFilePart(text, fallback) {
    var out = String(text || "").replace(/[<>:\"/\\|?*\u0000-\u001f]/g, " ").replace(/\s+/g, " ").trim();
    out = out.replace(/[. ]+$/g, "");
    if (!out) out = fallback || "AI研究对话";
    if (/^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(out)) out = "_" + out;
    return out.slice(0, 72);
  }
  function plainTitle(text, fallback) {
    var out = String(text || "")
      .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
      .replace(/[`*_~#>|]/g, " ").replace(/\s+/g, " ").trim();
    if (!out) out = fallback || "AI研究对话";
    if (out.length > 80) out = out.slice(0, 79) + "…";
    return out;
  }
  function downloadBlob(blob, filename) {
    var url = URL.createObjectURL(blob);
    var link = document.createElement("a");
    link.href = url; link.download = filename; link.style.display = "none";
    document.body.appendChild(link); link.click(); document.body.removeChild(link);
    setTimeout(function () { URL.revokeObjectURL(url); }, 30000);
  }
  function utf8Bytes(text) {
    if (window.TextEncoder) return new TextEncoder().encode(String(text));
    var encoded = unescape(encodeURIComponent(String(text))), out = new Uint8Array(encoded.length);
    for (var i = 0; i < encoded.length; i++) out[i] = encoded.charCodeAt(i);
    return out;
  }
  function concatBytes(parts) {
    var total = parts.reduce(function (sum, part) { return sum + part.length; }, 0);
    var out = new Uint8Array(total), offset = 0;
    parts.forEach(function (part) { out.set(part, offset); offset += part.length; });
    return out;
  }
  function pushU16(out, value) { out.push(value & 255, (value >>> 8) & 255); }
  function pushU32(out, value) { out.push(value & 255, (value >>> 8) & 255, (value >>> 16) & 255, (value >>> 24) & 255); }
  var ZIP_CRC_TABLE = (function () {
    var table = new Uint32Array(256);
    for (var n = 0; n < 256; n++) {
      var c = n;
      for (var k = 0; k < 8; k++) c = (c & 1) ? (0xedb88320 ^ (c >>> 1)) : (c >>> 1);
      table[n] = c >>> 0;
    }
    return table;
  })();
  function crc32(bytes) {
    var crc = 0xffffffff;
    for (var i = 0; i < bytes.length; i++) crc = ZIP_CRC_TABLE[(crc ^ bytes[i]) & 255] ^ (crc >>> 8);
    return (crc ^ 0xffffffff) >>> 0;
  }
  function zipDosTime(date) {
    var d = date || new Date(), year = Math.max(1980, d.getFullYear());
    return {
      time: ((d.getHours() & 31) << 11) | ((d.getMinutes() & 63) << 5) | ((Math.floor(d.getSeconds() / 2)) & 31),
      date: (((year - 1980) & 127) << 9) | (((d.getMonth() + 1) & 15) << 5) | (d.getDate() & 31),
    };
  }
  function StoreZip() { this.entries = []; }
  StoreZip.prototype.add = function (name, value) {
    var data = value instanceof Uint8Array ? value : utf8Bytes(value);
    this.entries.push({ name: String(name), nameBytes: utf8Bytes(String(name)), data: data, crc: crc32(data), stamp: zipDosTime(new Date()) });
  };
  StoreZip.prototype.blob = function (mime) {
    var locals = [], centrals = [], offset = 0;
    this.entries.forEach(function (entry) {
      var localHead = [];
      pushU32(localHead, 0x04034b50); pushU16(localHead, 20); pushU16(localHead, 0x0800); pushU16(localHead, 0);
      pushU16(localHead, entry.stamp.time); pushU16(localHead, entry.stamp.date); pushU32(localHead, entry.crc);
      pushU32(localHead, entry.data.length); pushU32(localHead, entry.data.length); pushU16(localHead, entry.nameBytes.length); pushU16(localHead, 0);
      var local = concatBytes([new Uint8Array(localHead), entry.nameBytes, entry.data]);
      var centralHead = [];
      pushU32(centralHead, 0x02014b50); pushU16(centralHead, 20); pushU16(centralHead, 20); pushU16(centralHead, 0x0800); pushU16(centralHead, 0);
      pushU16(centralHead, entry.stamp.time); pushU16(centralHead, entry.stamp.date); pushU32(centralHead, entry.crc);
      pushU32(centralHead, entry.data.length); pushU32(centralHead, entry.data.length); pushU16(centralHead, entry.nameBytes.length);
      pushU16(centralHead, 0); pushU16(centralHead, 0); pushU16(centralHead, 0); pushU16(centralHead, 0); pushU32(centralHead, 0); pushU32(centralHead, offset);
      locals.push(local); centrals.push(concatBytes([new Uint8Array(centralHead), entry.nameBytes])); offset += local.length;
    });
    var centralBytes = concatBytes(centrals), end = [];
    pushU32(end, 0x06054b50); pushU16(end, 0); pushU16(end, 0); pushU16(end, this.entries.length); pushU16(end, this.entries.length);
    pushU32(end, centralBytes.length); pushU32(end, offset); pushU16(end, 0);
    return new Blob(locals.concat([centralBytes, new Uint8Array(end)]), { type: mime || "application/zip" });
  };

  function exportUrl(url) {
    var safe = safeMarkdownUrl(url || "");
    if (safe.charAt(0) === "/") return window.location.origin + safe;
    return safe;
  }
  function renderExportMarkdown(markdown) {
    return renderBasicMarkdown(stripInlineCitations(markdown || "")).replace(/href="(\/[^\"]*)"/g, function (_, path) {
      return 'href="' + escAttr(window.location.origin + path.replace(/&amp;/g, "&")) + '"';
    });
  }

  function exportCitationHtml(message) {
    message = numberMessageCitations(message);
    var citations = Array.isArray(message.citations) ? message.citations : [];
    if (!citations.length) return "";
    return '<section class="citations"><h3>引用原文与出处</h3><ol>' + citations.map(function (citation, index) {
      var number = citation.grounding_index || citation.review_index || (index + 1);
      var context = String(citation.context || "").replace(/\[\[\/?H\]\]/g, "").trim();
      var url = exportUrl(citation.viewer_url || "");
      return '<li value="' + Number(number || index + 1) + '"><p class="cite">' + esc(pickCite(citation) || "出处未提供") + '</p>' +
        (Array.isArray(citation.evidence) ? citation.evidence.map(function (ev) {
          var link = exportUrl(ev.viewer_url || "");
          return '<p>' + esc(pickCite(ev) || "页码待核验") + '</p><blockquote>' + ctxHtml(ev.context || "") + '</blockquote>' +
            (link ? '<a href="' + escAttr(link) + '">打开对应原文页</a>' : '');
        }).join("") : '') +
        (context ? '<blockquote>' + esc(context) + '</blockquote>' : '') +
        (url ? '<a href="' + escAttr(url) + '" target="_blank" rel="noopener noreferrer">打开原文页</a>' : '') + '</li>';
    }).join("") + "</ol></section>";
  }
  function exportSourcesHtml(sources) {
    if (!Array.isArray(sources) || !sources.length) return "";
    return '<section class="sources"><h3>网络来源</h3><ol>' + sources.map(function (source) {
      var url = exportUrl(source.link || "");
      var title = esc(source.title || "未命名来源");
      return '<li>' + (url ? '<a href="' + escAttr(url) + '" target="_blank" rel="noopener noreferrer">' + title + '</a>' : title) +
        '<small>' + esc([source.site, source.date].filter(Boolean).join(" · ")) + '</small></li>';
    }).join("") + "</ol></section>";
  }
  function exportMessageHtml(message, answerNo) {
    message = numberMessageCitations(message);
    if (!message || message.pending) return "";
    if (message.role === "user") return '<section class="turn user"><div class="turn-label">用户</div><div class="user-text">' + esc(message.content || "") + "</div></section>";
    var kind = message.kind === "research" ? "研究综述" : "快速问答";
    var warnings = Array.isArray(message.warnings) && message.warnings.length
      ? '<ul class="warnings">' + message.warnings.map(function (item) { return "<li>" + esc(item) + "</li>"; }).join("") + "</ul>" : "";
    return '<section class="turn assistant' + (message.kind === "research" ? " research" : "") + '"><div class="turn-label">' + esc(kind) + ' · AI 回复 ' + answerNo + '</div>' +
      '<div class="answer">' + renderAnswerMarkdown(message, true) + '</div>' + warnings +
      exportCitationHtml(message) + exportSourcesHtml(message.sources) + "</section>";
  }
  function buildSessionHtml(session) {
    var title = session.title || deriveTitle(session.messages || []), answerNo = 0;
    var turns = (session.messages || []).map(function (message) {
      if (message && message.role === "assistant" && !message.pending) answerNo += 1;
      return exportMessageHtml(message, answerNo);
    }).join("");
    var css = "*{box-sizing:border-box}body{margin:0;background:#f4efe7;color:#29211b;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',sans-serif;line-height:1.8}.page{max-width:920px;margin:32px auto;padding:0 20px 64px}.doc-head{background:#fffdf8;border:1px solid #e5d8ca;border-radius:18px;padding:28px 32px;margin-bottom:18px;box-shadow:0 10px 30px rgba(70,45,25,.06)}h1{margin:0 0 8px;font-family:'Songti SC','SimSun',serif;font-size:28px;line-height:1.4}.meta{color:#806f61;font-size:13px}.turn{border-radius:18px;margin:14px 0;padding:22px 26px;border:1px solid #e6d9cc}.turn.user{margin-left:14%;background:#eee3d7}.turn.assistant{background:#fffdf9}.turn-label{color:#8f1d1d;font-size:13px;font-weight:700;margin-bottom:10px}.user-text{white-space:pre-wrap}.answer{font-family:'Songti SC','SimSun',serif;font-size:16px}.answer h3{font-size:19px;border-left:4px solid #8f1d1d;padding-left:10px;margin:25px 0 12px}.answer h4,.answer h5{font-size:17px;margin:22px 0 10px}.answer p,.answer ul,.answer ol,.answer blockquote{margin:10px 0}.answer blockquote,.citations blockquote{margin:8px 0;padding:9px 12px;border-left:3px solid #c9a227;background:#faf4e7}.answer table{width:100%;border-collapse:collapse}.answer th,.answer td{border:1px solid #d9c9ba;padding:7px 9px}.answer pre{overflow:auto;background:#2f2925;color:#fff;padding:14px;border-radius:10px}.citations,.sources{margin-top:20px;padding-top:14px;border-top:1px solid #eadfd4}.citations h3,.sources h3{font-size:16px;margin:0 0 8px}.citations li,.sources li{margin:9px 0}.cite{font-weight:700;margin:0}.citations a,.sources a,.answer a{color:#8f1d1d}.sources small{display:block;color:#806f61}.warnings{color:#805f00}.empty{padding:24px;color:#806f61;background:#fffdf9;border-radius:16px}@media(max-width:640px){.page{margin:0;padding:12px}.doc-head,.turn{padding:18px}.turn.user{margin-left:5%}}@media print{body{background:#fff}.page{max-width:none;margin:0;padding:0}.doc-head,.turn{box-shadow:none;break-inside:avoid}.turn.user{margin-left:8%}}";
    css += '.aip-direct-quote{font-family:KaiTi,STKaiti,"楷体","Kaiti SC",serif;font-style:normal}.assistant.research .answer blockquote{padding:0;border:0;background:transparent}';
    return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>' + esc(title) +
      '</title><style>' + css + '</style></head><body><main class="page"><header class="doc-head"><h1>' + esc(title) + '</h1><div class="meta">最后更新：' +
      esc(localDateTime(session.updatedAt)) + '　·　本文件由 AI 研究对话在本地生成</div></header>' + (turns || '<div class="empty">此会话暂无可导出的内容。</div>') + "</main></body></html>";
  }
  function sessionById(id) { return sessionsData.sessions.find(function (session) { return session.id === id; }); }
  function exportSessionHtml(id) {
    var session = sessionById(id);
    if (!session) return;
    if (!(session.messages || []).some(function (message) { return message && !message.pending; })) { setCloudStatus("这段会话暂无可导出的内容。", "warn"); return; }
    var filename = safeFilePart(session.title || deriveTitle(session.messages || []), "AI研究对话") + "_" + localDateValue(session.updatedAt) + ".html";
    downloadBlob(new Blob(["\ufeff", buildSessionHtml(session)], { type: "text/html;charset=utf-8" }), filename);
    setCloudStatus("已导出当前会话的本地 HTML 文件。", "ok");
  }
  function ensureExportDateDefaults() {
    if (!exportStartEl || !exportEndEl) return;
    var end = new Date(), start = new Date(end.getTime() - 29 * 86400000);
    if (!exportStartEl.value) exportStartEl.value = localDateValue(start.getTime());
    if (!exportEndEl.value) exportEndEl.value = localDateValue(end.getTime());
  }
  function exportSessionsInRange() {
    if (!exportStartEl || !exportEndEl || !exportRangeBtn || streaming) return;
    var startValue = exportStartEl.value, endValue = exportEndEl.value;
    var start = new Date(startValue + "T00:00:00").getTime();
    var end = new Date(endValue + "T23:59:59.999").getTime();
    if (!startValue || !endValue || !Number.isFinite(start) || !Number.isFinite(end) || start > end) {
      showStorageWarning("请选择有效的开始日期和结束日期，且开始日期不能晚于结束日期。"); return;
    }
    var selected = sessionsData.sessions.filter(function (session) {
      var updated = Number(session.updatedAt || 0);
      return updated >= start && updated <= end && (session.messages || []).some(function (message) { return message && !message.pending; });
    }).sort(function (a, b) { return Number(a.updatedAt || 0) - Number(b.updatedAt || 0); });
    if (!selected.length) { setCloudStatus("所选时间段内没有可导出的会话。", "warn"); return; }
    exportRangeBtn.disabled = true; exportRangeBtn.textContent = "正在整理…";
    setTimeout(function () {
      try {
        var zip = new StoreZip(), used = {};
        selected.forEach(function (session, index) {
          var base = localDateValue(session.updatedAt) + "_" + String(index + 1).padStart(3, "0") + "_" + safeFilePart(session.title || deriveTitle(session.messages || []), "AI研究对话");
          var name = base + ".html", serial = 2;
          while (used[name]) { name = base + "_" + serial + ".html"; serial += 1; }
          used[name] = true; zip.add(name, "\ufeff" + buildSessionHtml(session));
        });
        downloadBlob(zip.blob("application/zip"), "AI研究对话_" + startValue + "_至_" + endValue + ".zip");
        setCloudStatus("已导出 " + selected.length + " 段会话；ZIP 内每段会话一个 HTML 文件。", "ok");
      } catch (_) {
        setCloudStatus("批量导出失败，请缩短时间范围后重试。", "warn");
      } finally {
        exportRangeBtn.disabled = false; exportRangeBtn.textContent = "导出 ZIP";
      }
    }, 30);
  }

  function xmlText(text) { return String(text == null ? "" : text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
  function xmlAttr(text) { return xmlText(text).replace(/\"/g, "&quot;").replace(/'/g, "&apos;"); }
  function wordNormalizeChinesePunctuation(text) {
    var source = String(text == null ? "" : text);
    function isCjk(ch) { return !!ch && /[\u3400-\u9fff\uf900-\ufaff]/.test(ch); }
    function nearest(index, step) {
      for (var i = index + step; i >= 0 && i < source.length; i += step) {
        if (!/\s/.test(source.charAt(i))) return source.charAt(i);
      }
      return "";
    }
    function hasNearbyCjk(index, radius) {
      var start = Math.max(0, index - radius), end = Math.min(source.length, index + radius + 1);
      return /[\u3400-\u9fff\uf900-\ufaff]/.test(source.slice(start, end));
    }
    function pairContainsCjk(index, open, close, direction) {
      var end = direction > 0 ? source.indexOf(close, index + 1) : source.lastIndexOf(open, index - 1);
      if (end < 0 || Math.abs(end - index) > 120) return false;
      var left = Math.min(index, end), right = Math.max(index, end);
      return /[\u3400-\u9fff\uf900-\ufaff]/.test(source.slice(left + 1, right));
    }
    // 中文引号先成对处理，英文缩写和所有格不动。
    source = source.replace(/\"([^\"\r\n]*[\u3400-\u9fff\uf900-\ufaff][^\"\r\n]*)\"/g, "“$1”");
    source = source.replace(/'([^'\r\n]*[\u3400-\u9fff\uf900-\ufaff][^'\r\n]*)'/g, "‘$1’");
    // 中文句子中包围英文、数字或被 Markdown 标记拆开的直引号，也必须使用中文弯引号。
    // 这里补齐未被上面的成对规则覆盖的引号；英文单词内部的撇号仍保持半角。
    var doubleQuoteOpen = false, singleQuoteOpen = false;
    source = source.replace(/[\"＂]/g, function (mark, index) {
      if (!hasNearbyCjk(index, 24)) return mark;
      var previous = nearest(index, -1), next = nearest(index, 1);
      var closes = doubleQuoteOpen;
      if (!previous || /[（【〔《〈“‘]/.test(previous)) closes = false;
      else if (!next || /[，。；：！？、）】〕》〉”’]/.test(next)) closes = true;
      doubleQuoteOpen = !closes;
      return closes ? "”" : "“";
    });
    source = source.replace(/['＇]/g, function (mark, index) {
      var previous = nearest(index, -1), next = nearest(index, 1);
      if (/[A-Za-z0-9]/.test(previous) && /[A-Za-z0-9]/.test(next)) return mark;
      if (!hasNearbyCjk(index, 24)) return mark;
      var closes = singleQuoteOpen;
      if (!previous || /[（【〔《〈“‘]/.test(previous)) closes = false;
      else if (!next || /[，。；：！？、）】〕》〉”’]/.test(next)) closes = true;
      singleQuoteOpen = !closes;
      return closes ? "’" : "‘";
    });
    // 中文语境的三个及以上英文句点统一为两个全角省略号。
    source = source.replace(/\.{3,}/g, function (dots, index) {
      return hasNearbyCjk(index, 16) ? "……" : dots;
    });
    return source.replace(/[,:;!?().]/g, function (mark, index) {
      var previous = nearest(index, -1), next = nearest(index, 1);
      // 小数、千分位、时间与端口号保持半角，与数字一样使用 Times New Roman。
      if ((mark === "." || mark === "," || mark === ":") && /\d/.test(previous) && /\d/.test(next)) return mark;
      // URL、Windows 路径及西文词内部的标点不转换，避免损坏可识别的西文结构。
      var before = source.slice(Math.max(0, index - 20), index);
      var after = source.slice(index + 1, Math.min(source.length, index + 4));
      if (mark === ":" && (
        (/[a-z][a-z0-9+.-]*$/i.test(before) && after.indexOf("//") === 0) ||
        (/[a-z]$/i.test(before) && after.indexOf("\\") === 0)
      )) return mark;
      if (mark === "." && /[A-Za-z0-9]/.test(previous) && /[A-Za-z0-9]/.test(next)) return mark;
      var chineseContext = isCjk(previous) || isCjk(next) || hasNearbyCjk(index, 16);
      if (!chineseContext) return mark;
      if (mark === "(" && !(pairContainsCjk(index, "(", ")", 1) || isCjk(previous) || isCjk(next))) return mark;
      if (mark === ")" && !(pairContainsCjk(index, "(", ")", -1) || isCjk(previous) || isCjk(next))) return mark;
      return { ",": "，", ":": "：", ";": "；", "!": "！", "?": "？", "(": "（", ")": "）", ".": "。" }[mark] || mark;
    });
  }
  function wordUsesEastAsiaFont(ch) {
    return !!ch && /[\u3000-\u303f\u3400-\u9fff\uf900-\ufaff\ufe10-\ufe1f\ufe30-\ufe4f\uff01-\uff65\u2014\u2018\u2019\u201c\u201d\u2026]/.test(ch);
  }
  function wordRunXml(text, options) {
    options = options || {};
    var eastAsiaFont = options.eastAsiaFont || (options.code ? "仿宋" : "宋体");
    var value = String(text == null ? "" : text).replace(/\[\[\/?H\]\]/g, "");
    if (!options.code) value = wordNormalizeChinesePunctuation(value);
    var chars = Array.from(value), groups = [];
    chars.forEach(function (ch, index) {
      var east = wordUsesEastAsiaFont(ch);
      if (/\s/.test(ch)) {
        if (groups.length) east = groups[groups.length - 1].east;
        else {
          for (var lookahead = index + 1; lookahead < chars.length; lookahead++) {
            if (!/\s/.test(chars[lookahead])) { east = wordUsesEastAsiaFont(chars[lookahead]); break; }
          }
        }
      }
      var group = groups[groups.length - 1];
      if (!group || group.east !== east) { group = { east: east, text: "" }; groups.push(group); }
      group.text += ch;
    });
    if (!groups.length) groups.push({ east: false, text: "" });
    return groups.map(function (group) {
      // 中文、全角标点和中文弯引号显式绑定东亚字体；英文、数字及其内部标点保持 Times New Roman。
      var westernFont = group.east ? eastAsiaFont : "Times New Roman";
      var props = [
        '<w:rFonts w:ascii="' + xmlAttr(westernFont) + '" w:hAnsi="' + xmlAttr(westernFont) +
        '" w:eastAsia="' + xmlAttr(eastAsiaFont) + '" w:cs="' + xmlAttr(westernFont) + '"/>'
      ];
      if (options.bold) props.push("<w:b/>");
      if (options.italic) props.push("<w:i/>");
      if (options.code) props.push('<w:sz w:val="21"/><w:shd w:val="clear" w:color="auto" w:fill="F3F3F3"/>');
      return "<w:r><w:rPr>" + props.join("") + '</w:rPr><w:t xml:space="preserve">' + xmlText(group.text) + "</w:t></w:r>";
    }).join("");
  }
  function mkszyjCitation(citation) {
    var formats = citation && citation.citations;
    return String((formats && formats.mkszyj) || (citation && citation.citation) || pickCite(citation || {}) || "").replace(/^\s*\[\d+\]\s*/, "").trim();
  }
  function wordCitationMap(message) {
    var map = {};
    (Array.isArray(message.citations) ? message.citations : []).forEach(function (citation, index) {
      var number = String(citation.grounding_index || citation.review_index || (index + 1));
      var text = mkszyjCitation(citation);
      if (text) map[number] = text;
    });
    return map;
  }
  function wordNoteReferenceXml(id, kind) {
    var tag = kind === "endnote" ? "endnoteReference" : "footnoteReference";
    var style = kind === "endnote" ? "EndnoteReference" : "FootnoteReference";
    if (kind === "endnote") {
      // 尾注版用明文 [1][2] 和文末注释段落。Word 与 LibreOffice 对自定义真尾注
      // 标记的兼容结果不一致，会出现「¹[1]」重号；明文方案可保证学术排版始终只显示 [N]。
      return wordRunXml("[" + id + "]");
    }
    return '<w:r><w:rPr><w:rStyle w:val="' + style + '"/></w:rPr><w:' + tag + ' w:id="' + id + '"/></w:r>';
  }
  function wordInlineXml(text, state, runOptions) {
    runOptions = runOptions || {};
    var formatted = wordNormalizeChinesePunctuation(String(text || "")), plain = "", positions = [];
    for (var i=0;i<formatted.length;i++) {
      if (formatted[i] === "*" || formatted[i] === "_") continue;
      plain += formatted[i]; positions.push(i);
    }
    var quoteRanges = verifiedQuoteRanges(plain, state.message || {}, runOptions.quoteBlock).map(function (r) {
      return [positions[r[0]],positions[r[1]-1]+1];
    });
    function run(value, extras, offset) {
      var opts = Object.assign({}, runOptions, extras || {});
      if (offset === undefined || opts.code) return wordRunXml(value, opts);
      var parts = [], cursor = 0;
      quoteRanges.forEach(function (r) {
        var a = Math.max(0, r[0]-offset), b = Math.min(value.length, r[1]-offset);
        if (a >= b) return;
        if (a > cursor) parts.push(wordRunXml(value.slice(cursor,a),opts));
        parts.push(wordRunXml(value.slice(a,b),Object.assign({},opts,{eastAsiaFont:"楷体"})));
        cursor=b;
      });
      if (cursor < value.length) parts.push(wordRunXml(value.slice(cursor),opts));
      return parts.join("");
    }
    // 先在整段文本上处理标点，避免引号被粗体、链接或注释标记拆成多个运行后无法成对识别。
    var source = wordNormalizeChinesePunctuation(String(text || "")), out = [], cursor = 0;
    var re = /(\[[^\]]+\]\([^)]+\))|(\*\*[^*]+\*\*)|(`[^`]+`)|(\*[^*]+\*)|(\[\d+\])/g, match;
    while ((match = re.exec(source))) {
      if (match.index > cursor) out.push(run(source.slice(cursor, match.index), null, cursor));
      var token = match[0], link = token.match(/^\[([^\]]+)\]\(([^)]+)\)$/), citation = token.match(/^\[(\d+)\]$/);
      if (link) out.push(run(link[1]));
      else if (citation && state.citationMap[citation[1]]) {
        var noteId = state.notes.length + 1;
        state.notes.push({ id: noteId, text: state.citationMap[citation[1]] });
        out.push(wordNoteReferenceXml(noteId, state.kind));
      } else if (/^\*\*/.test(token)) out.push(run(token.slice(2, -2), { bold: true }, match.index+2));
      else if (/^`/.test(token)) out.push(run(token.slice(1, -1), { code: true, eastAsiaFont: "仿宋" }));
      else if (/^\*/.test(token)) out.push(run(token.slice(1, -1), { italic: true }, match.index+1));
      else out.push(run(token));
      cursor = re.lastIndex;
    }
    if (cursor < source.length) out.push(run(source.slice(cursor), null, cursor));
    return out.join("") || run("");
  }
  function wordParagraphXml(text, state, style, options) {
    options = options || {};
    var ppr = ['<w:pStyle w:val="' + (style || "Normal") + '"/>'];
    if (options.numId) ppr.push('<w:numPr><w:ilvl w:val="0"/><w:numId w:val="' + options.numId + '"/></w:numPr>');
    if (options.keepNext) ppr.push("<w:keepNext/>");
    var eastAsiaFont = style === "Title" ? "黑体" : (style === "Code" ? "仿宋" : "宋体");
    var inlineOptions = { eastAsiaFont: eastAsiaFont, quoteBlock: style === "Quote" };
    return "<w:p><w:pPr>" + ppr.join("") + "</w:pPr>" +
      (options.literal
        ? wordRunXml(text, Object.assign({}, inlineOptions, options.literal))
        : wordInlineXml(text, state, inlineOptions)) + "</w:p>";
  }
  function parseWordBlocks(markdown) {
    var lines = stripInlineCitations(markdown || "").replace(/\r\n?/g, "\n").split("\n"), blocks = [], paragraph = [];
    function flushParagraph() { if (paragraph.length) { blocks.push({ type: "p", text: paragraph.join(" ") }); paragraph = []; } }
    for (var i = 0; i < lines.length; i++) {
      var raw = lines[i], trimmed = raw.trim();
      if (!trimmed) { flushParagraph(); continue; }
      var fence = trimmed.match(/^(`{3,}|~{3,})/);
      if (fence) {
        flushParagraph(); var code = [], marker = fence[1].charAt(0), length = fence[1].length; i += 1;
        while (i < lines.length && lines[i].trim().indexOf(marker.repeat(length)) !== 0) { code.push(lines[i]); i += 1; }
        blocks.push({ type: "code", text: code.join("\n") }); continue;
      }
      var header = splitMarkdownTableRow(trimmed), separator = lines[i + 1] || "";
      if (header && isMarkdownTableSeparator(separator)) {
        flushParagraph(); var rows = []; i += 2;
        while (i < lines.length) {
          var cells = splitMarkdownTableRow(lines[i].trim());
          if (!cells || isMarkdownTableSeparator(lines[i].trim())) break;
          rows.push(cells); i += 1;
        }
        i -= 1; blocks.push({ type: "table", header: header, rows: rows }); continue;
      }
      var atx = trimmed.replace(/^\*\*(.+?)\*\*$/, "$1").match(/^(#{1,6})\s+(.+)$/);
      if (atx) { flushParagraph(); blocks.push({ type: "h", level: Math.min(3, atx[1].length), text: atx[2].replace(/\s*#+\s*$/, "") }); continue; }
      if (/^([-*_])\s*\1\s*\1(?:\s*\1)*$/.test(trimmed)) { flushParagraph(); continue; }
      var quote = trimmed.match(/^[>＞]\s*(.+)$/), unordered = trimmed.match(/^[-*+]\s+(.+)$/), ordered = trimmed.match(/^\d+[.)]\s+(.+)$/);
      if (quote) {
        flushParagraph();
        if (i > 0 && /^[>＞]\s*/.test(lines[i-1].trim()) && blocks.length && blocks[blocks.length-1].type === "quote") blocks[blocks.length-1].text += " " + quote[1];
        else blocks.push({ type: "quote", text: quote[1] });
        continue;
      }
      if (unordered || ordered) { flushParagraph(); blocks.push({ type: "list", ordered: !!ordered, text: (ordered || unordered)[1] }); continue; }
      paragraph.push(trimmed);
    }
    flushParagraph(); return blocks;
  }
  function wordTableWidths(header, rows) {
    var count = Math.max(1, header.length), weights = [], totalWeight = 0, total = 9638;
    for (var c = 0; c < count; c++) {
      var max = String(header[c] || "").length;
      rows.forEach(function (row) { max = Math.max(max, String(row[c] || "").length); });
      var weight = Math.max(6, Math.min(30, max)); weights.push(weight); totalWeight += weight;
    }
    var widths = [], used = 0;
    for (var i = 0; i < count; i++) { var width = i === count - 1 ? total - used : Math.round(total * weights[i] / totalWeight); widths.push(width); used += width; }
    return widths;
  }
  function wordTableXml(block, state) {
    var count = Math.max(1, block.header.length), rows = [block.header].concat(block.rows || []), widths = wordTableWidths(block.header, block.rows || []);
    var grid = widths.map(function (width) { return '<w:gridCol w:w="' + width + '"/>'; }).join("");
    var rowXml = rows.map(function (row, rowIndex) {
      var cells = [];
      for (var c = 0; c < count; c++) {
        cells.push('<w:tc><w:tcPr><w:tcW w:w="' + widths[c] + '" w:type="dxa"/>' + (rowIndex === 0 ? '<w:shd w:val="clear" w:color="auto" w:fill="F2F2F2"/>' : '') +
          '</w:tcPr>' + wordParagraphXml(rowIndex === 0 ? ("**" + (row[c] || "") + "**") : (row[c] || ""), state, "TableText") + '</w:tc>');
      }
      return '<w:tr>' + (rowIndex === 0 ? '<w:trPr><w:tblHeader/></w:trPr>' : '') + cells.join("") + '</w:tr>';
    }).join("");
    return '<w:tbl><w:tblPr><w:tblW w:w="9638" w:type="dxa"/><w:tblInd w:w="0" w:type="dxa"/>' +
      '<w:tblBorders><w:top w:val="single" w:sz="4" w:color="B7B7B7"/><w:left w:val="single" w:sz="4" w:color="B7B7B7"/><w:bottom w:val="single" w:sz="4" w:color="B7B7B7"/><w:right w:val="single" w:sz="4" w:color="B7B7B7"/><w:insideH w:val="single" w:sz="4" w:color="D0D0D0"/><w:insideV w:val="single" w:sz="4" w:color="D0D0D0"/></w:tblBorders>' +
      '<w:tblLayout w:type="fixed"/><w:tblCellMar><w:top w:w="80" w:type="dxa"/><w:start w:w="120" w:type="dxa"/><w:bottom w:w="80" w:type="dxa"/><w:end w:w="120" w:type="dxa"/></w:tblCellMar></w:tblPr><w:tblGrid>' + grid + '</w:tblGrid>' + rowXml + '</w:tbl>';
  }
  function wordBodyXml(message, state) {
    return parseWordBlocks(message.content || "").map(function (block) {
      if (block.type === "h") return wordParagraphXml(block.text, state, "Heading" + block.level, { keepNext: true });
      if (block.type === "list") return wordParagraphXml(block.text, state, "ListParagraph", { numId: block.ordered ? 2 : 1 });
      if (block.type === "quote") return wordParagraphXml(block.text, state, "Quote");
      if (block.type === "code") return wordParagraphXml(block.text, state, "Code", { literal: { code: true } });
      if (block.type === "table") return wordTableXml(block, state);
      return wordParagraphXml(block.text, state, "Normal");
    }).join("");
  }
  function wordStylesXml() {
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">' +
      '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:eastAsia="宋体"/><w:sz w:val="24"/><w:szCs w:val="24"/><w:lang w:val="zh-CN" w:eastAsia="zh-CN"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="0" w:line="360" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>' +
      '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="正文"/><w:qFormat/><w:pPr><w:spacing w:before="0" w:after="0" w:line="360" w:lineRule="auto"/><w:ind w:firstLine="480"/><w:jc w:val="both"/></w:pPr><w:rPr><w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:eastAsia="宋体"/><w:sz w:val="24"/></w:rPr></w:style>' +
      '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="标题"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="0" w:after="360" w:line="360" w:lineRule="auto"/><w:ind w:firstLine="0"/><w:jc w:val="center"/></w:pPr><w:rPr><w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:eastAsia="黑体"/><w:b/><w:sz w:val="32"/></w:rPr></w:style>' +
      '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="一级标题"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="280" w:after="140" w:line="336" w:lineRule="auto"/><w:ind w:firstLine="0"/><w:jc w:val="center"/></w:pPr><w:rPr><w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:eastAsia="宋体"/><w:b/><w:sz w:val="28"/></w:rPr></w:style>' +
      '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="二级标题"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="240" w:after="100" w:line="320" w:lineRule="auto"/><w:ind w:firstLine="0"/></w:pPr><w:rPr><w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:eastAsia="宋体"/><w:b/><w:sz w:val="24"/></w:rPr></w:style>' +
      '<w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="三级标题"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="180" w:after="80" w:line="320" w:lineRule="auto"/><w:ind w:firstLine="0"/></w:pPr><w:rPr><w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:eastAsia="宋体"/><w:b/><w:sz w:val="24"/></w:rPr></w:style>' +
      '<w:style w:type="paragraph" w:styleId="ListParagraph"><w:name w:val="列表正文"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:after="0" w:line="360" w:lineRule="auto"/><w:ind w:firstLine="0"/></w:pPr></w:style>' +
      '<w:style w:type="paragraph" w:styleId="Quote"><w:name w:val="引文"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:before="80" w:after="80" w:line="336" w:lineRule="auto"/><w:ind w:left="480" w:right="480" w:firstLine="0"/></w:pPr></w:style>' +
      '<w:style w:type="paragraph" w:styleId="Code"><w:name w:val="代码"/><w:basedOn w:val="Normal"/><w:pPr><w:shd w:val="clear" w:color="auto" w:fill="F3F3F3"/><w:spacing w:before="80" w:after="80" w:line="300" w:lineRule="auto"/><w:ind w:left="360" w:right="360" w:firstLine="0"/></w:pPr><w:rPr><w:rFonts w:ascii="Courier New" w:hAnsi="Courier New" w:eastAsia="仿宋"/><w:sz w:val="21"/></w:rPr></w:style>' +
      '<w:style w:type="paragraph" w:styleId="TableText"><w:name w:val="表格正文"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:before="0" w:after="0" w:line="300" w:lineRule="auto"/><w:ind w:firstLine="0"/><w:jc w:val="left"/></w:pPr><w:rPr><w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:eastAsia="宋体"/><w:sz w:val="21"/></w:rPr></w:style>' +
      '<w:style w:type="paragraph" w:styleId="FootnoteText"><w:name w:val="脚注文本"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:after="0" w:line="240" w:lineRule="auto"/><w:ind w:firstLine="0"/><w:jc w:val="both"/></w:pPr><w:rPr><w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:eastAsia="宋体"/><w:sz w:val="15"/></w:rPr></w:style>' +
      '<w:style w:type="paragraph" w:styleId="EndnoteText"><w:name w:val="尾注文本"/><w:basedOn w:val="FootnoteText"/></w:style>' +
      '<w:style w:type="character" w:styleId="FootnoteReference"><w:name w:val="脚注引用"/><w:rPr><w:vertAlign w:val="superscript"/></w:rPr></w:style>' +
      '<w:style w:type="character" w:styleId="EndnoteReference"><w:name w:val="尾注引用"/><w:rPr><w:vertAlign w:val="superscript"/></w:rPr></w:style></w:styles>';
  }
  function wordNumberingXml() {
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">' +
      '<w:abstractNum w:abstractNumId="1"><w:multiLevelType w:val="singleLevel"/><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="•"/><w:lvlJc w:val="left"/><w:pPr><w:tabs><w:tab w:val="num" w:pos="720"/></w:tabs><w:ind w:left="720" w:hanging="360"/></w:pPr><w:rPr><w:rFonts w:ascii="宋体" w:hAnsi="宋体" w:eastAsia="宋体"/></w:rPr></w:lvl></w:abstractNum>' +
      '<w:abstractNum w:abstractNumId="2"><w:multiLevelType w:val="singleLevel"/><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/><w:lvlJc w:val="left"/><w:pPr><w:tabs><w:tab w:val="num" w:pos="720"/></w:tabs><w:ind w:left="720" w:hanging="360"/></w:pPr></w:lvl></w:abstractNum>' +
      '<w:num w:numId="1"><w:abstractNumId w:val="1"/></w:num><w:num w:numId="2"><w:abstractNumId w:val="2"/></w:num></w:numbering>';
  }
  function wordNotesXml(kind, notes) {
    var plural = kind === "endnote" ? "endnotes" : "footnotes", item = kind === "endnote" ? "endnote" : "footnote";
    var ref = kind === "endnote" ? "endnoteRef" : "footnoteRef", style = kind === "endnote" ? "EndnoteText" : "FootnoteText", refStyle = kind === "endnote" ? "EndnoteReference" : "FootnoteReference";
    var entries = '<w:' + item + ' w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:' + item + '>' +
      '<w:' + item + ' w:type="continuationSeparator" w:id="0"><w:p><w:r><w:continuationSeparator/></w:r></w:p></w:' + item + '>';
    entries += notes.map(function (note) {
      var markerAndText = kind === "endnote"
        ? '<w:r><w:rPr><w:rStyle w:val="' + refStyle + '"/><w:color w:val="FFFFFF"/><w:sz w:val="1"/><w:szCs w:val="1"/></w:rPr><w:' + ref + '/></w:r>' +
          wordRunXml("[" + note.id + "] " + note.text)
        : '<w:r><w:rPr><w:rStyle w:val="' + refStyle + '"/></w:rPr><w:' + ref + '/></w:r>' + wordRunXml(" " + note.text);
      return '<w:' + item + ' w:id="' + note.id + '"><w:p><w:pPr><w:pStyle w:val="' + style + '"/></w:pPr>' +
        markerAndText + '</w:p></w:' + item + '>';
    }).join("");
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:' + plural + ' xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">' + entries + '</w:' + plural + '>';
  }
  function wordEndnoteSectionXml(notes) {
    if (!Array.isArray(notes) || !notes.length) return "";
    var separator = '<w:p><w:pPr><w:spacing w:before="180" w:after="80"/><w:pBdr><w:top w:val="single" w:sz="6" w:space="8" w:color="777777"/></w:pBdr></w:pPr></w:p>';
    return separator + notes.map(function (note) {
      return '<w:p><w:pPr><w:pStyle w:val="EndnoteText"/></w:pPr>' + wordRunXml("[" + note.id + "] " + note.text) + "</w:p>";
    }).join("");
  }
  function buildAnswerDocx(message, title, kind) {
    message = numberMessageCitations(message);
    if (kind !== "footnote" && kind !== "endnote") throw new Error("invalid Word note kind");
    var state = { kind: kind, citationMap: wordCitationMap(message), notes: [], message: message };
    var body = wordParagraphXml(title, state, "Title") + wordBodyXml(message, state);
    var isEndnote = kind === "endnote", notePart = "footnotes";
    if (isEndnote) body += wordEndnoteSectionXml(state.notes);
    var footnoteOptions = '<w:pos w:val="pageBottom"/><w:numFmt w:val="decimalEnclosedCircle"/><w:numRestart w:val="eachPage"/>';
    var noteProperties = isEndnote ? "" : '<w:footnotePr>' + footnoteOptions + '</w:footnotePr>';
    var sect = '<w:sectPr>' + noteProperties + '<w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1417" w:right="1134" w:bottom="1417" w:left="1134" w:header="708" w:footer="708" w:gutter="0"/><w:cols w:space="425"/></w:sectPr>';
    var documentXml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body>' + body + sect + '</w:body></w:document>';
    var footnoteContentType = isEndnote ? "" : '<Override PartName="/word/footnotes.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"/>';
    var contentTypes = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/><Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/><Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>' + footnoteContentType + '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/><Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/></Types>';
    var rootRels = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/><Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/></Relationships>';
    var footnoteRelationship = isEndnote ? "" : '<Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes" Target="footnotes.xml"/>';
    var docRels = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/><Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings" Target="settings.xml"/>' + footnoteRelationship + '</Relationships>';
    var packageMismatch = isEndnote
      ? (documentXml.indexOf("footnoteReference") >= 0 || documentXml.indexOf("endnoteReference") >= 0 ||
         docRels.indexOf("/relationships/footnotes") >= 0 || docRels.indexOf("/relationships/endnotes") >= 0 ||
         (state.notes.length && documentXml.indexOf('<w:pStyle w:val="EndnoteText"/>') < 0))
      : (documentXml.indexOf("endnoteReference") >= 0 || docRels.indexOf("/relationships/footnotes") < 0 ||
         contentTypes.indexOf('PartName="/word/footnotes.xml"') < 0 ||
         (state.notes.length && documentXml.indexOf("<w:footnoteReference") < 0));
    if (packageMismatch) {
      throw new Error("Word note package kind mismatch");
    }
    var settings = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:defaultTabStop w:val="420"/><w:characterSpacingControl w:val="doNotCompress"/><w:compat><w:compatSetting w:name="compatibilityMode" w:uri="http://schemas.microsoft.com/office/word" w:val="15"/></w:compat></w:settings>';
    var created = new Date().toISOString();
    var core = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><dc:title>' + xmlText(title) + '</dc:title><dc:creator>AI研究对话用户</dc:creator><cp:lastModifiedBy>AI研究对话用户</cp:lastModifiedBy><dcterms:created xsi:type="dcterms:W3CDTF">' + created + '</dcterms:created><dcterms:modified xsi:type="dcterms:W3CDTF">' + created + '</dcterms:modified></cp:coreProperties>';
    var app = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"><Application>AI研究对话</Application><AppVersion>1.0</AppVersion></Properties>';
    var zip = new StoreZip();
    zip.add("[Content_Types].xml", contentTypes); zip.add("_rels/.rels", rootRels); zip.add("docProps/core.xml", core); zip.add("docProps/app.xml", app);
    zip.add("word/document.xml", documentXml); zip.add("word/_rels/document.xml.rels", docRels); zip.add("word/styles.xml", wordStylesXml());
    zip.add("word/numbering.xml", wordNumberingXml()); zip.add("word/settings.xml", settings);
    if (!isEndnote) zip.add("word/footnotes.xml", wordNotesXml("footnote", state.notes));
    return zip.blob("application/vnd.openxmlformats-officedocument.wordprocessingml.document");
  }
  function precedingQuestion(index) {
    for (var i = Number(index) - 1; i >= 0; i--) if (messages[i] && messages[i].role === "user") return messages[i].content || "";
    return "";
  }
  function exportAnswerWord(index, kind, button) {
    if (kind !== "footnote" && kind !== "endnote") { setCloudStatus("Word 导出类型无效，请刷新后重试。", "warn"); return; }
    index = Number(index); var message = messages[index];
    if (!message || message.role !== "assistant" || message.pending || !String(message.content || "").trim()) return;
    var session = currentSession(), fallback = session ? (session.title || "AI研究回答") : "AI研究回答";
    var title = plainTitle(precedingQuestion(index), fallback), label = kind === "endnote" ? "尾注版" : "脚注版";
    var oldText = button ? button.textContent : "";
    if (button) { button.disabled = true; button.textContent = "生成中…"; }
    setTimeout(function () {
      try {
        downloadBlob(buildAnswerDocx(message, title, kind), safeFilePart(title, "AI研究回答") + "_" + label + ".docx");
        setCloudStatus("已在本地生成该条 AI 回复的 Word " + label + "。", "ok");
      } catch (_) {
        setCloudStatus("Word 文件生成失败，请刷新页面后重试。", "warn");
      } finally {
        if (button) { button.disabled = false; button.textContent = oldText; }
      }
    }, 30);
  }

  // 整页滚动模式：发问后把「本次提问」滚到顶部，答案在其下方自上而下平铺，便于从头读长综述。
  function scrollToLatestQuestion() {
    var nodes = messagesEl.querySelectorAll(".msg.user");
    var last = nodes[nodes.length - 1];
    if (last && last.scrollIntoView) last.scrollIntoView({ block: "start", behavior: "smooth" });
  }
  // ===================== 本地会话记录（按 uid 隔离，类主流 AI 应用侧栏） =====================
  function nowTs() { return Date.now(); }
  function genId() { return "s" + Date.now().toString(36) + Math.random().toString(36).slice(2, 7); }
  function storeSlot() { return aiUid || "_guest"; }
  function dbSessionKey(id) { return storeSlot() + "::" + id; }
  function dbMigrationKey() { return "migration::" + storeSlot(); }
  function tabCurrentKey() { return "marx-ai-current-session-v3:" + storeSlot(); }
  function loadStore() { try { return JSON.parse(localStorage.getItem(AI_SESSIONS_KEY) || "{}") || {}; } catch (_) { return {}; } }
  function showStorageWarning(text) {
    if (!storageWarningEl || !text) return;
    storageWarningEl.textContent = text;
    storageWarningEl.hidden = false;
  }
  function rememberCurrent() {
    try { sessionStorage.setItem(tabCurrentKey(), sessionsData.current || ""); } catch (_) {}
  }
  function requestedCurrent() {
    try { return sessionStorage.getItem(tabCurrentKey()) || ""; } catch (_) { return ""; }
  }
  function emergencyBackupKey(id) { return AI_BACKUP_PREFIX + encodeURIComponent(storeSlot()) + ":" + encodeURIComponent(id); }
  function loadEmergencyBackupIndex() {
    try { return JSON.parse(localStorage.getItem(AI_BACKUP_INDEX_KEY) || "{}") || {}; } catch (_) { return {}; }
  }
  function saveEmergencyBackupIndex(index) {
    try { localStorage.setItem(AI_BACKUP_INDEX_KEY, JSON.stringify(index || {})); return true; } catch (_) { return false; }
  }
  function pruneEmergencyBackups(keepId, maxCount) {
    var index = loadEmergencyBackupIndex();
    var mine = Array.isArray(index[storeSlot()]) ? index[storeSlot()].slice() : [];
    mine.sort(function (a, b) { return Number(b.updatedAt || 0) - Number(a.updatedAt || 0); });
    var kept = [], removed = [];
    mine.forEach(function (item) {
      if (item.id === keepId || kept.length < maxCount) kept.push(item); else removed.push(item);
    });
    removed.forEach(function (item) { try { localStorage.removeItem(emergencyBackupKey(item.id)); } catch (_) {} });
    index[storeSlot()] = kept;
    saveEmergencyBackupIndex(index);
  }
  function writeEmergencyTextBackup(session) {
    if (!session || !session.id) return false;
    var record = {
      id: session.id, title: session.title || deriveTitle(session.messages || []),
      updatedAt: Number(session.updatedAt || nowTs()), clearedAt: Number(session.clearedAt || 0),
      titleUpdatedAt: Number(session.titleUpdatedAt || session.updatedAt || nowTs()), titleManual: !!session.titleManual,
      messages: storedMessages(session.messages || [], 3), storageLevel: 3,
    };
    var index = loadEmergencyBackupIndex();
    var mine = Array.isArray(index[storeSlot()]) ? index[storeSlot()].filter(function (x) { return x.id !== session.id; }) : [];
    mine.unshift({ id: session.id, updatedAt: record.updatedAt });
    index[storeSlot()] = mine.slice(0, 20);
    try {
      localStorage.setItem(emergencyBackupKey(session.id), JSON.stringify(record));
      saveEmergencyBackupIndex(index);
      pruneEmergencyBackups(session.id, 12);
      return true;
    } catch (_) {
      // 先清掉旧版“整库正文副本”和最旧应急副本，给当前会话留出同步落盘空间。
      try { localStorage.removeItem(AI_SESSIONS_KEY); } catch (_) {}
      pruneEmergencyBackups(session.id, 4);
      try {
        localStorage.setItem(emergencyBackupKey(session.id), JSON.stringify(record));
        index[storeSlot()] = [{ id: session.id, updatedAt: record.updatedAt }];
        saveEmergencyBackupIndex(index);
        return true;
      } catch (_) {
        showStorageWarning("浏览器已无法写入本机应急副本；请立即复制当前回答，并使用“快捷清理”释放空间。");
        return false;
      }
    }
  }
  function readEmergencyTextBackups() {
    var index = loadEmergencyBackupIndex();
    var mine = Array.isArray(index[storeSlot()]) ? index[storeSlot()] : [];
    var out = [];
    mine.forEach(function (item) {
      try {
        var record = JSON.parse(localStorage.getItem(emergencyBackupKey(item.id)) || "null");
        if (record && record.id && Array.isArray(record.messages)) out.push(record);
      } catch (_) {}
    });
    return out;
  }
  function removeEmergencyBackup(id) {
    try { localStorage.removeItem(emergencyBackupKey(id)); } catch (_) {}
    var index = loadEmergencyBackupIndex();
    if (Array.isArray(index[storeSlot()])) {
      index[storeSlot()] = index[storeSlot()].filter(function (item) { return item.id !== id; });
      saveEmergencyBackupIndex(index);
    }
  }
  function loadCloudDeleteOutbox() {
    try { return JSON.parse(localStorage.getItem(AI_CLOUD_DELETE_OUTBOX_KEY) || "{}") || {}; } catch (_) { return {}; }
  }
  function queueCloudDelete(id) {
    var box = loadCloudDeleteOutbox();
    var mine = box[storeSlot()] || {};
    mine[id] = nowTs(); box[storeSlot()] = mine;
    try { localStorage.setItem(AI_CLOUD_DELETE_OUTBOX_KEY, JSON.stringify(box)); } catch (_) {}
  }
  function clearCloudDelete(id) {
    var box = loadCloudDeleteOutbox();
    var mine = box[storeSlot()] || {};
    delete mine[id]; box[storeSlot()] = mine;
    try { localStorage.setItem(AI_CLOUD_DELETE_OUTBOX_KEY, JSON.stringify(box)); } catch (_) {}
  }
  function checkStorageCapacity() {
    if (!navigator.storage || !navigator.storage.estimate) return Promise.resolve();
    return navigator.storage.estimate().then(function (estimate) {
      var usage = Number(estimate.usage || 0), quota = Number(estimate.quota || 0);
      if (!quota || !storageUsageEl) return;
      var ratio = Math.max(0, Math.min(1, usage / quota));
      storageUsageEl.hidden = false;
      storageUsageEl.classList.toggle("warn", ratio >= .75 && ratio < .9);
      storageUsageEl.classList.toggle("danger", ratio >= .9);
      if (storageUsageFill) storageUsageFill.style.width = Math.max(2, Math.round(ratio * 100)) + "%";
      if (storageUsageText) storageUsageText.textContent = "本浏览器网站存储已使用 " + Math.round(ratio * 100) + "%";
      if (ratio >= .75) showStorageWarning(
        ratio >= .9
          ? "本浏览器的网站存储已接近上限，请尽快使用“快捷清理”删除较早会话。"
          : "本浏览器的网站存储使用较高，建议适时清理不再需要的较早会话。"
      );
    }).catch(function () {});
  }
  function currentSession() {
    for (var i = 0; i < sessionsData.sessions.length; i++) if (sessionsData.sessions[i].id === sessionsData.current) return sessionsData.sessions[i];
    return null;
  }
  function deriveTitle(msgs) {
    for (var i = 0; i < msgs.length; i++) {
      if (msgs[i].role === "user" && msgs[i].content) {
        var t = String(msgs[i].content).replace(/\s+/g, " ").trim();
        return t.length > 22 ? t.slice(0, 22) + "…" : t;
      }
    }
    return "新会话";
  }
  function normalizeSessionRecord(record) {
    if (!record || !record.id) return record;
    // 老版记录可能没有 _mid。用「会话 id + 原始顺序」生成稳定身份，避免
    // 相同文本的两条消息被合并为一条，也避免不同标签页各自生成随机 id 后重复。
    (Array.isArray(record.messages) ? record.messages : []).forEach(function (message, index) {
      if (!message || typeof message !== "object") return;
      if (!message._mid) message._mid = "legacy-" + String(record.id) + "-" + String(index);
      if (!message._createdAt) {
        var base = Number(record.updatedAt || nowTs()) - Math.max(0, (record.messages.length - index) * 2);
        message._createdAt = base + index;
      }
    });
    var automatic = deriveTitle(record.messages || []);
    if (!record.title) record.title = automatic;
    if (!record.titleUpdatedAt) record.titleUpdatedAt = Number(record.updatedAt || nowTs());
    if (record.titleManual === undefined) record.titleManual = !!(record.title !== automatic && record.title !== "新会话");
    record.clearedAt = Number(record.clearedAt || 0);
    record.expiresAt = Number(record.expiresAt || 0);
    if (!record.cloudState && record.expiresAt) record.cloudState = "backed_up";
    return record;
  }

  function openSessionDb() {
    return new Promise(function (resolve, reject) {
      if (!window.indexedDB) { reject(new Error("IndexedDB unavailable")); return; }
      var req = window.indexedDB.open(AI_DB_NAME, AI_DB_VERSION);
      req.onupgradeneeded = function () {
        var db = req.result;
        if (!db.objectStoreNames.contains(AI_DB_SESSION_STORE)) {
          var sessions = db.createObjectStore(AI_DB_SESSION_STORE, { keyPath: "key" });
          sessions.createIndex("uid", "uid", { unique: false });
        }
        if (!db.objectStoreNames.contains(AI_DB_META_STORE)) {
          db.createObjectStore(AI_DB_META_STORE, { keyPath: "key" });
        }
      };
      req.onsuccess = function () {
        var db = req.result;
        db.onversionchange = function () { try { db.close(); } catch (_) {} };
        resolve(db);
      };
      req.onerror = function () { reject(req.error || new Error("IndexedDB open failed")); };
      req.onblocked = function () { reject(new Error("IndexedDB upgrade blocked")); };
    });
  }
  function messageIdentity(message) {
    if (message && message._mid) return "id:" + message._mid;
    return "legacy:" + String((message && message.role) || "") + "|" +
      String((message && message.kind) || "") + "|" + String((message && message.content) || "");
  }
  function ensureMessageIdentity(message) {
    if (!message || typeof message !== "object") return message;
    if (!message._mid) message._mid = sessionTabId + "-" + nowTs().toString(36) + "-" + (++sessionMessageSeq).toString(36);
    if (!message._createdAt) message._createdAt = nowTs() + sessionMessageSeq;
    return message;
  }
  function payloadScore(value) {
    if (value === undefined || value === null) return -1;
    try { return JSON.stringify(value).length; } catch (_) { return 0; }
  }
  function mergeMessageVersions(existing, incoming) {
    if (!existing || typeof existing !== "object") return incoming;
    if (!incoming || typeof incoming !== "object") return existing;
    var merged = Object.assign({}, existing, incoming);
    // 消息正文是不可变的；同一 _mid 的多份副本中，引文、来源和警告必须保留
    // 信息量更大的版本，不得让纯正文应急副本或云端精简副本反向覆盖。
    ["citations", "sources", "warnings"].forEach(function (key) {
      if (payloadScore(existing[key]) > payloadScore(incoming[key])) merged[key] = existing[key];
    });
    if (payloadScore(existing.groundingScope) > payloadScore(incoming.groundingScope)) merged.groundingScope = existing.groundingScope;
    if (existing._mid) merged._mid = existing._mid;
    var oldCreated = Number(existing._createdAt || 0), newCreated = Number(incoming._createdAt || 0);
    if (oldCreated && newCreated) merged._createdAt = Math.min(oldCreated, newCreated);
    else merged._createdAt = oldCreated || newCreated || merged._createdAt;
    return merged;
  }
  function mergeMessages(existing, incoming, clearedAt) {
    var oldList = Array.isArray(existing) ? existing.slice() : [];
    var newList = Array.isArray(incoming) ? incoming.slice() : [];
    if (!oldList.length) return newList.filter(function (m) { return !(clearedAt && Number(m && m._createdAt || 0) <= clearedAt); });
    if (!newList.length) return oldList.filter(function (m) { return !(clearedAt && Number(m && m._createdAt || 0) <= clearedAt); });
    var common = 0;
    while (common < oldList.length && common < newList.length &&
           messageIdentity(oldList[common]) === messageIdentity(newList[common])) {
      oldList[common] = mergeMessageVersions(oldList[common], newList[common]);
      common++;
    }
    var out = oldList.slice();
    var positions = {};
    for (var i = 0; i < out.length; i++) positions[messageIdentity(out[i])] = i;
    var start = common > 0 ? common : 0;
    for (var j = start; j < newList.length; j++) {
      var key = messageIdentity(newList[j]);
      if (positions[key] !== undefined) out[positions[key]] = mergeMessageVersions(out[positions[key]], newList[j]);
      else { positions[key] = out.length; out.push(newList[j]); }
    }
    return out.filter(function (m) { return !(clearedAt && Number(m && m._createdAt || 0) <= clearedAt); });
  }
  function compactCitation(citation, textOnly) {
    if (!citation || typeof citation !== "object") return null;
    // 最紧凑层仍保留引文序号、三种出处格式和原文链接；只省略占空间较大的
    // 上下文与 evidence。这样容量降级后仍能显示「引用原文」索引和引用格式选择。
    var keep = ["grounding_index", "review_index", "citation", "citations", "viewer_url", "book", "volume",
      "title", "source_file", "pdf_page", "pdf_pages", "printed_page", "subject_label", "review_quoted", "review_quote_unmatched",
      "document_id", "work_title", "work_authors", "provenance_verified", "kind", "quote", "text_verified", "location_status", "printed_pages", "page_refs", "page_location", "quote_page_verified", "candidate_pdf_pages"];
    var out = {};
    keep.forEach(function (key) { if (citation[key] !== undefined) out[key] = citation[key]; });
    if (!textOnly && citation.context) out.context = String(citation.context).slice(0, 360);
    if (Array.isArray(citation.evidence)) {
      out.evidence = citation.evidence.map(function (ev) {
        return compactCitation(ev, textOnly) || {};
      });
    }
    return out;
  }
  function compactSource(source) {
    if (!source || typeof source !== "object") return null;
    var out = {};
    ["title", "link", "url", "citation", "site", "site_name", "date"].forEach(function (key) {
      if (source[key] !== undefined) out[key] = source[key];
    });
    if (!out.link && out.url) out.link = out.url;
    if (!out.site && out.site_name) out.site = out.site_name;
    return out;
  }
  function storedMessages(list, level) {
    return (Array.isArray(list) ? list : []).filter(function (m) { return m && !m.pending; }).map(function (message) {
      ensureMessageIdentity(message);
      if (level === 0) return Object.assign({}, message);
      var out = {
        role: message.role,
        content: String(message.content || ""),
        kind: message.kind,
        _mid: message._mid,
        _createdAt: message._createdAt,
      };
      if (message.groundingScope) out.groundingScope = message.groundingScope;
      if (Array.isArray(message.warnings) && message.warnings.length) out.warnings = message.warnings.slice(0, 8);
      if (level === 1 || level === 2) {
        if (Array.isArray(message.citations)) out.citations = message.citations.map(function (c) { return compactCitation(c, level === 2); }).filter(Boolean);
        if (Array.isArray(message.sources)) out.sources = message.sources.map(compactSource).filter(Boolean);
      }
      return out;
    });
  }
  function isQuotaError(error) {
    var name = String((error && error.name) || "");
    var text = String((error && error.message) || error || "");
    return name === "QuotaExceededError" || /quota|space|storage/i.test(text);
  }
  function readDbSessions() {
    return new Promise(function (resolve, reject) {
      var tx = sessionDb.transaction(AI_DB_SESSION_STORE, "readonly");
      var index = tx.objectStore(AI_DB_SESSION_STORE).index("uid");
      var req = index.getAll(window.IDBKeyRange.only(storeSlot()));
      req.onsuccess = function () { resolve((req.result || []).filter(function (record) { return !record.deletedAt; })); };
      req.onerror = function () { reject(req.error || tx.error || new Error("IndexedDB read failed")); };
    });
  }
  function migrateLegacySessions() {
    return new Promise(function (resolve, reject) {
      var tx = sessionDb.transaction([AI_DB_SESSION_STORE, AI_DB_META_STORE], "readwrite");
      var sessionStore = tx.objectStore(AI_DB_SESSION_STORE);
      var metaStore = tx.objectStore(AI_DB_META_STORE);
      var metaReq = metaStore.get(dbMigrationKey());
      metaReq.onsuccess = function () {
        if (metaReq.result && metaReq.result.done) return;
        var legacyStore = loadStore();
        var mine = legacyStore[storeSlot()];
        var imported = [];
        if (mine && Array.isArray(mine.sessions)) {
          mine.sessions.forEach(function (s) {
            if (!s || !s.id) return;
            imported.push({
              key: dbSessionKey(s.id), uid: storeSlot(), id: s.id,
              title: s.title || deriveTitle(s.messages || []), updatedAt: Number(s.updatedAt || nowTs()),
              clearedAt: Number(s.clearedAt || 0), titleUpdatedAt: Number(s.titleUpdatedAt || s.updatedAt || nowTs()),
              titleManual: s.titleManual !== undefined ? !!s.titleManual : (String(s.title || "") !== deriveTitle(s.messages || [])),
              messages: storedMessages(s.messages || [], 0), storageLevel: 0,
            });
          });
        }
        var oldThread = [];
        try { oldThread = (JSON.parse(localStorage.getItem(AI_THREAD_KEY) || "[]") || []).filter(function (m) { return m && !m.pending; }); } catch (_) {}
        if (oldThread.length) {
          var oldSig = oldThread.map(messageIdentity).join("\n");
          var already = imported.some(function (s) { return (s.messages || []).map(messageIdentity).join("\n") === oldSig; });
          if (!already) {
            var recoveredId = "legacy-" + nowTs().toString(36);
            imported.push({
              key: dbSessionKey(recoveredId), uid: storeSlot(), id: recoveredId,
              title: "恢复的旧会话 · " + deriveTitle(oldThread), updatedAt: nowTs() - 1, clearedAt: 0,
              titleUpdatedAt: nowTs() - 1, titleManual: true,
              messages: storedMessages(oldThread, 0), storageLevel: 0,
            });
          }
        }
        imported.forEach(function (record) { sessionStore.put(record); });
        metaStore.put({
          key: dbMigrationKey(), done: true, migratedAt: nowTs(),
          legacyCurrent: mine && mine.current ? mine.current : "",
        });
      };
      tx.oncomplete = function () { resolve(); };
      tx.onerror = function () { reject(tx.error || new Error("IndexedDB migration failed")); };
      tx.onabort = function () { reject(tx.error || new Error("IndexedDB migration aborted")); };
    });
  }
  function readMigrationMeta() {
    return new Promise(function (resolve) {
      var tx = sessionDb.transaction(AI_DB_META_STORE, "readonly");
      var req = tx.objectStore(AI_DB_META_STORE).get(dbMigrationKey());
      req.onsuccess = function () { resolve(req.result || {}); };
      req.onerror = function () { resolve({}); };
    });
  }
  function importEmergencyBackups() {
    var backups = readEmergencyTextBackups();
    if (!backups.length) return Promise.resolve();
    return readDbSessions().then(function (existingRecords) {
      var existingById = {};
      (existingRecords || []).forEach(function (record) { existingById[record.id] = record; });
      return backups.reduce(function (chain, record) {
        return chain.then(function () {
          var existing = existingById[record.id];
          var snapshot = {
            id: record.id, title: record.title || deriveTitle(record.messages || []),
            updatedAt: Number(record.updatedAt || 0), clearedAt: Number(record.clearedAt || 0),
            titleUpdatedAt: Number(record.titleUpdatedAt || record.updatedAt || 0), titleManual: !!record.titleManual,
            messages: record.messages || [],
          };
          if (!existing) {
            // 主存储确实缺失时才用纯正文副本整体恢复。
            return writeDbSession(snapshot, false, 3).catch(function () { return null; });
          }
          var known = {};
          (existing.messages || []).forEach(function (message) { known[messageIdentity(message)] = true; });
          var hasMissingMessages = (snapshot.messages || []).some(function (message) { return !known[messageIdentity(message)]; });
          if (!hasMissingMessages) return null;
          // 应急副本只可补入主存储没来得及落盘的新消息，不得把已有引文降级。
          return writeDbSession(snapshot, false, 0).catch(function () { return null; });
        });
      }, Promise.resolve());
    });
  }
  function writeDbSession(snapshot, replaceMessages, level) {
    return new Promise(function (resolve, reject) {
      var tx = sessionDb.transaction(AI_DB_SESSION_STORE, "readwrite");
      var store = tx.objectStore(AI_DB_SESSION_STORE);
      var req = store.get(dbSessionKey(snapshot.id));
      var saved = null;
      req.onsuccess = function () {
        var existing = req.result || null;
        if (existing && existing.deletedAt) return;  // 另一标签页已删除：旧标签页不得用迟到写入将其复活
        var clearedAt = Math.max(Number(existing && existing.clearedAt || 0), Number(snapshot.clearedAt || 0));
        var merged = replaceMessages && Number(snapshot.clearedAt || 0) >= Number(existing && existing.clearedAt || 0)
          ? snapshot.messages : mergeMessages(existing && existing.messages, snapshot.messages, clearedAt);
        merged = storedMessages(merged, level);
        var existingTitleAt = Number(existing && existing.titleUpdatedAt || existing && existing.updatedAt || 0);
        var snapshotTitleAt = Number(snapshot.titleUpdatedAt || snapshot.updatedAt || 0);
        var snapshotTitleWins = !existing || snapshotTitleAt >= existingTitleAt;
        saved = {
          key: dbSessionKey(snapshot.id), uid: storeSlot(), id: snapshot.id,
          title: snapshotTitleWins ? (snapshot.title || deriveTitle(merged)) : (existing.title || deriveTitle(merged)),
          titleUpdatedAt: Math.max(existingTitleAt, snapshotTitleAt),
          titleManual: snapshotTitleWins ? !!snapshot.titleManual : !!existing.titleManual,
          updatedAt: Math.max(Number(existing && existing.updatedAt || 0), Number(snapshot.updatedAt || nowTs())),
          clearedAt: clearedAt,
          messages: merged, storageLevel: level,
          expiresAt: snapshot.cloudState === "expired" ? 0 : Number(snapshot.expiresAt || existing && existing.expiresAt || 0),
          serverRevision: snapshot.cloudState === "expired" ? 0 : Number(snapshot.serverRevision || existing && existing.serverRevision || 0),
          cloudState: snapshot.cloudState !== undefined ? String(snapshot.cloudState || "") : String(existing && existing.cloudState || ""),
          cloudExpiredAt: Number(snapshot.cloudExpiredAt || existing && existing.cloudExpiredAt || 0),
        };
        store.put(saved);
      };
      tx.oncomplete = function () { resolve(saved); };
      tx.onerror = function () { reject(tx.error || new Error("IndexedDB write failed")); };
      tx.onabort = function () { reject(tx.error || new Error("IndexedDB write aborted")); };
    });
  }
  function persistDbSession(snapshot, replaceMessages) {
    var level = 0;
    function attempt() {
      return writeDbSession(snapshot, replaceMessages, level).catch(function (error) {
        if (isQuotaError(error) && level < 2) { level++; return attempt(); }
        throw error;
      });
    }
    return attempt().then(function (saved) {
      if (saved && saved.storageLevel > 0) {
        showStorageWarning(saved.storageLevel === 1
          ? "浏览器存储空间较紧张：会话正文已完整保存，引文展开上下文已自动精简。"
          : "浏览器存储空间不足：会话正文和引文索引已保存，展开上下文已省略。建议删除不需要的旧会话。"
        );
      }
      return saved;
    });
  }
  function legacySnapshot(level) {
    return {
      current: sessionsData.current,
      sessions: sessionsData.sessions.map(function (s) {
        return {
          id: s.id, title: s.title, updatedAt: s.updatedAt, clearedAt: Number(s.clearedAt || 0),
          titleUpdatedAt: Number(s.titleUpdatedAt || s.updatedAt || 0), titleManual: !!s.titleManual,
          messages: storedMessages(s.messages || [], level), storageLevel: level,
        };
      }),
    };
  }
  function writeLegacyTextBackup(records, currentId) {
    // IndexedDB 正常时不用一个 localStorage 大对象镜像整库（那会再次触发 5MB 配额灾难）。
    // 仅为最近会话逐条保留同步写入的纯正文应急副本，关闭页面前也能可靠落盘。
    var sorted = (records || []).filter(function (r) { return !r.deletedAt; }).slice().sort(function (a, b) {
      if (a.id === currentId) return -1;
      if (b.id === currentId) return 1;
      return Number(b.updatedAt || 0) - Number(a.updatedAt || 0);
    });
    sorted.slice(0, 12).forEach(writeEmergencyTextBackup);
    try { localStorage.removeItem(AI_SESSIONS_KEY); } catch (_) {}
  }
  function persistLegacySafely() {
    var store = loadStore();
    for (var level = 0; level <= 2; level++) {
      try {
        store[storeSlot()] = legacySnapshot(level);
        localStorage.setItem(AI_SESSIONS_KEY, JSON.stringify(store));
      if (level > 0) showStorageWarning("浏览器大容量会话存储不可用；已保留正文并精简引文附件。请勿清理浏览器数据。");
        return true;
      } catch (_) {}
    }
    showStorageWarning("本次会话仍显示在当前页面，但浏览器未能保存。请立即复制正文，并清理不需要的旧会话后重试。");
    return false;
  }
  function notifySessionChange(type, sessionId) {
    var notice = { uid: storeSlot(), type: type, sessionId: sessionId || "", source: sessionTabId, at: nowTs() };
    if (sessionSyncChannel) { try { sessionSyncChannel.postMessage(notice); } catch (_) {} }
    try { localStorage.setItem(AI_SYNC_KEY, JSON.stringify(notice)); } catch (_) {}
  }
  function updateLocalSession(saved) {
    if (!saved) return;
    for (var i = 0; i < sessionsData.sessions.length; i++) {
      if (sessionsData.sessions[i].id === saved.id) {
        sessionsData.sessions[i] = saved;
        if (sessionsData.current === saved.id && !streaming) messages = (saved.messages || []).slice();
        return;
      }
    }
    sessionsData.sessions.unshift(saved);
  }
  function enqueueSessionWrite(session, options) {
    options = options || {};
    var snapshot = {
      id: session.id, title: session.title, updatedAt: session.updatedAt,
      clearedAt: Number(session.clearedAt || 0),
      titleUpdatedAt: Number(session.titleUpdatedAt || session.updatedAt || 0), titleManual: !!session.titleManual,
      expiresAt: Number(session.expiresAt || 0), serverRevision: Number(session.serverRevision || 0),
      cloudState: String(session.cloudState || ""), cloudExpiredAt: Number(session.cloudExpiredAt || 0),
      messages: (session.messages || []).slice(),
    };
    sessionWriteQueue = sessionWriteQueue.catch(function () {}).then(function () {
      if (sessionDb && !sessionDbFailed) return persistDbSession(snapshot, !!options.replaceMessages);
      persistLegacySafely();
      return snapshot;
    }).then(function (saved) {
      if (!saved) {
        showStorageWarning("该会话已在另一个标签页删除，本标签页的迟到写入未覆盖删除结果。");
        removeLocalConversation(snapshot.id, nowTs()); renderSidebar();
        return;
      }
      updateLocalSession(saved);
      writeEmergencyTextBackup(saved);
      enqueueCloudWrite(saved, options);
      notifySessionChange("save", snapshot.id);
      renderSidebar();
    }).catch(function (error) {
      showStorageWarning("会话保存失败：正文仍显示在当前页面，请先复制备份。" + (isQuotaError(error) ? "浏览器存储空间不足。" : "请刷新后重试。"));
    });
    return sessionWriteQueue;
  }
  function requestPersistentStorage() {
    if (requestPersistentStorage.done) return;
    requestPersistentStorage.done = true;
    try { if (navigator.storage && navigator.storage.persist) navigator.storage.persist().catch(function () {}); } catch (_) {}
  }
  // 把当前 messages 存回当前会话。IndexedDB 逐会话事务写入；同会话被多个标签页追加时按消息合并。
  function saveMessages(options) {
    var s = currentSession();
    if (!s) { s = { id: genId(), title: "新会话", updatedAt: nowTs(), titleUpdatedAt: nowTs(), titleManual: false, clearedAt: 0, messages: [] }; sessionsData.sessions.unshift(s); sessionsData.current = s.id; }
    s.messages = messages.filter(function (m) { return m && !m.pending; }).map(ensureMessageIdentity);
    var autoTitle = deriveTitle(s.messages), saveAt = nowTs();
    if (s.titleManual === undefined) s.titleManual = !!(s.title && s.title !== autoTitle && s.title !== "新会话");
    if (!s.titleManual && s.title !== autoTitle) { s.title = autoTitle; s.titleUpdatedAt = saveAt; }
    if (!s.titleUpdatedAt) s.titleUpdatedAt = saveAt;
    s.updatedAt = saveAt;
    rememberCurrent();
    requestPersistentStorage();
    // localStorage 的逐会话纯正文副本是同步写：即使用户马上关闭页面，也不会等异步 IDB 事务。
    writeEmergencyTextBackup(s);
    enqueueSessionWrite(s, options || {});
    checkStorageCapacity();
    renderSidebar();
  }
  function legacyLoadSessions() {
    var store = loadStore();
    var mine = store[storeSlot()];
    if (mine && mine.sessions && mine.sessions.length) {
      mine.sessions = mine.sessions.map(normalizeSessionRecord);
      sessionsData = mine;
    } else {
      var legacy = [];
      try { legacy = (JSON.parse(localStorage.getItem(AI_THREAD_KEY) || "[]") || []).filter(function (m) { return m && !m.pending; }); } catch (_) {}
      var firstAt = nowTs();
      var first = { id: genId(), title: legacy.length ? deriveTitle(legacy) : "新会话", updatedAt: firstAt, titleUpdatedAt: firstAt, titleManual: false, clearedAt: 0, messages: legacy };
      sessionsData = { current: first.id, sessions: [first] };
      persistLegacySafely();
    }
    if (!currentSession() && sessionsData.sessions.length) sessionsData.current = sessionsData.sessions[0].id;
    var cur = currentSession();
    return cur ? (cur.messages || []).slice() : [];
  }
  function loadSessions() {
    return openSessionDb().then(function (db) {
      sessionDb = db;
      return migrateLegacySessions();
    }).then(function () {
      return importEmergencyBackups();
    }).then(function () {
      return Promise.all([readDbSessions(), readMigrationMeta()]);
    }).then(function (parts) {
      var records = parts[0] || [];
      records = records.map(normalizeSessionRecord);
      var meta = parts[1] || {};
      records.sort(function (a, b) { return Number(b.updatedAt || 0) - Number(a.updatedAt || 0); });
      if (!records.length) {
        var firstId = genId();
        var firstAt = nowTs();
        var first = { key: dbSessionKey(firstId), uid: storeSlot(), id: firstId, title: "新会话", updatedAt: firstAt, titleUpdatedAt: firstAt, titleManual: false, clearedAt: 0, messages: [], storageLevel: 0 };
        sessionsData = { current: first.id, sessions: [first] };
        enqueueSessionWrite(first, { replaceMessages: true });
      } else {
        var wanted = requestedCurrent() || meta.legacyCurrent || "";
        sessionsData = { current: wanted, sessions: records };
        if (!currentSession()) sessionsData.current = records[0].id;
      }
      rememberCurrent();
      writeLegacyTextBackup(records.length ? records : sessionsData.sessions, sessionsData.current);
      var cur = currentSession();
      return cur ? (cur.messages || []).slice() : [];
    }).catch(function () {
      sessionDbFailed = true;
      showStorageWarning("浏览器大容量会话存储暂不可用，已进入兼容保存模式；请勿清理浏览器数据。若此提示持续出现，请更换非隐私窗口。 ");
      return legacyLoadSessions();
    });
  }
  function deleteDbSession(id) {
    if (!sessionDb || sessionDbFailed) { persistLegacySafely(); notifySessionChange("delete", id); return Promise.resolve(); }
    sessionWriteQueue = sessionWriteQueue.catch(function () {}).then(function () {
      return new Promise(function (resolve, reject) {
        var tx = sessionDb.transaction(AI_DB_SESSION_STORE, "readwrite");
        tx.objectStore(AI_DB_SESSION_STORE).put({
          key: dbSessionKey(id), uid: storeSlot(), id: id,
          deletedAt: nowTs(), updatedAt: nowTs(), messages: [], title: "",
        });
        tx.oncomplete = resolve;
        tx.onerror = function () { reject(tx.error || new Error("IndexedDB delete failed")); };
      });
    }).then(function () { notifySessionChange("delete", id); });
    return sessionWriteQueue;
  }
  function refreshSessionsFromDb() {
    if (!sessionDb || sessionDbFailed || streaming) return;
    readDbSessions().then(function (records) {
      records.sort(function (a, b) { return Number(b.updatedAt || 0) - Number(a.updatedAt || 0); });
      var current = sessionsData.current;
      sessionsData = { current: current, sessions: records };
      if (!currentSession() && records.length) sessionsData.current = records[0].id;
      var cur = currentSession();
      messages = cur ? (cur.messages || []).slice() : [];
      rememberCurrent();
      renderMessages();
      renderSidebar();
    }).catch(function () {});
  }
  function scheduleSessionRefresh(notice) {
    if (notice && (notice.uid !== storeSlot() || notice.source === sessionTabId)) return;
    if (streaming) { sessionRefreshPending = true; return; }
    if (sessionRefreshTimer) clearTimeout(sessionRefreshTimer);
    sessionRefreshTimer = setTimeout(function () { sessionRefreshTimer = null; refreshSessionsFromDb(); }, 80);
  }
  function setupSessionSync() {
    if (window.BroadcastChannel) {
      try {
        sessionSyncChannel = new BroadcastChannel("marx-ai-sessions-v3");
        sessionSyncChannel.onmessage = function (event) { scheduleSessionRefresh(event.data || {}); };
      } catch (_) { sessionSyncChannel = null; }
    }
    window.addEventListener("storage", function (event) {
      if (event.key === AI_SYNC_KEY) {
        var notice = {}; try { notice = JSON.parse(event.newValue || "{}"); } catch (_) {}
        scheduleSessionRefresh(notice);
      } else if (sessionDbFailed && event.key === AI_SESSIONS_KEY && !streaming) {
        messages = legacyLoadSessions(); renderMessages(); renderSidebar();
      }
    });
  }

  // ===================== 会员可选：个人文库服务器会话备份 =====================
  function apiJson(url, options) {
    return apiFetch(url, options || {}).then(function (resp) {
      return parseJsonResponse(resp).then(function (data) {
        if (!resp.ok || !data || data.ok === false) {
          var error = new Error((data && data.error) || ("请求失败（HTTP " + resp.status + "）"));
          error.status = resp.status;
          error.payload = data || {};
          throw error;
        }
        return data;
      });
    });
  }
  function setCloudStatus(text, kind) {
    if (!cloudStatusEl) return;
    cloudStatusEl.textContent = text || "";
    cloudStatusEl.classList.toggle("ok", kind === "ok");
    cloudStatusEl.classList.toggle("warn", kind === "warn");
  }
  function renderCloudControls() {
    if (!cloudConsentEl) return;
    cloudConsentEl.checked = !!cloudSync.enabled;
    cloudConsentEl.disabled = !!cloudSync.busy || !cloudSync.loaded || !cloudSync.available || (!cloudSync.eligible && !cloudSync.enabled);
    if (cloudConsentRow) cloudConsentRow.classList.toggle("is-disabled", cloudConsentEl.disabled);
    if (cloudConsentHint) {
      if (!aiUid) cloudConsentHint.textContent = "登录后可设置";
      else if (!cloudSync.available) cloudConsentHint.textContent = "服务器暂不可用";
      else if (cloudSync.graceActive && cloudSync.enabled) cloudConsentHint.textContent = "既有会话宽限至 " + new Date(cloudSync.graceUntil).toLocaleDateString();
      else if (cloudSync.membershipExpired) cloudConsentHint.textContent = "会员宽限期已结束";
      else if (!cloudSync.eligible) cloudConsentHint.textContent = "有效会员可开启";
      else if (cloudSync.enabled) cloudConsentHint.textContent = "已开启 · " + cloudSync.retentionDays + "天保留";
      else cloudConsentHint.textContent = "默认关闭";
    }
    if (cloudPrivacyEl) {
      cloudPrivacyEl.textContent = "独立个人文库服务器 · 账号隔离 · 仅本人可见 · 可申请找回";
    }
    if (sessionsFootEl) sessionsFootEl.textContent = cloudSync.graceActive
      ? "既有云端会话宽限保留；新会话仅存本机"
      : (cloudSync.membershipExpired ? "云端宽限期已结束；本机会话不受影响" : (cloudSync.enabled
      ? "本机＋个人文库服务器备份"
      : (hasCloudRecords() ? "云端保存已停；既有记录保留至到期" : "本机副本")));
  }
  function updateExpiryWarnings() {
    var now = Number(cloudSync.serverNow || nowTs());
    var warningMs = Number(cloudSync.warningDays || 5) * 86400000;
    var expiring = sessionsData.sessions.filter(function (s) {
      var expires = Number(s.expiresAt || 0);
      return expires && expires > now && expires - now <= warningMs;
    });
    if (cloudSync.graceActive) {
      var graceDays = Math.max(0, Math.ceil((Number(cloudSync.graceUntil || 0) - now) / 86400000));
      setCloudStatus("会员已到期；既有云端会话仍可修改并宽限保留 " + graceDays + " 天，新会话只存本机。", "warn");
      if (graceDays <= Number(cloudSync.warningDays || 5)) showStorageWarning("云端会话宽限期即将结束，请及时导出需要长期保留的内容；本机会话不会被删除。");
    } else if (cloudSync.membershipExpired) {
      setCloudStatus("云端保存宽限期已结束；本机会话仍可正常查看和导出。", "warn");
    } else if (expiring.length) {
      setCloudStatus(expiring.length + " 条云端会话将在 " + cloudSync.warningDays + " 天内清理，可在记录右侧点击“续”延长。", "warn");
      showStorageWarning(expiring.length + " 条保存在个人文库服务器的会话即将到期，请及时延长或导出需要保留的内容。");
    } else if (cloudSync.enabled) {
      setCloudStatus("云端保存正常；每次更新会自动续期 " + cloudSync.retentionDays + " 天。", "ok");
    }
  }
  function hasCloudRecords() {
    return sessionsData.sessions.some(function (session) { return Number(session.expiresAt || 0) > 0; });
  }
  function flushCloudDeletes() {
    if (!aiUid || !cloudSync.available) return Promise.resolve();
    var box = loadCloudDeleteOutbox();
    var ids = Object.keys(box[storeSlot()] || {});
    return ids.reduce(function (chain, id) {
      return chain.then(function () {
        return apiJson("/api/ai/conversations/" + encodeURIComponent(id), { method: "DELETE" })
          .then(function () { clearCloudDelete(id); })
          .catch(function () {});
      });
    }, Promise.resolve());
  }
  function enqueueCloudDelete(id) {
    queueCloudDelete(id);
    cloudWriteQueue = cloudWriteQueue.catch(function () {}).then(function () {
      return apiJson("/api/ai/conversations/" + encodeURIComponent(id), { method: "DELETE" });
    }).then(function (data) {
      clearCloudDelete(id);
      setCloudStatus("已删除；云端回收区保留至 " + new Date(Number(data.recovery_until_ms || 0)).toLocaleDateString() + "。", "ok");
      return data;
    }).catch(function () {
      setCloudStatus("本机记录已删除，但云端删除暂未完成；联网后会自动重试。", "warn");
      return null;
    });
    return cloudWriteQueue;
  }
  function loadCloudStatus() {
    if (!aiUid) {
      cloudSync.loaded = true; cloudSync.available = false; cloudSync.eligible = false; cloudSync.enabled = false;
      renderCloudControls(); return Promise.resolve();
    }
    return apiJson("/api/ai/conversations/status", { headers: { Accept: "application/json" } }).then(function (data) {
      cloudSync.loaded = true;
      cloudSync.available = data.available !== false;
      cloudSync.eligible = !!data.eligible;
      cloudSync.canCreate = data.can_create !== undefined ? !!data.can_create : !!data.eligible;
      cloudSync.canUpdateExisting = data.can_update_existing !== undefined ? !!data.can_update_existing : !!data.eligible;
      cloudSync.membershipExpired = !!data.membership_expired;
      cloudSync.graceActive = !!data.grace_active;
      cloudSync.graceUntil = Number(data.grace_until_ms || 0);
      cloudSync.enabled = !!data.enabled;
      cloudSync.retentionDays = Number(data.retention_days || 30);
      cloudSync.warningDays = Number(data.warning_days || 5);
      cloudSync.recoveryDays = Number(data.recovery_days || 7);
      cloudSync.serverNow = Number(data.server_now_ms || nowTs());
      renderCloudControls();
      return syncCloudSessions({ uploadMissing: cloudSync.enabled && cloudSync.canCreate });
    }).catch(function () {
      cloudSync.loaded = true; cloudSync.available = false; cloudSync.enabled = false;
      renderCloudControls();
      setCloudStatus("个人文库服务器暂时不可用；当前会话仍会保存到本机。", "warn");
    });
  }
  function cloudConversationSnapshot(session, level) {
    return {
      id: session.id, title: session.title || deriveTitle(session.messages || []),
      updatedAt: Number(session.updatedAt || nowTs()), clearedAt: Number(session.clearedAt || 0),
      titleUpdatedAt: Number(session.titleUpdatedAt || session.updatedAt || nowTs()), titleManual: !!session.titleManual,
      messages: storedMessages(session.messages || [], level || 0), storageLevel: Number(level || 0),
    };
  }
  function cloudPayloadLevel(session) {
    try {
      var bytes = new Blob([JSON.stringify(cloudConversationSnapshot(session, 0))]).size;
      if (bytes > 4400000) return bytes > 4900000 ? 2 : 1;
    } catch (_) {}
    return 0;
  }
  function applyCloudMetadata(id, cloudRecord) {
    for (var i = 0; i < sessionsData.sessions.length; i++) {
      var session = sessionsData.sessions[i];
      if (session.id !== id) continue;
      session.expiresAt = Number(cloudRecord.expiresAt || cloudRecord.expires_at_ms || session.expiresAt || 0);
      session.serverRevision = Number(cloudRecord.serverRevision || session.serverRevision || 0);
      session.cloudState = "backed_up";
      session.cloudExpiredAt = 0;
      if (Number(cloudRecord.titleUpdatedAt || 0) >= Number(session.titleUpdatedAt || 0)) {
        session.title = cloudRecord.title || session.title;
        session.titleUpdatedAt = Number(cloudRecord.titleUpdatedAt || session.titleUpdatedAt || 0);
        session.titleManual = !!cloudRecord.titleManual;
      }
      if (Array.isArray(cloudRecord.messages)) session.messages = mergeMessages(session.messages, cloudRecord.messages, Number(cloudRecord.clearedAt || session.clearedAt || 0));
      if (!streaming && sessionsData.current === id) messages = (session.messages || []).slice();
      return session;
    }
    return null;
  }
  function enqueueCloudWrite(session, options) {
    if (!session || !session.id || !cloudSync.enabled) return Promise.resolve();
    var existingCloudCopy = Number(session.expiresAt || 0) > Number(cloudSync.serverNow || nowTs()) && session.cloudState !== "expired";
    if (!cloudSync.canCreate && !(cloudSync.graceActive && cloudSync.canUpdateExisting && existingCloudCopy)) return Promise.resolve();
    var snapshot = {
      id: session.id, title: session.title, updatedAt: session.updatedAt,
      clearedAt: Number(session.clearedAt || 0), messages: (session.messages || []).slice(),
      titleUpdatedAt: Number(session.titleUpdatedAt || session.updatedAt || 0), titleManual: !!session.titleManual,
    };
    var level = cloudPayloadLevel(snapshot);
    cloudWriteQueue = cloudWriteQueue.catch(function () {}).then(function () {
      if (!cloudSync.enabled) return null;
      return apiJson("/api/ai/conversations/" + encodeURIComponent(snapshot.id), {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ conversation: cloudConversationSnapshot(snapshot, level), replace_messages: !!(options && options.replaceMessages) }),
      });
    }).then(function (data) {
      if (!data || !data.conversation) return;
      var saved = applyCloudMetadata(snapshot.id, data.conversation);
      if (saved && sessionDb && !sessionDbFailed) persistDbSession(saved, false).catch(function () {});
      if (level > 0) showStorageWarning(level === 1
        ? "本条会话较大：云端已完整保存正文，并精简部分引文展开附件。"
        : "本条会话很大：云端已完整保存正文，未同步引文展开附件。"
      );
      cloudSync.serverNow = nowTs();
      updateExpiryWarnings(); renderSidebar();
    }).catch(function (error) {
      setCloudStatus("云端暂未同步成功，本机副本仍然完整；稍后更新会自动重试。", "warn");
      // 云端自然到期、回收区冲突和临时权限变化都不得反向删除本机副本。
      // 真正的跨设备主动删除由列表接口中带 reason 的删除标记统一处理。
    });
    return cloudWriteQueue;
  }
  function removeLocalConversation(id, deletedAt) {
    var idx = sessionsData.sessions.findIndex(function (s) { return s.id === id; });
    if (idx >= 0) sessionsData.sessions.splice(idx, 1);
    removeEmergencyBackup(id);
    if (sessionDb && !sessionDbFailed) {
      try {
        var tx = sessionDb.transaction(AI_DB_SESSION_STORE, "readwrite");
        tx.objectStore(AI_DB_SESSION_STORE).put({
          key: dbSessionKey(id), uid: storeSlot(), id: id, deletedAt: Number(deletedAt || nowTs()),
          updatedAt: Number(deletedAt || nowTs()), messages: [], title: "",
        });
      } catch (_) {}
    }
  }
  function markLocalCloudExpired(id, expiredAt) {
    var session = sessionsData.sessions.find(function (s) { return s.id === id; });
    if (!session) return;
    session.expiresAt = 0;
    session.serverRevision = 0;
    session.cloudState = "expired";
    session.cloudExpiredAt = Number(expiredAt || nowTs());
    writeEmergencyTextBackup(session);
    if (sessionDb && !sessionDbFailed) persistDbSession(session, false).catch(function () {});
  }
  function syncCloudSessions(options) {
    options = options || {};
    if (!cloudSync.available || !sessionsReady || !aiUid) return Promise.resolve();
    return flushCloudDeletes().then(function () {
      return apiJson("/api/ai/conversations", { headers: { Accept: "application/json" } });
    }).then(function (data) {
      cloudSync.serverNow = Number(data.server_now_ms || nowTs());
      cloudSync.retentionDays = Number(data.retention_days || cloudSync.retentionDays);
      cloudSync.warningDays = Number(data.warning_days || cloudSync.warningDays);
      cloudSync.recoveryDays = Number(data.recovery_days || cloudSync.recoveryDays);
      (data.deleted || []).forEach(function (deleted) {
        var deletionReason = String(deleted.reason || "unknown");
        if (deletionReason === "user" || deletionReason === "user_cleanup") removeLocalConversation(deleted.id, deleted.deletedAt);
        else markLocalCloudExpired(deleted.id, deleted.deletedAt);
      });
      var remoteIds = {};
      (data.conversations || []).forEach(function (remote) {
        remoteIds[remote.id] = true;
        remote.cloudState = "backed_up";
        remote.cloudExpiredAt = 0;
        var local = sessionsData.sessions.find(function (s) { return s.id === remote.id; });
        if (!local) {
          local = normalizeSessionRecord(remote); sessionsData.sessions.push(local);
        } else {
          var clearedAt = Math.max(Number(local.clearedAt || 0), Number(remote.clearedAt || 0));
          local.messages = mergeMessages(remote.messages || [], local.messages || [], clearedAt);
          if (Number(remote.titleUpdatedAt || remote.updatedAt || 0) >= Number(local.titleUpdatedAt || local.updatedAt || 0)) {
            local.title = remote.title || local.title;
            local.titleUpdatedAt = Number(remote.titleUpdatedAt || remote.updatedAt || 0);
            local.titleManual = !!remote.titleManual;
          }
          local.updatedAt = Math.max(Number(local.updatedAt || 0), Number(remote.updatedAt || 0));
          local.clearedAt = clearedAt;
          local.expiresAt = Number(remote.expiresAt || 0);
          local.serverRevision = Number(remote.serverRevision || 0);
          local.cloudState = "backed_up";
          local.cloudExpiredAt = 0;
        }
        writeEmergencyTextBackup(local);
        if (sessionDb && !sessionDbFailed) persistDbSession(local, false).catch(function () {});
      });
      sessionsData.sessions.sort(function (a, b) { return Number(b.updatedAt || 0) - Number(a.updatedAt || 0); });
      if (!currentSession() && sessionsData.sessions.length) sessionsData.current = sessionsData.sessions[0].id;
      var cur = currentSession(); messages = cur ? (cur.messages || []).slice() : [];
      rememberCurrent(); renderMessages(); renderSidebar(); renderCloudControls(); updateExpiryWarnings();
      // 首次授权时把现有本机记录逐条上传；已存在的记录也用并集合并，避免任何一端覆盖另一端。
      if (options.uploadMissing) sessionsData.sessions.forEach(function (session) { if (!remoteIds[session.id]) enqueueCloudWrite(session, {}); });
    }).catch(function () {
      setCloudStatus("云端记录暂时读取失败，本机副本仍可正常使用。", "warn");
    });
  }
  function setCloudConsent(enabled) {
    if (cloudSync.busy || (enabled && !cloudSync.eligible)) { renderCloudControls(); return; }
    var message = enabled
      ? "确认将会话保存到独立的个人文库服务器？记录按账号隔离，只有你登录后可查看；保留 " + cloudSync.retentionDays + " 天，删除后有 " + cloudSync.recoveryDays + " 天管理员协助找回期。"
      : "确认停止新增云端保存？已保存记录仍保留到各自到期日，你仍可查看、导出或删除。";
    if (!window.confirm(message)) { renderCloudControls(); return; }
    var previousEnabled = cloudSync.enabled;
    cloudSync.enabled = !!enabled; // 勾选状态立即稳定显示；失败时再回滚，避免请求期间视觉跳回。
    cloudSync.busy = true; renderCloudControls();
    apiJson("/api/ai/conversations/consent", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: !!enabled }),
    }).then(function (data) {
      cloudSync.enabled = !!data.enabled;
      cloudSync.retentionDays = Number(data.retention_days || cloudSync.retentionDays);
      cloudSync.warningDays = Number(data.warning_days || cloudSync.warningDays);
      cloudSync.recoveryDays = Number(data.recovery_days || cloudSync.recoveryDays);
      setCloudStatus(cloudSync.enabled ? "已授权，正在把本机记录安全同步到个人文库服务器…" : "已停止新增云端保存。", cloudSync.enabled ? "ok" : "");
      if (cloudSync.enabled) return syncCloudSessions({ uploadMissing: true });
    }).catch(function () {
      cloudSync.enabled = previousEnabled;
      setCloudStatus("保存设置未能更新，请稍后重试。", "warn");
    }).then(function () {
      cloudSync.busy = false; renderCloudControls();
    });
  }
  function extendCloudConversation(id) {
    if (!cloudSync.available || !cloudSync.eligible || cloudSync.graceActive) return;
    apiJson("/api/ai/conversations/" + encodeURIComponent(id) + "/extend", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    }).then(function (data) {
      var session = sessionsData.sessions.find(function (s) { return s.id === id; });
      if (session) session.expiresAt = Number(data.expires_at_ms || 0);
      setCloudStatus("已延长保留 " + cloudSync.retentionDays + " 天。", "ok"); renderSidebar();
    }).catch(function () { setCloudStatus("延长保留失败，请稍后重试。", "warn"); });
  }
  function cleanupBeforeSelectedDate() {
    if (!cleanupDateEl || !cleanupDateEl.value || streaming) return;
    var before = new Date(cleanupDateEl.value + "T00:00:00").getTime();
    if (!before || before > nowTs()) { showStorageWarning("请选择今天以前的有效日期。"); return; }
    var ids = sessionsData.sessions.filter(function (s) {
      return s.id !== sessionsData.current && Number(s.updatedAt || 0) < before;
    }).map(function (s) { return s.id; });
    if (!ids.length) { setCloudStatus("该日期以前没有可清理的会话；当前会话已自动保护。", ""); return; }
    var recoveryText = hasCloudRecords() ? "云端记录会进入 " + cloudSync.recoveryDays + " 天回收区。" : "未启用云端保存的本机记录删除后无法恢复。";
    if (!window.confirm("将删除 " + ids.length + " 条较早会话，当前会话不会删除。" + recoveryText + "是否继续？")) return;
    if (cleanupOldBtn) cleanupOldBtn.disabled = true;
    var cloudStep = hasCloudRecords() ? cloudWriteQueue.catch(function () {}).then(function () {
      return apiJson("/api/ai/conversations/cleanup-before", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ before_ms: before, keep_ids: [sessionsData.current] }),
      });
    }) : Promise.resolve({ ok: true });
    cloudStep.then(function () {
      ids.forEach(function (id) { deleteSession(id, { skipCloud: true, silent: true }); });
      setCloudStatus("已清理 " + ids.length + " 条较早会话。", "ok");
      checkStorageCapacity(); renderSidebar();
    }).catch(function () {
      setCloudStatus("云端清理失败，为避免两端不一致，本机记录尚未删除。", "warn");
    }).then(function () { if (cleanupOldBtn) cleanupOldBtn.disabled = false; });
  }
  function switchSession(id) {
    if (!sessionsReady || streaming) { closeSessionsDrawer(); return; }
    if (id !== sessionsData.current) {
      sessionsData.current = id; rememberCurrent();
      var s = currentSession();
      messages = s ? (s.messages || []).slice() : [];
      renderMessages(); renderSidebar();
    }
    closeSessionsDrawer();
    try { messagesEl.scrollIntoView({ block: "start" }); } catch (_) {}
  }
  function newSession() {
    if (!sessionsReady || streaming) return;
    var cur = currentSession();
    if (!(cur && (!cur.messages || !cur.messages.length))) {   // 当前已是空新会话则复用，不堆叠空会话
      var createdAt = nowTs();
      var s = { id: genId(), title: "新会话", updatedAt: createdAt, titleUpdatedAt: createdAt, titleManual: false, clearedAt: 0, messages: [] };
      sessionsData.sessions.unshift(s); sessionsData.current = s.id; rememberCurrent();
      enqueueSessionWrite(s, { replaceMessages: true });
    }
    messages = []; renderMessages(); renderSidebar(); closeSessionsDrawer();
    if (promptEl) { try { promptEl.focus(); } catch (_) {} }
  }
  function deleteSession(id, options) {
    options = options || {};
    if (!sessionsReady) return;
    var idx = -1, i;
    for (i = 0; i < sessionsData.sessions.length; i++) if (sessionsData.sessions[i].id === id) idx = i;
    if (idx < 0) return;
    var hadCloudCopy = Number(sessionsData.sessions[idx].expiresAt || 0) > 0;
    sessionsData.sessions.splice(idx, 1);
    removeEmergencyBackup(id);
    deleteDbSession(id);
    if ((cloudSync.enabled || hadCloudCopy) && !options.skipCloud) {
      enqueueCloudDelete(id);
    }
    if (sessionsData.current === id) {
      if (!sessionsData.sessions.length) {
        var createdAt = nowTs();
        sessionsData.sessions.unshift({ id: genId(), title: "新会话", updatedAt: createdAt, titleUpdatedAt: createdAt, titleManual: false, clearedAt: 0, messages: [] });
      }
      sessionsData.current = sessionsData.sessions[0].id;
      var s = currentSession(); messages = s ? (s.messages || []).slice() : [];
      renderMessages();
      rememberCurrent();
      if (s && sessionsData.sessions.length === 1 && !s.messages.length) enqueueSessionWrite(s, { replaceMessages: true });
    }
    renderSidebar();
  }
  function renameSession(id) {
    if (!sessionsReady) return;
    var s = null, i;
    for (i = 0; i < sessionsData.sessions.length; i++) if (sessionsData.sessions[i].id === id) s = sessionsData.sessions[i];
    if (!s) return;
    var row = sessionsListEl ? sessionsListEl.querySelector('.aip-session[data-sid="' + id + '"] .aip-session-main') : null;
    if (!row) return;
    var input = document.createElement("input");
    input.className = "aip-session-rename"; input.value = s.title || "";
    row.innerHTML = ""; row.appendChild(input); input.focus(); input.select();
    // 重命名只更新标题；绝不能用旧标签页的 messages 整段替换服务器/IndexedDB 中的新内容。
    var committed = false;
    function commit() {
      if (committed) return; committed = true;
      var renamedAt = nowTs();
      s.title = (input.value || "").trim() || s.title; s.titleManual = true;
      s.titleUpdatedAt = renamedAt; s.updatedAt = renamedAt;
      enqueueSessionWrite(s, { replaceMessages: false }); renderSidebar();
    }
    input.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); commit(); } else if (e.key === "Escape") { renderSidebar(); } });
    input.addEventListener("blur", commit);
  }
  function relTime(ts) {
    var d = nowTs() - (ts || 0), m = Math.floor(d / 60000);
    if (m < 1) return "刚刚"; if (m < 60) return m + " 分钟前";
    var h = Math.floor(m / 60); if (h < 24) return h + " 小时前";
    var day = Math.floor(h / 24); if (day < 30) return day + " 天前";
    return Math.floor(day / 30) + " 个月前";
  }
  function renderSidebar() {
    if (!sessionsListEl) return;
    var arr = sessionsData.sessions.slice().sort(function (a, b) { return (b.updatedAt || 0) - (a.updatedAt || 0); });
    if (!arr.length) { sessionsListEl.innerHTML = '<div class="aip-sessions-empty">还没有会话。点「新建会话」开始。</div>'; return; }
    sessionsListEl.innerHTML = arr.map(function (s) {
      var expires = Number(s.expiresAt || 0), now = Number(cloudSync.serverNow || nowTs());
      var daysLeft = expires ? Math.max(0, Math.ceil((expires - now) / 86400000)) : 0;
      var expiry = expires && daysLeft <= Number(cloudSync.warningDays || 5) && !cloudSync.graceActive
        ? ' · <span class="aip-session-expiry">云端' + daysLeft + '天后清理</span>' : '';
      var cloudBadge = '';
      var cloudBadgeKind = '';
      if (s.cloudState === "expired") { cloudBadge = "云备份已到期"; cloudBadgeKind = " expired"; }
      else if (expires && cloudSync.graceActive) { cloudBadge = "宽限保留 · " + daysLeft + "天"; cloudBadgeKind = " grace"; }
      else if (expires) { cloudBadge = "已上云备份"; cloudBadgeKind = " saved"; }
      else if (cloudSync.enabled && cloudSync.canCreate) { cloudBadge = "等待上云"; cloudBadgeKind = " pending"; }
      else if (cloudSync.graceActive || cloudSync.membershipExpired) { cloudBadge = "仅本机保存"; cloudBadgeKind = " local"; }
      return '<div class="aip-session' + (s.id === sessionsData.current ? " active" : "") + '" data-sid="' + esc(s.id) + '">'
        + '<div class="aip-session-main"><div class="aip-session-title">' + esc(s.title || deriveTitle(s.messages || [])) + '</div>'
        + '<div class="aip-session-time">' + esc(relTime(s.updatedAt)) + expiry + '</div></div>'
        + '<div class="aip-session-side"><div class="aip-session-acts">'
        + (expires && daysLeft <= Number(cloudSync.warningDays || 5) && cloudSync.eligible && !cloudSync.graceActive ? '<button type="button" data-act="extend" title="延长云端保留">续</button>' : '')
        + '<button type="button" class="aip-session-export" data-act="export-html" title="导出本会话 HTML" aria-label="导出本会话 HTML">导出本地</button>'
        + '<button type="button" data-act="rename" title="重命名">✎</button>'
        + '<button type="button" data-act="delete" title="删除">🗑</button>'
        + '</div>' + (cloudBadge ? '<div class="aip-session-cloud-badge' + cloudBadgeKind + '">' + esc(cloudBadge) + '</div>' : '') + '</div></div>';
    }).join("");
  }
  function openSessionsDrawer() { if (sessionsEl) sessionsEl.classList.add("open"); if (sessionsScrim) { sessionsScrim.hidden = false; sessionsScrim.classList.add("open"); } }
  function closeSessionsDrawer() { if (sessionsEl) sessionsEl.classList.remove("open"); if (sessionsScrim) sessionsScrim.classList.remove("open"); }

  // ===================== 额度 =====================
  function updateTokenQuota(quota) {
    if (!quota || typeof quota !== "object") return;
    aiTokenQuota = quota;
    if (!tokenQuotaWrap) return;
    if (quota.unlimited || quota.limit === null || quota.limit === undefined) {
      tokenQuotaWrap.classList.add("hidden");
      return;
    }
    tokenQuotaWrap.classList.remove("hidden");
    tokenQuotaWrap.classList.toggle("low", !!quota.low);
    tokenQuotaWrap.classList.toggle("exhausted", !!quota.exhausted);
    var pct = Math.max(0, Math.min(100, Math.round((quota.ratio || 0) * 100)));
    tokenQuotaFill.style.width = pct + "%";
    if (quota.exhausted) {
      var hasPack = aiCredits && aiCredits.chat > 0;
      tokenQuotaText.textContent = "本周 AI 额度已用完，下周一恢复" + (hasPack ? "（可用资源包继续）" : "");
    } else if (Number(quota.limit) <= 0) {
      tokenQuotaText.textContent = "当前身份未开放免费 AI 额度";
    } else {
      tokenQuotaText.textContent = "本周 AI 额度剩余约 " + pct + "%";
    }
  }
  function updateResearchQuota(q) {
    if (q && typeof q === "object") researchQuota = q;
    if (!researchQuotaEl) return;
    var rq = researchQuota || {};
    // 后端 _research_quota_payload 始终附带权威 message（管理员不限次 / 本周剩余 X/Y 次，含资源包）。
    if (rq.message) { researchQuotaEl.textContent = rq.message; return; }
    if (rq.unlimited) { researchQuotaEl.textContent = "研究综述不限次"; return; }
    if (typeof rq.limit === "number") {
      var left = (typeof rq.remaining === "number") ? rq.remaining : Math.max(0, rq.limit - Number(rq.used || 0));
      researchQuotaEl.textContent = "本周研究综述剩余 " + left + "/" + rq.limit + " 次";
    } else {
      researchQuotaEl.textContent = "";
    }
  }

  // ===================== 通道 / 接地 / 范围 =====================
  function webAccess() { return !!(config && config.web_access); }
  function glmEntitled() {
    var models = config && config.ai_entitlements && config.ai_entitlements.models;
    return !!(models && Array.isArray(models["glm-5.1"]) && models["glm-5.1"].indexOf("off") >= 0);
  }
  function hasChat() { return !!(config && config.access); }
  function hasResearch() { return !!(config && config.research_access); }
  function anyAccess() { return hasChat() || hasResearch(); }
  function runtimeEnabled() { return !!(config && config.runtime && config.runtime.enabled); }
  function depthAllowed(d) { return d === "research" ? hasResearch() : hasChat(); }
  function currentModelChoice() {
    return modelSelect ? modelSelect.value : "flash";
  }
  function defaultModelForDepth() {
    var defaults = config && config.ai_entitlements && config.ai_entitlements.defaults;
    var selected = defaults && defaults[depth === "research" ? "research" : "quick"];
    if (!selected || !selected.model) return "";
    if (selected.model === "glm-5.1") return "zhipu";
    var provider = selected.provider || (selected.model.indexOf("mimo-") === 0 ? "mimo" : "deepseek");
    return provider + "|" + selected.model + "|" + (selected.reasoning_effort || "off");
  }
  function syncModelForDepth(forceDefault) {
    if (!modelSelect) return;
    var research = depth === "research";
    Array.prototype.forEach.call(modelSelect.options, function (opt) {
      var hidden = (!research && opt.getAttribute("data-research-only") === "1") ||
                   (research && opt.getAttribute("data-research-disabled") === "1") ||
                   (opt.value === "zhipu" && !webAccess() && !glmEntitled());
      opt.hidden = hidden; opt.disabled = hidden;
    });
    var wanted = forceDefault ? defaultModelForDepth() : "";
    var wantedOption = wanted && Array.prototype.find.call(modelSelect.options, function (opt) {
      return opt.value === wanted && !opt.disabled;
    });
    if (wantedOption) modelSelect.value = wantedOption.value;
    var selected = modelSelect.options[modelSelect.selectedIndex];
    if (!selected || selected.disabled) {
      var first = Array.prototype.find.call(modelSelect.options, function (opt) { return !opt.disabled; });
      if (first) modelSelect.value = first.value;
    }
  }
  function currentModelOption() { return modelSelect && modelSelect.options[modelSelect.selectedIndex]; }
  function currentProvider() {
    var opt = currentModelOption();
    return opt ? (opt.getAttribute("data-provider") || (opt.value === "zhipu" ? "zhipu" : "deepseek")) : "deepseek";
  }
  function currentApiModel() {
    var opt = currentModelOption();
    if (!opt || opt.value === "zhipu") return null;
    return opt.getAttribute("data-model") || (opt.value === "pro" ? "deepseek-v4-pro" : "deepseek-v4-flash");
  }
  function currentReasoningEffort() {
    var opt = currentModelOption();
    return opt ? (opt.getAttribute("data-effort") || "off") : "off";
  }
  function currentGrounding() { return groundingToggle ? !!groundingToggle.checked : true; }

  var scopeState = { mode: "auto", selected: [] };
  function saveScopeState() { try { localStorage.setItem(AI_SCOPE_KEY, JSON.stringify(scopeState)); } catch (_) {} }
  function restoreScopeState() {
    try {
      var s = JSON.parse(localStorage.getItem(AI_SCOPE_KEY) || "null");
      if (s && (s.mode === "auto" || s.mode === "all" || s.mode === "custom")) {
        var sel = Array.isArray(s.selected) ? s.selected.filter(function (x) { return typeof x === "string"; }) : [];
        scopeState = { mode: (s.mode === "custom" && !sel.length) ? "auto" : s.mode, selected: sel };
      }
    } catch (_) {}
  }
  function currentScope() {
    if (bookScopeCtl && bookScopeCtl.hasSelection()) return bookScopeCtl.getTokens();
    if (scopeState.mode === "all") return "all";
    if (scopeState.mode === "custom" && scopeState.selected.length) return scopeState.selected.slice();
    return "auto";
  }
  function mountBookScopeOnce(tree) {
    if (bookScopeCtl || !bookScopeMount || !window.BookScope) return;
    if (!Array.isArray(tree) || !tree.length) return;
    bookScopeCtl = window.BookScope.mount(bookScopeMount, tree, {
      onChange: paintScopeChips,
    });
  }
  function paintScopeChips() {
    if (!scopeChips) return;
    var exactBookSelection = !!(bookScopeCtl && bookScopeCtl.hasSelection());
    Array.prototype.forEach.call(scopeChips.querySelectorAll(".aip-chip"), function (btn) {
      var id = btn.getAttribute("data-scope");
      var on = !exactBookSelection && ((id === "auto" && scopeState.mode === "auto") ||
               (id === "all" && scopeState.mode === "all") ||
               (scopeState.mode === "custom" && scopeState.selected.indexOf(id) >= 0));
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }
  function renderScopeChips(list) {
    if (!scopeChips) return;
    var opts = (Array.isArray(list) && list.length) ? list : [{ id: "auto", label: "自动（智能判断）" }, { id: "all", label: "全部著作" }];
    scopeChips.innerHTML = opts.map(function (o) {
      return '<button type="button" class="aip-chip" data-scope="' + escAttr(o.id) + '" aria-pressed="false">' + esc(o.label) + "</button>";
    }).join("");
    var validIds = opts.map(function (o) { return o.id; });
    scopeState.selected = scopeState.selected.filter(function (x) { return validIds.indexOf(x) >= 0; });
    if (scopeState.mode === "custom" && !scopeState.selected.length) scopeState.mode = "auto";
    paintScopeChips();
  }
  function onScopeChip(id) {
    // 点击著作群即切回群级范围；此前“指定著作/卷”的硬限定同步清除，避免界面显示与实际请求不一致。
    if (bookScopeCtl && bookScopeCtl.hasSelection()) bookScopeCtl.clear();
    if (id === "auto") scopeState = { mode: "auto", selected: [] };
    else if (id === "all") scopeState = { mode: "all", selected: [] };
    else {
      var sel = scopeState.mode === "custom" ? scopeState.selected.slice() : [];
      var i = sel.indexOf(id);
      if (i >= 0) sel.splice(i, 1); else sel.push(id);
      scopeState = sel.length ? { mode: "custom", selected: sel } : { mode: "auto", selected: [] };
    }
    saveScopeState();
    paintScopeChips();
  }

  // ===================== 深度切换 =====================
  function scopeRelevant() {
    // 研究档：范围始终有意义；快速档：仅接地时有意义。
    return depth === "research" || currentGrounding();
  }
  function applyDepthUI(forceDefaultModel) {
    depthTabs.forEach(function (t) {
      var d = t.getAttribute("data-depth");
      var on = d === depth;
      t.classList.toggle("active", on);
      t.setAttribute("aria-selected", on ? "true" : "false");
      t.disabled = !depthAllowed(d);
    });
    var isResearch = depth === "research";
    // 模型选择两档都显示；但研究综述剔除 flash（默认 v4pro，会员可选智谱），快速档保留 flash 默认。
    if (providerRow) providerRow.hidden = false;
    syncModelForDepth(!!forceDefaultModel);
    updateStatusLine();
    if (groundingRow) groundingRow.hidden = isResearch;
    if (researchNote) researchNote.hidden = !isResearch;
    if (scopeRow) scopeRow.hidden = !(runtimeEnabled() && hasAnyForDepth() && scopeRelevant());
    updateResearchQuota();
    updateSendEnabled();
  }
  function hasAnyForDepth() { return depthAllowed(depth); }
  function setDepth(d) {
    if (d !== "quick" && d !== "research") return;
    if (!depthAllowed(d)) return;   // 无权限的档不可切
    depth = d;
    try { localStorage.setItem(AI_DEPTH_KEY, d); } catch (_) {}
    applyDepthUI(true);
  }

  function updateStatusLine() {
    if (!runtimeEnabled() || !anyAccess()) return;
    var rt = config.runtime;
    if (!modelBadge) return;
    var opt = currentModelOption();
    modelBadge.textContent = opt ? opt.textContent.trim() : "AI";
  }
  function updateSendEnabled() {
    var canUse = sessionsReady && runtimeEnabled() && depthAllowed(depth);
    // 生成中：把「发送提问 / 清空会话」换成单个「停止回答」，仿主流 AI 对话的收发切换。
    if (sendBtn) { sendBtn.disabled = !(canUse && !streaming); sendBtn.hidden = streaming; }
    if (stopBtn) stopBtn.hidden = !streaming;
    if (clearBtn) clearBtn.hidden = streaming;
    if (promptEl) promptEl.disabled = !canUse;   // 生成中仍可输入下一问，但需先停止或等本条完成
  }

  // /ai 页专属锁定文案（区别于抽屉的「AI 随心问」措辞）：按当前权限门（登录/会员）取词。
  function lockedMessage() {
    var gate = (config && config.access_gate) || "";
    return gate === "login"
      ? "「AI 研究对话」登录后即可使用，每位登录用户每日均有免费额度。"
      : "「AI 研究对话」需登录并开通会员后使用。";
  }

  function modelDisplayName(model, effort) {
    var names = {
      "mimo-v2.5": "MiMo V2.5",
      "mimo-v2.5-pro": "MiMo V2.5 Pro",
      "deepseek-v4-flash": "DeepSeek V4 Flash",
      "deepseek-v4-pro": "DeepSeek V4 Pro"
    };
    var suffix = effort === "on" ? "（深度思考）"
      : effort === "off" ? "（非思考）"
      : "（思考 " + effort + "）";
    return (names[model] || model) + suffix;
  }

  function rebuildEntitledModelOptions(c) {
    if (!modelSelect) return;
    var models = c && c.ai_entitlements && c.ai_entitlements.models;
    if (!models || typeof models !== "object") return;
    var preferred = ["mimo-v2.5", "mimo-v2.5-pro", "deepseek-v4-flash", "deepseek-v4-pro"];
    var keys = Object.keys(models).filter(function (model) {
      return model !== "glm-5.1" && Array.isArray(models[model]) && models[model].length;
    }).sort(function (a, b) {
      var ai = preferred.indexOf(a), bi = preferred.indexOf(b);
      return (ai < 0 ? 99 : ai) - (bi < 0 ? 99 : bi) || a.localeCompare(b);
    });
    if (!keys.length) return;

    Array.prototype.slice.call(modelSelect.options).forEach(function (opt) {
      if (opt.value !== "zhipu") modelSelect.removeChild(opt);
    });
    keys.forEach(function (model) {
      models[model].forEach(function (effort) {
        var provider = model.indexOf("mimo-") === 0 ? "mimo" : "deepseek";
        var opt = document.createElement("option");
        opt.value = provider + "|" + model + "|" + effort;
        opt.setAttribute("data-provider", provider);
        opt.setAttribute("data-model", model);
        opt.setAttribute("data-effort", effort);
        if (provider === "deepseek" && effort !== "off") opt.setAttribute("data-research-only", "1");
        opt.textContent = modelDisplayName(model, effort);
        modelSelect.insertBefore(opt, zhipuOpt && zhipuOpt.parentNode === modelSelect ? zhipuOpt : null);
      });
    });
  }

  // ===================== 配置 =====================
  function applyConfig(c) {
    config = c;
    aiTokenQuota = c.quota || {};
    aiCredits = c.credits || { chat: 0, research: 0 };
    researchQuota = c.research_quota || {};

    if (!anyAccess()) {
      if (lockEl) { lockEl.textContent = lockedMessage(); lockEl.hidden = false; }
      if (modelBadge) modelBadge.textContent = "未开放";
      if (controlsEl) controlsEl.hidden = true;
      if (composerEl) composerEl.hidden = true;
      renderMessages();
      return;
    }
    if (lockEl) lockEl.hidden = true;
    if (controlsEl) controlsEl.hidden = false;
    if (composerEl) composerEl.hidden = false;

    rebuildEntitledModelOptions(c);
    // 模型选择：无联网权限则移除「智谱」项；有则更新其显示名。
    if (zhipuOpt) {
      if (!webAccess() && !glmEntitled()) {
        if (zhipuOpt.parentNode) zhipuOpt.parentNode.removeChild(zhipuOpt);
        zhipuOpt = null;
      } else if (c.runtime && c.runtime.zhipu_model) {
        zhipuOpt.textContent = "智谱 " + c.runtime.zhipu_model + "（可联网检索）";
      }
    }
    // 模型初值由服务端的“套餐 × 功能场景”默认值决定，不恢复历史选择。
    restoreScopeState();
    renderScopeChips(c.scopes);
    mountBookScopeOnce(c.book_scope_tree);

    // 初始深度：记忆值（无权限则回落到有权限的一档）
    var savedDepth = "quick";
    try { savedDepth = localStorage.getItem(AI_DEPTH_KEY) || "quick"; } catch (_) {}
    depth = depthAllowed(savedDepth) ? savedDepth : (hasChat() ? "quick" : "research");

    if (runtimeEnabled()) {
      updateStatusLine();
      updateTokenQuota(aiTokenQuota);
    } else {
      if (modelBadge) modelBadge.textContent = c.unavailable_message || "AI 服务暂未启用。";
      if (lockEl) { lockEl.textContent = c.unavailable_message || "AI 服务暂未启用。"; lockEl.hidden = false; }
    }
    applyDepthUI(true);
    renderMessages();
  }
  function loadConfig() {
    if (config || configLoading) return;
    configLoading = true;
    fetch("/api/ai/assistant-config", { headers: { Accept: "application/json" }, credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (j) { configLoading = false; if (j && j.ok) applyConfig(j); else showConfigError(); })
      .catch(function () { configLoading = false; showConfigError(); });
  }
  function showConfigError() {
    if (modelBadge) modelBadge.textContent = "载入失败";
    if (lockEl) { lockEl.textContent = "AI 配置载入失败，请刷新页面重试。"; lockEl.hidden = false; }
  }

  // ===================== 提问 =====================
  function historyCitationRefs(message) {
    if (!message || message.role !== "assistant" || !Array.isArray(message.citations)) return [];
    return message.citations.slice(0, 40).map(function (citation) {
      if (!citation || typeof citation !== "object") return null;
      var ref = {};
      ["source_file", "pdf_page", "citation", "viewer_url", "grounding_index", "review_index"].forEach(function (key) {
        if (citation[key] !== undefined && citation[key] !== null) ref[key] = citation[key];
      });
      if (Array.isArray(citation.pdf_pages)) ref.pdf_pages = citation.pdf_pages.slice();
      if (Array.isArray(citation.candidate_pdf_pages)) ref.candidate_pdf_pages = citation.candidate_pdf_pages.slice();
      if (citation.context) ref.context = String(citation.context).slice(0, 360);
      return Object.keys(ref).length ? ref : null;
    }).filter(Boolean);
  }
  function buildHistory(question) {
    var augmenting = /(?:增加|增补|补充|补足|补齐|更多).{0,16}(?:引文|引用|原文|证据|来源)|(?:引文|引用|原文|证据|来源).{0,16}(?:增加|增补|补充|补足|更多|上限)/.test(question || "");
    var lastAssistant = -1;
    messages.forEach(function (m, i) { if (!m.pending && m.role === "assistant") lastAssistant = i; });
    var fullAnswer = lastAssistant >= 0 ? messages[lastAssistant] : null;
    return messages
      .filter(function (m) { return !m.pending && (m.role === "user" || m.role === "assistant"); })
      .map(function (m) {
        var numbered = numberMessageCitations(m);
        var content = String(numbered.content || "");
        var charCap = augmenting && m === fullAnswer ? 80000 : HISTORY_CHAR_CAP;
        if (content.length > charCap) content = content.slice(0, charCap) + "……（此处略）";
        var item = { role: m.role, content: content };
        var citationRefs = historyCitationRefs(numbered);
        if (citationRefs.length) item.citation_refs = citationRefs;
        return item;
      });
}

  function submitQuestion(text) {
    if (!sessionsReady) { showStorageWarning("会话记录仍在载入，请稍候再发送，避免新内容覆盖历史记录。"); return; }
    if (!runtimeEnabled() || !depthAllowed(depth) || streaming) return;
    var question = String(text || "").trim();
    if (!question) return;
    var history = buildHistory(question);
    var isResearch = depth === "research";

    messages.push({ role: "user", content: question });
    var assistant = { role: "assistant", content: "", sources: [], citations: [], warnings: [], pending: true, kind: isResearch ? "research" : "quick" };
    messages.push(assistant);
    saveMessages();
    renderMessages();
    scrollToLatestQuestion();
    if (promptEl) promptEl.value = "";
    streaming = true;
    // 中止控制：持有本次请求的 AbortController，「停止回答」按钮即中断这条在飞的 fetch。
    var abort = (typeof AbortController !== "undefined") ? new AbortController() : null;
    currentAbort = abort;
    updateSendEnabled();

    var req = isResearch
      ? apiFetch("/api/search/associative", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ q: question, gist: question, mode: "research", messages: history, scope: currentScope(), rerank: true, provider: currentProvider(), model: currentApiModel(), reasoning_effort: currentReasoningEffort() }),
          signal: abort ? abort.signal : undefined,
        })
      : apiFetch("/api/ai/search-chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ question: question, messages: history, provider: currentProvider(), model: currentApiModel(), reasoning_effort: currentReasoningEffort(), grounding: currentGrounding(), scope: currentScope() }),
          signal: abort ? abort.signal : undefined,
        });

    req.then(function (resp) {
      var ctype = (resp.headers.get("content-type") || "").toLowerCase();
      if (ctype.indexOf("text/event-stream") >= 0) {
        return readSseResultStream(resp, function (progress) {
          var elapsed = Number(progress.elapsed_seconds || 0);
          assistant.progress = String(progress.message || "AI 正在生成") + (elapsed ? "（" + elapsed + "秒）" : "");
          renderMessages();
        });
      }
      return parseJsonResponse(resp);
    }).then(function (data) {
      if (data.ai_credits) aiCredits = data.ai_credits;
      if (data.ai_token_quota) updateTokenQuota(data.ai_token_quota);
      if (data.research_quota) updateResearchQuota(data.research_quota);
      if (!data.ok) throw new Error(data.error || "AI 请求失败");
      if (isResearch) {
        assistant.content = data.review_markdown || "";
        assistant.citations = (data.review_citations || []).map(function (c) {
          return Object.assign({}, c, { grounding_index: c.review_index });
        });
        assistant.warnings = data.warnings || [];
        assistant.groundingScope = data.scope || null;
      } else {
        assistant.content = data.answer_markdown || "";
        assistant.sources = data.sources || [];
        assistant.citations = data.citations || [];
        assistant.warnings = data.warnings || [];
        assistant.groundingScope = data.grounding_scope || null;
      }
      delete assistant.pending;
      saveMessages();
      renderMessages();
    }).catch(function (error) {
      // 用户主动「停止回答」：撤下这条待答气泡、保留提问，不显示报错，随即可开启新回答。
      if (error && error.name === "AbortError") {
        var idx = messages.indexOf(assistant);
        if (idx >= 0) messages.splice(idx, 1);
        saveMessages();
        renderMessages();
        return;
      }
      assistant.content = "请求失败：" + (error.message || "未知错误");
      assistant.sources = []; assistant.citations = []; assistant.warnings = [];
      delete assistant.pending;
      saveMessages();
      renderMessages();
    }).then(function () {
      if (currentAbort === abort) currentAbort = null;
      streaming = false;
      updateSendEnabled();
      if (sessionRefreshPending) {
        sessionRefreshPending = false;
        sessionWriteQueue.catch(function () {}).then(function () { scheduleSessionRefresh(); });
      }
    });
  }

  // 「停止回答」：中断当前在飞的这条请求。服务端后台生成可能仍会跑完，但客户端不再等待，
  // 待答气泡随即撤下，用户可立即开启新的提问。
  function stopStreaming() {
    if (!streaming || !currentAbort) return;
    try { currentAbort.abort(); } catch (_) {}
  }

  // ===================== 事件绑定 =====================
  depthTabs.forEach(function (t) {
    t.addEventListener("click", function () { setDepth(t.getAttribute("data-depth")); });
  });
  // 引用条目「展开/收起」+「复制引文」：事件委托到对话容器（renderMessages 会重建 DOM，故不逐个绑定）。
  if (messagesEl) {
    messagesEl.addEventListener("click", function (e) {
      var t = e.target;
      if (!t || !t.closest) return;
      var wordBtn = t.closest('[data-export-word="footnote"], [data-export-word="endnote"]');
      if (wordBtn) {
        // 两个按钮分支硬绑定，不把可变 DOM 字符串直接透传给文档生成器。
        var requestedKind = wordBtn.matches('[data-export-word="footnote"]') ? "footnote" : "endnote";
        exportAnswerWord(wordBtn.getAttribute("data-message-index"), requestedKind, wordBtn);
        return;
      }
      var copyBtn = t.closest("[data-copy-cite]");
      if (copyBtn) {
        var card = copyBtn.closest(".ai-citation-item");
        var citeNode = card ? card.querySelector(".ai-citation-cite [data-citations], .ai-citation-cite span:last-child") : null;
        var text = citeNode ? (citeNode.textContent || "").trim() : "";
        if (text) copyText(text, copyBtn);
        return;
      }
      var btn = t.closest(".ai-citations-toggle");
      if (!btn) return;
      var box = btn.closest(".ai-citations");
      if (!box) return;
      var nowCollapsed = box.classList.toggle("aip-cites-collapsed");
      btn.setAttribute("aria-expanded", nowCollapsed ? "false" : "true");
      var hint = btn.querySelector(".aip-cite-hint");
      if (hint) hint.textContent = nowCollapsed ? "展开" : "收起";
    });
    // 「引用格式」切换：就地重写所有已渲染引文出处串并同步各处下拉（共享 marx-citation-format-v1）。
    messagesEl.addEventListener("change", function (e) {
      var sel = (e.target && e.target.closest) ? e.target.closest(".aip-cite-fmt-select") : null;
      if (!sel) return;
      applyCiteFormat(sel.value);
    });
  }
  function copyText(text, btn) {
    var done = function () {
      if (!btn) return;
      var old = btn.textContent;
      btn.textContent = "已复制";
      setTimeout(function () { btn.textContent = old; }, 1400);
    };
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(text); done(); });
        return;
      }
    } catch (_) {}
    fallbackCopy(text); done();
  }
  function fallbackCopy(text) {
    try {
      var ta = document.createElement("textarea");
      ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      document.execCommand("copy"); document.body.removeChild(ta);
    } catch (_) {}
  }
  if (promptEl) {
    promptEl.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submitQuestion(promptEl.value); }
    });
  }
  if (sendBtn) sendBtn.addEventListener("click", function () { submitQuestion(promptEl ? promptEl.value : ""); });
  if (stopBtn) stopBtn.addEventListener("click", stopStreaming);
  if (clearBtn) clearBtn.addEventListener("click", function () {
    if (!sessionsReady || streaming) return;
    var current = currentSession();
    if (current) current.clearedAt = nowTs();
    messages = []; saveMessages({ replaceMessages: true }); renderMessages();
  });
  if (modelSelect) {
    modelSelect.addEventListener("change", function () {
      updateStatusLine();
    });
  }
  if (groundingToggle) {
    try {
      var savedG = localStorage.getItem(AI_GROUNDING_KEY);
      if (savedG === "0") groundingToggle.checked = false;
      else if (savedG === "1") groundingToggle.checked = true;
      else groundingToggle.checked = true;   // 研究对话页默认开启接地，更贴「研究导向」
    } catch (_) { groundingToggle.checked = true; }
    groundingToggle.addEventListener("change", function () {
      try { localStorage.setItem(AI_GROUNDING_KEY, groundingToggle.checked ? "1" : "0"); } catch (_) {}
      if (scopeRow) scopeRow.hidden = !(runtimeEnabled() && depthAllowed(depth) && scopeRelevant());
    });
  }
  if (scopeChips) {
    scopeChips.addEventListener("click", function (e) {
      var btn = (e.target && e.target.closest) ? e.target.closest(".aip-chip") : null;
      var id = btn && btn.getAttribute("data-scope");
      if (id) onScopeChip(id);
    });
  }

  // ===================== 会话侧栏事件 =====================
  if (newChatBtn) newChatBtn.addEventListener("click", newSession);
  if (sessionsToggle) sessionsToggle.addEventListener("click", openSessionsDrawer);
  if (sessionsClose) sessionsClose.addEventListener("click", closeSessionsDrawer);
  if (sessionsScrim) sessionsScrim.addEventListener("click", closeSessionsDrawer);
  if (cloudConsentEl) cloudConsentEl.addEventListener("change", function () { setCloudConsent(!!cloudConsentEl.checked); });
  if (cleanupOldBtn) cleanupOldBtn.addEventListener("click", cleanupBeforeSelectedDate);
  if (exportRangeBtn) exportRangeBtn.addEventListener("click", exportSessionsInRange);
  if (sessionsListEl) sessionsListEl.addEventListener("click", function (e) {
    var row = e.target && e.target.closest ? e.target.closest(".aip-session") : null;
    if (!row) return;
    var id = row.getAttribute("data-sid");
    var actBtn = e.target && e.target.closest ? e.target.closest(".aip-session-acts button") : null;
    if (actBtn) {
      e.stopPropagation();
      var act = actBtn.getAttribute("data-act");
      if (act === "delete") {
        var rowSession = sessionsData.sessions.find(function (s) { return s.id === id; });
        var recoverable = rowSession && Number(rowSession.expiresAt || 0) > 0;
        if (window.confirm(recoverable
          ? "删除这条会话记录？云端会进入 " + cloudSync.recoveryDays + " 天回收区，必要时可联系管理员找回。"
          : "删除这条仅存本机的会话记录？删除后无法恢复。")) deleteSession(id);
      }
      else if (act === "rename") { renameSession(id); }
      else if (act === "extend") { extendCloudConversation(id); }
      else if (act === "export-html") { exportSessionHtml(id); }
      return;
    }
    switchSession(id);
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeSessionsDrawer(); });

  // 供全站导航判断「本页是否有在飞的生成」：在飞时点其它标签会改为新标签打开，不打断本页生成。
  window.__marxBusy = function () { return streaming; };

  // 页面关闭前同步写入纯正文应急副本；不依赖尚未完成的异步 IndexedDB/网络事务。
  function backupCurrentBeforeLeave() { var session = currentSession(); if (session) writeEmergencyTextBackup(session); }
  window.addEventListener("pagehide", backupCurrentBeforeLeave);
  document.addEventListener("visibilitychange", function () { if (document.visibilityState === "hidden") backupCurrentBeforeLeave(); });

  // ===================== 初始化 =====================
  ensureExportDateDefaults();
  setupSessionSync();
  sessionsLoadPromise = loadSessions().then(function (loaded) {
    messages = loaded || [];
    sessionsReady = true;
    renderMessages();
    renderSidebar();
    updateSendEnabled();
    checkStorageCapacity();
    return loadCloudStatus();
  }).catch(function () {
    messages = legacyLoadSessions();
    sessionsReady = true;
    renderMessages(); renderSidebar();
    updateSendEnabled();
    checkStorageCapacity();
    return loadCloudStatus();
  });
  loadConfig();
})();
