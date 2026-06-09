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

  var LS_DOCK   = 'mx-docked-v1';
  var LS_UID    = 'mx-last-uid-v1';

  var isLoggedIn   = root.dataset.loggedIn === 'true';
  var displayName  = (root.dataset.name || '').trim();
  var userId       = (root.dataset.uid || '').trim();
  var defaultDock  = root.dataset.defaultDocked === 'true';
  var aiAccess     = root.dataset.ai === 'true';
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

  /* ------------------------------------------------------- dock / summon -- */
  function dockMascot(withLine) {
    root.classList.add('mx-docked');
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
    /* Phase 2: if a dialogue handler is attached, defer to it instead. */
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
  if (chipHide) { chipHide.addEventListener('click', function (e) { e.stopPropagation(); hideBubble(); }); }

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
    openDialogue: function () {                 /* real conversation (Phase 2) */
      if (typeof api.dialogueHandler === 'function') { return api.dialogueHandler(api); }
      speakContextual();
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
  }

  init();
}());
