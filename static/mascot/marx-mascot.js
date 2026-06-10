/* ===========================================================================
   Marx mascot widget — behaviour engine (Phase 1)

   Phase 1 (this file):
     - Guest (logged out): Marx sits on a bench wearing his top hat; clicking him
       shows a Marx-flavoured remark inviting the visitor to log in.
     - Member (logged in): on first page after login he stands up and doffs his
       hat, then cycles through a repertoire of actions (reading, tea, speech,
       marching, writing, ...). Clicking him shows a Marx-style aphorism.
     - He can be dismissed/hidden to a small round dock ("收纳口"); clicking the
       dock brings him back. The preference persists across pages.

   Phase 2 hooks (left as a stable public API on window.MarxMascot):
     - play / cycle / stop / say / dock / summon
     - registerAction(...)         add new actions
     - setOutfit(id)               clothing changes (CSS data-outfit hook)
     - on / off / emit             event bus (click, action, gift, companion...)
     - giveItem(item)              strategy interactions (give an item / trigger)
     - addCompanion(cfg)           more characters joining (placeholder)
     - openDialogue()              real conversation (wired to AI endpoints later)
   =========================================================================== */
(function () {
  'use strict';

  var root = document.getElementById('marx-mascot-root');
  if (!root || root.dataset.mxInit === '1') { return; }
  root.dataset.mxInit = '1';

  var stage      = root.querySelector('.mx-stage');
  var dock       = root.querySelector('.mx-dock');
  var bubble     = root.querySelector('.mx-bubble');
  var bubbleText = root.querySelector('.mx-bubble-text');
  var caption    = root.querySelector('.mx-caption');
  var closeBtn   = root.querySelector('.mx-close');
  var chipNext   = root.querySelector('.mx-chip-next');
  var chipHide   = root.querySelector('.mx-chip-hide');
  var chipTalk   = root.querySelector('.mx-chip-talk');
  var chipSkip   = root.querySelector('.mx-chip-skip');
  var chipSend   = root.querySelector('.mx-chip-send');
  var inputRow   = root.querySelector('.mx-input-row');
  var inputEl    = root.querySelector('.mx-input');

  var LS_DOCK   = 'mx-docked-v1';
  var LS_UID    = 'mx-last-uid-v1';
  var LS_AI     = 'mx-ai-stats-v1';

  var isLoggedIn   = root.dataset.loggedIn === 'true';
  var displayName  = (root.dataset.name || '').trim();
  var userId       = (root.dataset.uid || '').trim();
  var defaultDock  = root.dataset.defaultDocked === 'true';
  var aiAccess     = root.dataset.ai === 'true';
  var csrfToken    = (root.dataset.csrf || '').trim();
  var aiAvailable  = isLoggedIn && aiAccess;
  var reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ------------------------------------------------------------- content -- */
  var ACTIONS = [
    { id: 'reading', label: '研读《资本论》' },
    { id: 'tea',     label: '啜一盏清茶' },
    { id: 'speech',  label: '发表演讲' },
    { id: 'march',   label: '投身游行' },
    { id: 'writing', label: '撰写手稿' },
    { id: 'cigar',   label: '抽一支雪茄' },
    { id: 'ponder',  label: '捋须沉思' },
    { id: 'chess',   label: '对弈一局' },
    { id: 'news',    label: '翻阅《新莱茵报》' },
    { id: 'toast',   label: '举杯祝酒' },
    { id: 'chalk',   label: '黑板推演' },
    { id: 'letter',  label: '致信恩格斯' },
    { id: 'pacing',  label: '踱步思索' },
    { id: 'wave',    label: '挥手致意' },
    { id: 'doze',    label: '伏案小憩' }
  ];

  var QUOTES = [
    '哲学家们只是用不同的方式解释世界，而问题在于改变世界。',
    '怀疑一切。',
    '人的本质，是一切社会关系的总和。',
    '全世界无产者，联合起来！你们失去的只是锁链，赢得的将是整个世界。',
    '历史不过是追求着自己目的的人的活动而已。',
    '理论一经掌握群众，就会变成物质力量。',
    '时间，是人类发展的空间。',
    '一切坚固的东西都烟消云散了。',
    '在科学的入口处，正像在地狱的入口处一样，必须根绝一切犹豫。',
    '任何节约，归根到底都是时间的节约。',
    '这本《资本论》，我写了不止一盏茶的工夫。',
    '雪茄是会思想的芦苇的一点奢侈——恩格斯总笑我抽得太凶。'
  ];

  var GUEST_QUOTES = [
    '同志，你尚未登录——正如尚未觉醒的阶级，只能在长椅上旁观历史。登录之后，我便起身为你演讲。',
    '我在这长椅上已久候多时。请登录，让这具形象动起来吧。',
    '哲学家们只是解释世界；而你若想看我改变姿态，得先登录。',
    '无产者在登录中失去的只是一个表单，得到的却是一个会动的马克思。',
    '门票即注册，自由始于登录。来吧，同志。',
    '且容我戴帽静坐。待你登录，我自当脱帽相迎。'
  ];

  var DOCK_LINE   = '好，我暂退幕后——但一个幽灵仍在游荡。点那圆球，我便归来。';
  var SUMMON_LINE = '我又回来了。继续吧，同志。';
  function introLine() {
    return displayName
      ? '「' + displayName + '」同志，欢迎归来！且容我起身、脱帽——为你，也为全世界的无产者。'
      : '同志，欢迎归来！且容我起身、脱帽致意。';
  }

  /* --------------------------------------------------------- event bus ---- */
  var listeners = {};
  function on(type, fn) { (listeners[type] = listeners[type] || []).push(fn); return api; }
  function off(type, fn) {
    var arr = listeners[type]; if (!arr) { return api; }
    listeners[type] = arr.filter(function (f) { return f !== fn; });
    return api;
  }
  function emit(type, detail) {
    (listeners[type] || []).forEach(function (fn) {
      try { fn(detail); } catch (e) { /* never let a listener break the mascot */ }
    });
    return api;
  }

  /* ----------------------------------------------------------- helpers ---- */
  var actionIds = ACTIONS.map(function (a) { return a.id; });
  var SPECIAL = ['tiphat'];
  function clearActions() {
    actionIds.concat(SPECIAL).forEach(function (id) { root.classList.remove('mx-act-' + id); });
  }
  function labelFor(id) {
    for (var i = 0; i < ACTIONS.length; i++) { if (ACTIONS[i].id === id) { return ACTIONS[i].label; } }
    return '';
  }

  var posture = null;            // 'seated' | 'standing'
  function setPosture(p) {
    posture = p;
    root.classList.toggle('mx-seated', p === 'seated');
    root.classList.toggle('mx-standing', p === 'standing');
  }

  var captionTimer = null;
  function showCaption(text) {
    if (!caption) { return; }
    caption.textContent = text || '';
    if (!text) { caption.classList.remove('mx-show'); return; }
    caption.classList.add('mx-show');
    clearTimeout(captionTimer);
    captionTimer = setTimeout(function () { caption.classList.remove('mx-show'); }, 1800);
  }

  var currentAction = null;
  function playAction(id) {
    clearActions();
    if (id) {
      root.classList.add('mx-act-' + id);
      currentAction = id;
      var lbl = labelFor(id);
      if (lbl) { showCaption(lbl); }
      emit('action', { id: id, label: lbl });
    } else {
      currentAction = null;
    }
  }

  /* --------------------------------------------------------- action loop -- */
  var cycleTimer = null;
  var lastIdx = -1;
  function nextActionId() {
    if (actionIds.length <= 1) { return actionIds[0]; }
    var idx;
    do { idx = Math.floor(Math.random() * actionIds.length); } while (idx === lastIdx);
    lastIdx = idx;
    return actionIds[idx];
  }
  function scheduleNext() {
    clearTimeout(cycleTimer);
    var delay = reduceMotion ? 9000 : (6500 + Math.floor(Math.random() * 4000));
    cycleTimer = setTimeout(tick, delay);
  }
  function tick() {
    if (root.classList.contains('mx-docked')) { return; }
    playAction(nextActionId());
    scheduleNext();
  }
  function startCycle() {
    if (root.classList.contains('mx-docked')) { return; }
    setPosture('standing');
    tick();
  }
  function stopCycle() {
    clearTimeout(cycleTimer);
    clearActions();
    currentAction = null;
  }

  /* ------------------------------------------------------------- bubble --- */
  var bubbleTimer = null;
  function say(text, opts) {
    opts = opts || {};
    if (!bubble || !bubbleText) { return api; }
    if (root.classList.contains('mx-docked') && !opts.force) { return api; }
    bubbleText.textContent = text;
    bubble.hidden = false;
    /* allow the element to lay out before transitioning in */
    window.requestAnimationFrame(function () { bubble.classList.add('mx-show'); });
    clearTimeout(bubbleTimer);
    if (!opts.sticky) {
      var ms = opts.duration || Math.min(9000, 3200 + text.length * 110);
      bubbleTimer = setTimeout(hideBubble, ms);
    }
    emit('say', { text: text });
    return api;
  }
  function hideBubble() {
    if (!bubble) { return; }
    bubble.classList.remove('mx-show');
    clearTimeout(bubbleTimer);
    bubbleTimer = setTimeout(function () { bubble.hidden = true; }, 240);
  }

  var guestIdx = -1, quoteIdx = -1;
  function nextFrom(list, ref) {
    if (!list.length) { return ''; }
    ref.i = (ref.i + 1) % list.length;
    return list[ref.i];
  }
  var guestRef = { i: -1 }, quoteRef = { i: -1 };
  function speakContextual() {
    if (isLoggedIn) { say(nextFrom(QUOTES, quoteRef)); }
    else { say(nextFrom(GUEST_QUOTES, guestRef)); }
  }

  /* =======================================================================
     DIALOGUE ENGINE (Phase 2) — deepseek-backed, heavily throttled.
     Modes hit /api/ai/mascot-chat: scene | invite | evaluate | ask.
     ======================================================================= */

  var SELFTALK = [
    '罢了，看来你正专注于更要紧的阅读——这正是我乐见的。',
    '沉默也是一种回答。当年恩格斯不回信时，我也这样安慰自己。',
    '无妨，问题会自己发酵，改日再谈。',
    '你继续用功，我在这儿抽我的雪茄。',
    '思想者各有自己的时区，不打扰了。',
    '好吧，这个问题留给历史去回答。'
  ];
  var selfRef = { i: -1 };

  var AI_LIMITS = {
    dailyTotal: 30,                    /* passive AI calls per browser per day */
    sceneDaily: 12,
    sceneGlobalGapMs: 150 * 1000,
    inviteDaily: 3,
    inviteGapMs: 18 * 60 * 1000,
    inviteFirstDelayMs: 4 * 60 * 1000,
    inviteReplyWindowMs: 75 * 1000,
    askMinGapMs: 15 * 1000
  };
  /* per-scene-kind cooldowns (ms) */
  var KIND_COOLDOWN = {
    search: 8 * 60 * 1000, reading: 20 * 60 * 1000, longread: 30 * 60 * 1000,
    idle: 30 * 60 * 1000, latenight: 6 * 60 * 60 * 1000, library: 15 * 60 * 1000,
    journal: 15 * 60 * 1000, pricing: 15 * 60 * 1000, dictionary: 10 * 60 * 1000,
    account: 30 * 60 * 1000
  };

  function todayKey() {
    var d = new Date();
    return d.getFullYear() + '-' + (d.getMonth() + 1) + '-' + d.getDate();
  }
  function loadStats() {
    var s = null;
    try { s = JSON.parse(localStorage.getItem(LS_AI) || 'null'); } catch (e) {}
    if (!s || s.day !== todayKey()) {
      s = { day: todayKey(), total: 0, scene: 0, invite: 0, kinds: {}, lastSceneTs: 0, lastInviteTs: 0, lastAskTs: 0 };
    }
    return s;
  }
  function saveStats(s) { try { localStorage.setItem(LS_AI, JSON.stringify(s)); } catch (e) {} }

  function aiCall(payload, cb) {
    var headers = { 'Content-Type': 'application/json' };
    if (csrfToken) { headers['X-CSRF-Token'] = csrfToken; }
    fetch('/api/ai/mascot-chat', { method: 'POST', headers: headers, body: JSON.stringify(payload) })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { cb(d && d.ok && d.text ? String(d.text) : null); })
      .catch(function () { cb(null); });
  }

  /* ----- dialogue UI state ----- */
  var round = null;          /* null | {type:'invite', invitation} | {type:'ask'} */
  var roundBusy = false;
  var replyTimer = null;

  function setChips(mode) {
    /* mode: 'default' | 'invite' | 'ask' | 'busy' */
    if (!chipNext) { return; }
    chipNext.hidden = mode !== 'default';
    chipTalk.hidden = mode !== 'default';
    chipSkip.hidden = mode !== 'invite';
    inputRow.hidden = (mode !== 'invite' && mode !== 'ask');
    inputEl.disabled = chipSend.disabled = (mode === 'busy');
  }
  function setThinking(on) {
    if (bubble) { bubble.classList.toggle('mx-thinking', !!on); }
  }
  function endRound() {
    clearTimeout(replyTimer);
    round = null; roundBusy = false;
    setThinking(false);
    setChips('default');
    if (inputEl) { inputEl.value = ''; }
  }
  function cancelRound() { endRound(); hideBubble(); }

  /* ----- (2) proactive invitation round ----- */
  function startInviteRound() {
    var s = loadStats();
    s.invite += 1; s.total += 1; s.lastInviteTs = Date.now(); saveStats(s);
    round = { type: 'invite', invitation: '' }; roundBusy = true;
    say('（马克思放下笔，似乎想同你聊聊）', { sticky: true });
    setThinking(true); setChips('busy');
    aiCall({ mode: 'invite' }, function (text) {
      setThinking(false);
      if (!text || !round || round.type !== 'invite') { endRound(); hideBubble(); return; }
      round.invitation = text; roundBusy = false;
      say(text, { sticky: true });
      setChips('invite');
      replyTimer = setTimeout(function () { concludeInvite(false); }, AI_LIMITS.inviteReplyWindowMs);
    });
  }
  function concludeInvite(viaSkip) {
    /* reader stayed silent or skipped: Marx rounds it off locally (no tokens) */
    clearTimeout(replyTimer);
    var line = nextFrom(SELFTALK, selfRef);
    endRound();
    say(line, { duration: 5200 });
  }
  function submitInviteReply(textVal) {
    var inv = round && round.invitation;
    clearTimeout(replyTimer);
    roundBusy = true; setChips('busy'); setThinking(true);
    say('（他捻着胡须听完了你的话）', { sticky: true });
    aiCall({ mode: 'evaluate', invitation: inv, user_text: textVal }, function (text) {
      setThinking(false);
      endRound();
      say(text || '你的话我记下了。思想需要时间发酵，我们改日再谈。', { duration: 11000 });
    });
  }

  /* ----- (3) user-initiated ask round ----- */
  function startAskRound() {
    if (!aiAvailable) {
      say(isLoggedIn ? '与我对谈的权限尚未开通——到会员页看看吧，那里有入场券。'
                     : '待你登录，我们再促膝长谈。', { duration: 6000 });
      return;
    }
    if (round) { return; }
    var s = loadStats();
    if (Date.now() - (s.lastAskTs || 0) < AI_LIMITS.askMinGapMs) {
      say('稍等，让我先把这口茶喝完。', { duration: 3200 });
      return;
    }
    round = { type: 'ask' };
    say('请讲，我在听。', { sticky: true });
    setChips('ask');
    if (inputEl) { try { inputEl.focus(); } catch (e) {} }
  }
  function submitAsk(textVal) {
    var s = loadStats();
    s.lastAskTs = Date.now(); saveStats(s);
    roundBusy = true; setChips('busy'); setThinking(true);
    say('（马克思沉吟着）', { sticky: true });
    aiCall({ mode: 'ask', user_text: textVal }, function (text) {
      setThinking(false);
      endRound();
      say(text || '这个问题值得用一整个下午来谈，可惜眼下网络的邮路不通。换个问法试试？', { duration: 14000 });
    });
  }

  function submitInput() {
    if (!round || roundBusy || !inputEl) { return; }
    var v = (inputEl.value || '').trim().slice(0, 120);
    if (!v) { return; }
    if (round.type === 'invite') { submitInviteReply(v); }
    else if (round.type === 'ask') { submitAsk(v); }
  }

  /* ----- (1) scene-aware remarks ----- */
  function sceneEligible(kind) {
    if (!aiAvailable || document.hidden || round) { return false; }
    if (root.classList.contains('mx-docked')) { return false; }
    var s = loadStats();
    if (s.total >= AI_LIMITS.dailyTotal || s.scene >= AI_LIMITS.sceneDaily) { return false; }
    if (Date.now() - (s.lastSceneTs || 0) < AI_LIMITS.sceneGlobalGapMs) { return false; }
    var kindTs = (s.kinds || {})[kind] || 0;
    if (Date.now() - kindTs < (KIND_COOLDOWN[kind] || 10 * 60 * 1000)) { return false; }
    return true;
  }
  function triggerScene(kind, detail) {
    if (!sceneEligible(kind)) { return; }
    var s = loadStats();
    s.scene += 1; s.total += 1; s.lastSceneTs = Date.now();
    s.kinds = s.kinds || {}; s.kinds[kind] = Date.now(); saveStats(s);
    aiCall({ mode: 'scene', scene: { kind: kind, detail: (detail || '').slice(0, 60) } }, function (text) {
      if (!text || round || root.classList.contains('mx-docked')) { return; }
      say(text, { duration: 9000 });
      emit('scene', { kind: kind });
    });
  }

  function pageKind() {
    var p = location.pathname;
    if (p === '/' || p === '/index') { return 'index'; }
    if (p.indexOf('/viewer') === 0 || p.indexOf('/reader') === 0 || p.indexOf('/pdf') === 0) { return 'viewer'; }
    if (p.indexOf('/library') === 0) { return 'library'; }
    if (p.indexOf('/dictionary') === 0) { return 'dictionary'; }
    if (p.indexOf('/pricing') === 0) { return 'pricing'; }
    if (p.indexOf('/journal') === 0 || p.indexOf('/account/journal') === 0) { return 'journal'; }
    if (p.indexOf('/account') === 0) { return 'account'; }
    return 'other';
  }

  function setupSceneTriggers() {
    if (!aiAvailable) { return; }
    var kind = pageKind();

    /* search submit on the home page */
    var form = document.getElementById('searchForm');
    var q = document.getElementById('q');
    if (form && q) {
      form.addEventListener('submit', function () {
        var v = (q.value || '').trim().slice(0, 40);
        if (v) { setTimeout(function () { triggerScene('search', v); }, 2500); }
      });
    }

    /* reading scenes in the viewer */
    if (kind === 'viewer') {
      var title = (document.title || '').split(' - ')[0].slice(0, 40);
      setTimeout(function () { triggerScene('reading', title); }, 25 * 1000);
      setTimeout(function () { triggerScene('longread', title); }, 12 * 60 * 1000);
    }

    /* page-presence scenes */
    var presence = { library: 1, journal: 1, pricing: 1, dictionary: 1, account: 1 };
    if (presence[kind]) {
      setTimeout(function () { triggerScene(kind, ''); }, 9 * 1000);
    }

    /* late-night studying */
    var h = new Date().getHours();
    if (h >= 23 || h < 5) {
      setTimeout(function () { triggerScene('latenight', ''); }, 45 * 1000);
    }

    /* idle watcher: 5 min without input, once per page load */
    var idleFired = false;
    var idleTimer = null;
    function resetIdle() {
      if (idleFired) { return; }
      clearTimeout(idleTimer);
      idleTimer = setTimeout(function () {
        idleFired = true;
        triggerScene('idle', '');
      }, 5 * 60 * 1000);
    }
    ['mousemove', 'keydown', 'scroll', 'click', 'touchstart'].forEach(function (ev) {
      document.addEventListener(ev, resetIdle, { passive: true });
    });
    resetIdle();
  }

  /* ----- invite scheduler ----- */
  var loadTs = Date.now();
  function setupInviteScheduler() {
    if (!aiAvailable) { return; }
    setInterval(function () {
      if (document.hidden || round || root.classList.contains('mx-docked')) { return; }
      if (Date.now() - loadTs < AI_LIMITS.inviteFirstDelayMs) { return; }
      var s = loadStats();
      if (s.total >= AI_LIMITS.dailyTotal || s.invite >= AI_LIMITS.inviteDaily) { return; }
      if (Date.now() - (s.lastInviteTs || 0) < AI_LIMITS.inviteGapMs) { return; }
      if (bubble && !bubble.hidden) { return; }   /* don't talk over an open bubble */
      startInviteRound();
    }, 60 * 1000);
  }

  /* ------------------------------------------------------- dock / summon -- */
  function dockMascot(withLine) {
    root.classList.add('mx-docked');
    endRound();
    stopCycle();
    hideBubble();
    try { localStorage.setItem(LS_DOCK, '1'); } catch (e) {}
    emit('dock', {});
    if (withLine) {
      /* fire the farewell from the dock so it is visible after collapse */
      setTimeout(function () { say(DOCK_LINE, { force: true, duration: 4200 }); }, 60);
    }
  }
  function summon() {
    root.classList.remove('mx-docked');
    try { localStorage.setItem(LS_DOCK, '0'); } catch (e) {}
    emit('summon', {});
    if (isLoggedIn) { startCycle(); say(SUMMON_LINE); }
    else { setPosture('seated'); }
  }

  /* --------------------------------------------------------------- intro -- */
  function playIntro() {
    setPosture('standing');
    clearActions();
    root.classList.add('mx-act-tiphat');
    say(introLine(), { duration: 5200 });
    setTimeout(function () {
      root.classList.remove('mx-act-tiphat');
      startCycle();
    }, reduceMotion ? 400 : 2400);
  }

  /* ---------------------------------------------------------------- wire -- */
  function onInteract() {
    emit('interact', { loggedIn: isLoggedIn, action: currentAction });
    /* an active dialogue round owns the bubble — don't talk over it */
    if (round) { return; }
    /* Phase 2: if a custom dialogue handler is attached, defer to it instead. */
    if (typeof api.dialogueHandler === 'function') { api.dialogueHandler(api); return; }
    speakContextual();
  }

  if (stage) {
    stage.addEventListener('click', onInteract);
    stage.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') { e.preventDefault(); onInteract(); }
    });
  }
  if (closeBtn) { closeBtn.addEventListener('click', function (e) { e.stopPropagation(); dockMascot(true); }); }
  if (dock) { dock.addEventListener('click', summon); }
  if (chipNext) { chipNext.addEventListener('click', function (e) { e.stopPropagation(); speakContextual(); }); }
  if (chipHide) { chipHide.addEventListener('click', function (e) { e.stopPropagation(); cancelRound(); }); }
  if (chipTalk) { chipTalk.addEventListener('click', function (e) { e.stopPropagation(); startAskRound(); }); }
  if (chipSkip) { chipSkip.addEventListener('click', function (e) { e.stopPropagation(); concludeInvite(true); }); }
  if (chipSend) { chipSend.addEventListener('click', function (e) { e.stopPropagation(); submitInput(); }); }
  if (inputEl) {
    inputEl.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); submitInput(); }
      e.stopPropagation();
    });
    inputEl.addEventListener('click', function (e) { e.stopPropagation(); });
  }

  /* pause the loop while the tab is hidden (saves cycles & battery) */
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) { clearTimeout(cycleTimer); }
    else if (isLoggedIn && !root.classList.contains('mx-docked')) { scheduleNext(); }
  });

  /* --------------------------------------------------------- public API --- */
  var api = {
    el: root,
    actions: ACTIONS,
    config: {
      aiAccess: aiAccess,
      endpoints: { searchChat: '/api/ai/search-chat', associative: '/api/search/associative' }
    },
    dialogueHandler: null,   /* Phase 2: assign a fn(api) to take over clicks */

    play: function (id) { stopCycleForManual(); playAction(id); return api; },
    cycle: function () { startCycle(); return api; },
    stop: function () { stopCycle(); return api; },
    say: function (text, opts) { return say(text, opts); },
    dock: function () { dockMascot(false); return api; },
    summon: function () { summon(); return api; },

    registerAction: function (action) {
      if (!action || !action.id) { return api; }
      ACTIONS.push({ id: action.id, label: action.label || '' });
      actionIds = ACTIONS.map(function (a) { return a.id; });
      emit('register', action);
      return api;
    },
    setOutfit: function (outfitId) {            /* clothing changes (Phase 2) */
      root.setAttribute('data-outfit', outfitId || 'default');
      emit('outfit', { outfit: outfitId });
      return api;
    },
    giveItem: function (item, meta) {           /* strategy interaction (Phase 2) */
      emit('gift', { item: item, meta: meta || null });
      return api;
    },
    addCompanion: function (cfg) {              /* more characters (Phase 2) */
      emit('companion', cfg || {});
      return api;
    },
    openDialogue: function () {                 /* real conversation */
      if (typeof api.dialogueHandler === 'function') { return api.dialogueHandler(api); }
      startAskRound();
      return api;
    },
    triggerScene: function (kind, detail) {     /* let pages fire custom scenes */
      triggerScene(kind, detail);
      return api;
    },
    startInvite: function () {                  /* manual invitation (ops/testing) */
      if (aiAvailable && !round && !root.classList.contains('mx-docked')) { startInviteRound(); }
      return api;
    },

    on: on, off: off, emit: emit,
    snapshot: function () {
      return {
        posture: posture,
        action: currentAction,
        docked: root.classList.contains('mx-docked'),
        loggedIn: isLoggedIn
      };
    }
  };
  function stopCycleForManual() { clearTimeout(cycleTimer); }
  window.MarxMascot = api;

  /* ----------------------------------------------------------------- init - */
  function rememberUser() { try { localStorage.setItem(LS_UID, userId); } catch (e) {} }
  function forgetUser()   { try { localStorage.setItem(LS_UID, ''); } catch (e) {} }

  function init() {
    root.classList.remove('mx-state-loading');

    var pref = null;
    try { pref = localStorage.getItem(LS_DOCK); } catch (e) {}
    var startDocked = pref === '1' || (pref === null && defaultDock);

    /* "Fresh login" = we are logged in as a user we have not greeted yet.
       Guest pages (the login page included) reset the marker via forgetUser(),
       so the welcome fires on EVERY login — every login passes through a
       logged-out state first — and once when arriving already-logged-in in a
       browser we have not greeted before. */
    var lastUid = null;
    try { lastUid = localStorage.getItem(LS_UID); } catch (e) {}
    var freshLogin = isLoggedIn && lastUid !== userId;

    /* set a sensible resting posture even while docked, for a clean summon */
    setPosture(isLoggedIn ? 'standing' : 'seated');

    if (startDocked) {
      root.classList.add('mx-docked');
      if (isLoggedIn) { rememberUser(); } else { forgetUser(); }
      return;
    }

    if (isLoggedIn) {
      rememberUser();
      if (freshLogin) { playIntro(); } else { startCycle(); }
    } else {
      forgetUser();
      setPosture('seated');     /* guest: seated idle, no action cycle */
    }

    /* Phase 2: AI-driven scenes + proactive invitations (throttled) */
    setupSceneTriggers();
    setupInviteScheduler();
  }

  init();
}());
