/* 「AI 研究对话」全屏页 —— 自包含脚本（命名空间 aip*，与全站抽屉零冲突）。
 *
 * 一条连续会话线程（复用抽屉的 localStorage 键 marx-ai-thread-v1，跨页/跨标签连贯），
 * 每条提问可选两档深度：
 *   · 快速问答 → /api/ai/search-chat（多轮、可选检索引文库接地）；扣「随心问」token 额度。
 *   · 研究综述 → /api/search/associative?mode=research（一次性深度长文 + 20–24 条真实引用）；
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

  // 共用记忆键（与抽屉一致，体验连贯）
  var AI_THREAD_KEY = "marx-ai-thread-v1";
  var AI_SESSIONS_KEY = "marx-ai-sessions-v2";   // 多会话记录，按账号 uid 隔离
  var AI_DB_NAME = "marx-ai-conversations-v1";   // v3 主存储：IndexedDB，每条会话独立写入
  var AI_DB_VERSION = 1;
  var AI_DB_SESSION_STORE = "sessions";
  var AI_DB_META_STORE = "meta";
  var AI_SYNC_KEY = "marx-ai-sessions-sync-v3";  // Safari 等无 BroadcastChannel 时的轻量通知
  var AI_MODEL_KEY = "marx-ai-model-v1";       // /ai 页「模型选择」：flash/pro/zhipu，默认 flash
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
      if (!ev.viewer_url) continue;
      var kindLabel = ev.kind === "paraphrase" ? "转述出处" : "逐字引文";
      var pageLabel = ev.printed_page ? ("第 " + ev.printed_page + " 页") : (ev.pdf_page ? ("第 " + ev.pdf_page + " 页（PDF）") : "");
      links.push('<a class="ai-citation-open" href="' + escAttr(ev.viewer_url) + '" target="_blank" rel="noopener">' +
        esc(kindLabel + (pageLabel ? "·" + pageLabel : "")) + " →</a>");
    }
    return links.join("");
  }
  function renderCitations(message) {
    var citations = message.citations;
    if (!Array.isArray(citations) || !citations.length) return "";
    var isResearch = message.kind === "research";
    var scope = message.groundingScope;
    var scopeNote = (scope && scope.applied && scope.label)
      ? '<span class="ai-citations-scope">· 检索范围：' + esc(scope.label) + (scope.manual ? "（手动指定）" : "（智能判断）") + "</span>"
      : "";
    var title = isResearch
      ? '引用原文 · 综述所据（' + citations.length + "）" + scopeNote
      : '引用原文 · 来自引文库（' + citations.length + "）" + scopeNote;
    // 引用条目默认折叠：标题做成开合按钮，点击展开全部条目（快速问答与研究综述通用）。
    // 每次 renderMessages 重绘会恢复默认折叠态（历史里不存开合状态，保持默认收起）。
    var cardsHtml = citations.map(function (c, i) {
      var idx = c.grounding_index || c.review_index || (i + 1);
      var cite = esc(pickCite(c));
      var dataAttr = citeDataAttr(c);   // 供「引用格式」切换时就地重写此条出处串
      var ctx = ctxHtml(c.context);
      var evLinks = isResearch ? renderEvidenceLinks(c.evidence) : "";
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
      return '<div class="ai-citation-item"><div class="ai-citation-cite">' + citeSpan + "</div>" +
        (ctx ? '<div class="ai-citation-ctx">' + ctx + "</div>" : "") + evLinks + actions + "</div>";
    }).join("");
    return '<div class="ai-citations aip-cites-collapsed">' +
      '<div class="ai-citations-head">' +
        '<button type="button" class="ai-citations-toggle" aria-expanded="false">' +
          '<span class="aip-cite-caret" aria-hidden="true">▸</span>' +
          '<span class="ai-citations-title">' + title + "</span>" +
          '<span class="aip-cite-hint">展开</span>' +
        "</button>" +
        citeFormatSelectHtml() +
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
  function renderMessages() {
    if (!messages.length) { messagesEl.innerHTML = emptyStateHtml(); return; }
    messagesEl.innerHTML = messages.map(function (message) {
      if (message.role === "user") return '<div class="msg user">' + esc(message.content) + "</div>";
      var isResearch = message.kind === "research";
      if (message.pending) {
        var pendMsg = isResearch ? "正在检索原著并生成研究综述（较慢，请稍候）" : "AI 正在思考";
        return '<div class="msg assistant pending' + (isResearch ? " research" : "") + '" aria-live="polite">' +
          '<span class="msg-title">' + esc(pendMsg) + '</span>' +
          '<span class="typing-dots" aria-label="生成中"><span></span><span></span><span></span></span></div>';
      }
      var kindPill = '<span class="msg-kind">' + (isResearch ? "研究综述" : "快速问答") + "</span>";
      return '<div class="msg assistant' + (isResearch ? " research" : "") + '">' +
        '<span class="msg-title">' + kindPill + "AI 回答</span>" +
        '<div class="msg-body' + (isResearch ? " aip-essay" : "") + '">' + renderBasicMarkdown(stripInlineCitations(message.content)) + "</div>" +
        renderWarnings(message.warnings) + renderCitations(message) + renderSources(message.sources) + "</div>";
    }).join("");
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
  function mergeMessages(existing, incoming) {
    var oldList = Array.isArray(existing) ? existing.slice() : [];
    var newList = Array.isArray(incoming) ? incoming.slice() : [];
    if (!oldList.length) return newList;
    if (!newList.length) return oldList;
    var common = 0;
    while (common < oldList.length && common < newList.length &&
           messageIdentity(oldList[common]) === messageIdentity(newList[common])) common++;
    var out = oldList.slice();
    var positions = {};
    for (var i = 0; i < out.length; i++) positions[messageIdentity(out[i])] = i;
    var start = common > 0 ? common : 0;
    for (var j = start; j < newList.length; j++) {
      var key = messageIdentity(newList[j]);
      if (positions[key] !== undefined) out[positions[key]] = newList[j];
      else { positions[key] = out.length; out.push(newList[j]); }
    }
    return out;
  }
  function compactCitation(citation, textOnly) {
    if (!citation || typeof citation !== "object") return null;
    if (textOnly) return null;
    var keep = ["grounding_index", "review_index", "citation", "citations", "viewer_url", "book", "volume",
      "title", "source_file", "pdf_page", "printed_page", "subject_label", "review_quoted", "review_quote_unmatched"];
    var out = {};
    keep.forEach(function (key) { if (citation[key] !== undefined) out[key] = citation[key]; });
    if (citation.context) out.context = String(citation.context).slice(0, 360);
    if (Array.isArray(citation.evidence)) {
      out.evidence = citation.evidence.slice(0, 3).map(function (ev) {
        return compactCitation(ev, false) || {};
      });
    }
    return out;
  }
  function compactSource(source) {
    if (!source || typeof source !== "object") return null;
    var out = {};
    ["title", "url", "citation", "site_name", "date"].forEach(function (key) {
      if (source[key] !== undefined) out[key] = source[key];
    });
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
      if (level === 1) {
        if (Array.isArray(message.citations)) out.citations = message.citations.map(function (c) { return compactCitation(c, false); }).filter(Boolean);
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
              title: "恢复的旧会话 · " + deriveTitle(oldThread), updatedAt: nowTs() - 1,
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
  function writeDbSession(snapshot, replaceMessages, level) {
    return new Promise(function (resolve, reject) {
      var tx = sessionDb.transaction(AI_DB_SESSION_STORE, "readwrite");
      var store = tx.objectStore(AI_DB_SESSION_STORE);
      var req = store.get(dbSessionKey(snapshot.id));
      var saved = null;
      req.onsuccess = function () {
        var existing = req.result || null;
        if (existing && existing.deletedAt) return;  // 另一标签页已删除：旧标签页不得用迟到写入将其复活
        var merged = replaceMessages ? snapshot.messages : mergeMessages(existing && existing.messages, snapshot.messages);
        merged = storedMessages(merged, level);
        saved = {
          key: dbSessionKey(snapshot.id), uid: storeSlot(), id: snapshot.id,
          title: snapshot.title || deriveTitle(merged), updatedAt: Number(snapshot.updatedAt || nowTs()),
          messages: merged, storageLevel: level,
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
          : "浏览器存储空间不足：会话正文已完整保存，引文附件未继续保存。建议删除不需要的旧会话。"
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
          id: s.id, title: s.title, updatedAt: s.updatedAt,
          messages: storedMessages(s.messages || [], level), storageLevel: level,
        };
      }),
    };
  }
  function writeLegacyTextBackup(records, currentId) {
    // 迁移成功后把旧 localStorage 副本收缩为“正文保险箱”：保留可读对话、释放大段引文上下文占用。
    // IndexedDB 是主存储；此副本仅在浏览器禁用 IndexedDB 时兜底，不再参与日常多标签写入。
    try {
      var store = loadStore();
      store[storeSlot()] = {
        current: currentId || "",
        sessions: (records || []).filter(function (r) { return !r.deletedAt; }).map(function (r) {
          return {
            id: r.id, title: r.title, updatedAt: r.updatedAt,
            messages: storedMessages(r.messages || [], 2), storageLevel: 2,
          };
        }),
      };
      localStorage.setItem(AI_SESSIONS_KEY, JSON.stringify(store));
    } catch (_) {}
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
      messages: (session.messages || []).slice(),
    };
    sessionWriteQueue = sessionWriteQueue.catch(function () {}).then(function () {
      if (sessionDb && !sessionDbFailed) return persistDbSession(snapshot, !!options.replaceMessages);
      persistLegacySafely();
      return snapshot;
    }).then(function (saved) {
      updateLocalSession(saved);
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
    if (!s) { s = { id: genId(), title: "新会话", updatedAt: nowTs(), messages: [] }; sessionsData.sessions.unshift(s); sessionsData.current = s.id; }
    s.messages = messages.filter(function (m) { return m && !m.pending; }).map(ensureMessageIdentity);
    s.title = deriveTitle(s.messages);
    s.updatedAt = nowTs();
    rememberCurrent();
    requestPersistentStorage();
    enqueueSessionWrite(s, options || {});
    renderSidebar();
  }
  function legacyLoadSessions() {
    var store = loadStore();
    var mine = store[storeSlot()];
    if (mine && mine.sessions && mine.sessions.length) {
      sessionsData = mine;
    } else {
      var legacy = [];
      try { legacy = (JSON.parse(localStorage.getItem(AI_THREAD_KEY) || "[]") || []).filter(function (m) { return m && !m.pending; }); } catch (_) {}
      var first = { id: genId(), title: legacy.length ? deriveTitle(legacy) : "新会话", updatedAt: nowTs(), messages: legacy };
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
      return Promise.all([readDbSessions(), readMigrationMeta()]);
    }).then(function (parts) {
      var records = parts[0] || [];
      var meta = parts[1] || {};
      records.sort(function (a, b) { return Number(b.updatedAt || 0) - Number(a.updatedAt || 0); });
      if (!records.length) {
        var firstId = genId();
        var first = { key: dbSessionKey(firstId), uid: storeSlot(), id: firstId, title: "新会话", updatedAt: nowTs(), messages: [], storageLevel: 0 };
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
  function switchSession(id) {
    if (streaming) { closeSessionsDrawer(); return; }
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
    if (streaming) return;
    var cur = currentSession();
    if (!(cur && (!cur.messages || !cur.messages.length))) {   // 当前已是空新会话则复用，不堆叠空会话
      var s = { id: genId(), title: "新会话", updatedAt: nowTs(), messages: [] };
      sessionsData.sessions.unshift(s); sessionsData.current = s.id; rememberCurrent();
      enqueueSessionWrite(s, { replaceMessages: true });
    }
    messages = []; renderMessages(); renderSidebar(); closeSessionsDrawer();
    if (promptEl) { try { promptEl.focus(); } catch (_) {} }
  }
  function deleteSession(id) {
    var idx = -1, i;
    for (i = 0; i < sessionsData.sessions.length; i++) if (sessionsData.sessions[i].id === id) idx = i;
    if (idx < 0) return;
    sessionsData.sessions.splice(idx, 1);
    deleteDbSession(id);
    if (sessionsData.current === id) {
      if (!sessionsData.sessions.length) sessionsData.sessions.unshift({ id: genId(), title: "新会话", updatedAt: nowTs(), messages: [] });
      sessionsData.current = sessionsData.sessions[0].id;
      var s = currentSession(); messages = s ? (s.messages || []).slice() : [];
      renderMessages();
      rememberCurrent();
      if (s && sessionsData.sessions.length === 1 && !s.messages.length) enqueueSessionWrite(s, { replaceMessages: true });
    }
    renderSidebar();
  }
  function renameSession(id) {
    var s = null, i;
    for (i = 0; i < sessionsData.sessions.length; i++) if (sessionsData.sessions[i].id === id) s = sessionsData.sessions[i];
    if (!s) return;
    var row = sessionsListEl ? sessionsListEl.querySelector('.aip-session[data-sid="' + id + '"] .aip-session-main') : null;
    if (!row) return;
    var input = document.createElement("input");
    input.className = "aip-session-rename"; input.value = s.title || "";
    row.innerHTML = ""; row.appendChild(input); input.focus(); input.select();
    function commit() { s.title = (input.value || "").trim() || s.title; s.updatedAt = nowTs(); enqueueSessionWrite(s, { replaceMessages: true }); renderSidebar(); }
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
      return '<div class="aip-session' + (s.id === sessionsData.current ? " active" : "") + '" data-sid="' + esc(s.id) + '">'
        + '<div class="aip-session-main"><div class="aip-session-title">' + esc(s.title || deriveTitle(s.messages || [])) + '</div>'
        + '<div class="aip-session-time">' + esc(relTime(s.updatedAt)) + '</div></div>'
        + '<div class="aip-session-acts">'
        + '<button type="button" data-act="rename" title="重命名">✎</button>'
        + '<button type="button" data-act="delete" title="删除">🗑</button>'
        + '</div></div>';
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
  function hasChat() { return !!(config && config.access); }
  function hasResearch() { return !!(config && config.research_access); }
  function anyAccess() { return hasChat() || hasResearch(); }
  function runtimeEnabled() { return !!(config && config.runtime && config.runtime.enabled); }
  function depthAllowed(d) { return d === "research" ? hasResearch() : hasChat(); }
  function currentModelChoice() {
    var v = modelSelect ? modelSelect.value : "flash";
    var research = depth === "research";
    if (v === "zhipu" && !webAccess()) return research ? "pro" : "flash";   // 无联网权限智谱不可用
    if (research && v === "flash") return "pro";                            // 研究综述剔除 flash，回落 pro
    if (v === "pro" || v === "zhipu") return v;
    return research ? "pro" : "flash";
  }
  // 研究综述剔除 flash：切到研究档时隐藏 flash 选项、把「正选 flash」切到 pro；切回快速档恢复偏好(默认 flash)。
  var flashOpt = modelSelect ? modelSelect.querySelector('option[value="flash"]') : null;
  function syncModelForDepth() {
    if (!modelSelect) return;
    var research = depth === "research";
    if (flashOpt) { flashOpt.hidden = research; flashOpt.disabled = research; }
    if (research) {
      if (modelSelect.value === "flash") modelSelect.value = "pro";
    } else {
      var saved = "flash";
      try { saved = localStorage.getItem(AI_MODEL_KEY) || "flash"; } catch (_) {}
      if (["flash", "pro", "zhipu"].indexOf(saved) < 0) saved = "flash";
      if (saved === "zhipu" && !webAccess()) saved = "flash";
      if (modelSelect.querySelector('option[value="' + saved + '"]')) modelSelect.value = saved;
    }
  }
  function currentProvider() { return currentModelChoice() === "zhipu" ? "zhipu" : "deepseek"; }
  // DeepSeek 档位（flash/pro）传给后端做白名单覆盖；智谱通道返回 null（模型由服务端 zhipu 路由处理）。
  function currentDeepseekModel() {
    var c = currentModelChoice();
    if (c === "flash") return "deepseek-v4-flash";
    if (c === "pro") return "deepseek-v4-pro";
    return null;
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
  function applyDepthUI() {
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
    syncModelForDepth();
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
    applyDepthUI();
  }

  function updateStatusLine() {
    if (!runtimeEnabled() || !anyAccess()) return;
    var rt = config.runtime;
    var c = currentModelChoice();
    if (!modelBadge) return;
    if (c === "zhipu") modelBadge.textContent = "智谱 " + (rt.zhipu_model || "GLM-5.1");
    else if (c === "pro") modelBadge.textContent = "DeepSeek Pro";
    else modelBadge.textContent = "DeepSeek Flash";
  }
  function updateSendEnabled() {
    var canUse = runtimeEnabled() && depthAllowed(depth);
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

    // 模型选择：无联网权限则移除「智谱」项，只留 flash/pro；有则更新其显示名。
    if (zhipuOpt) {
      if (!webAccess()) {
        if (zhipuOpt.parentNode) zhipuOpt.parentNode.removeChild(zhipuOpt);
        zhipuOpt = null;
      } else if (c.runtime && c.runtime.zhipu_model) {
        zhipuOpt.textContent = "智谱 " + c.runtime.zhipu_model + "（可联网检索）";
      }
    }
    // 恢复保存的模型选择（默认 flash；保存值为 zhipu 但无联网权限则回落 flash）。
    if (modelSelect) {
      var savedModel = "flash";
      try { savedModel = localStorage.getItem(AI_MODEL_KEY) || "flash"; } catch (_) {}
      if (["flash", "pro", "zhipu"].indexOf(savedModel) < 0) savedModel = "flash";
      if (savedModel === "zhipu" && !webAccess()) savedModel = "flash";
      modelSelect.value = modelSelect.querySelector('option[value="' + savedModel + '"]') ? savedModel : "flash";
    }
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
    applyDepthUI();
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
  function buildHistory() {
    return messages
      .filter(function (m) { return !m.pending && (m.role === "user" || m.role === "assistant"); })
      .map(function (m) {
        var content = String(m.content || "");
        if (content.length > HISTORY_CHAR_CAP) content = content.slice(0, HISTORY_CHAR_CAP) + "……（此处略）";
        return { role: m.role, content: content };
      });
}

  function submitQuestion(text) {
    if (!runtimeEnabled() || !depthAllowed(depth) || streaming) return;
    var question = String(text || "").trim();
    if (!question) return;
    var history = buildHistory();
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
          body: JSON.stringify({ q: question, gist: question, mode: "research", messages: history, scope: currentScope(), rerank: true, provider: currentProvider(), model: currentDeepseekModel() }),
          signal: abort ? abort.signal : undefined,
        })
      : apiFetch("/api/ai/search-chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ question: question, messages: history, provider: currentProvider(), model: currentDeepseekModel(), grounding: currentGrounding(), scope: currentScope() }),
          signal: abort ? abort.signal : undefined,
        });

    req.then(function (resp) {
      var ctype = (resp.headers.get("content-type") || "").toLowerCase();
      if (ctype.indexOf("text/event-stream") >= 0) {
        return resp.text().then(function (t) {
          var data = parseSseResult(t);
          if (!data) throw new Error("回答生成超时或服务繁忙，请稍后重试。");
          return data;
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
    if (streaming) return;
    messages = []; saveMessages({ replaceMessages: true }); renderMessages();
  });
  if (modelSelect) {
    // 初值由 applyConfig 依权限与 localStorage 恢复；此处仅记忆用户切换。
    modelSelect.addEventListener("change", function () {
      try { localStorage.setItem(AI_MODEL_KEY, modelSelect.value); } catch (_) {}
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
  if (sessionsListEl) sessionsListEl.addEventListener("click", function (e) {
    var row = e.target && e.target.closest ? e.target.closest(".aip-session") : null;
    if (!row) return;
    var id = row.getAttribute("data-sid");
    var actBtn = e.target && e.target.closest ? e.target.closest(".aip-session-acts button") : null;
    if (actBtn) {
      e.stopPropagation();
      var act = actBtn.getAttribute("data-act");
      if (act === "delete") { if (window.confirm("删除这条会话记录？不可恢复。")) deleteSession(id); }
      else if (act === "rename") { renameSession(id); }
      return;
    }
    switchSession(id);
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeSessionsDrawer(); });

  // 供全站导航判断「本页是否有在飞的生成」：在飞时点其它标签会改为新标签打开，不打断本页生成。
  window.__marxBusy = function () { return streaming; };

  // ===================== 初始化 =====================
  setupSessionSync();
  loadSessions().then(function (loaded) {
    messages = loaded || [];
    renderMessages();
    renderSidebar();
  }).catch(function () {
    messages = legacyLoadSessions();
    renderMessages(); renderSidebar();
  });
  loadConfig();
})();
