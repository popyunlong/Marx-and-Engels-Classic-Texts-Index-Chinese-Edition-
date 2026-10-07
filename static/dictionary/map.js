/* Read-only dictionary exploration. Lists are complete; diagrams are deliberately local. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  if (!$('dmDirectory')) return;
  const make = (tag, value, cls) => {
    const el = document.createElement(tag);
    if (value !== undefined) el.textContent = value;
    if (cls) el.className = cls;
    return el;
  };
  const button = (label, action) => {
    const el = make('button', label); el.type = 'button'; el.addEventListener('click', action); return el;
  };
  const link = (label, href) => { const el = make('a', label); el.href = href; return el; };
  const status = message => { $('dmStatus').textContent = message; };
  let state, data, requestNo = 0, controller, cy, graphPromise, drawing = 0;
  const suggestions = {dmTerm: new Map(), dmTarget: new Map()};
  const suggestSeq = {dmTerm: 0, dmTarget: 0};
  const node = slug => data.nodes.find(n => n.slug === slug) || data.focus?.nodes.find(n => n.slug === slug);
  const title = slug => data ? node(slug)?.title || slug : slug;
  const edge = id => data.edges.find(e => e.id === id) || data.focus?.edges.find(e => e.id === id);
  const edgesFor = item => item.edge_ids.map(edge).filter(Boolean);
  const graphItems = () => [...new Map([...(data.focus?.items || []), ...data.items].map(item => [item.slug, item])).values()];
  const pageSize = () => state.center ? 20 : 30;
  const isPath = () => !!(state.center && state.to);
  const isGraph = () => !!(state.center && !state.to && state.view === 'graph');
  const relationText = edge => `${title(edge.source)} → ${title(edge.target)} · ${edge.label}`;
  const GRAPH_CAP = 20;
  const defaultPicks = () => data.total <= GRAPH_CAP ? data.items.map(item => item.slug) :
    (data.offset === 0 && data.focus ? data.focus.recommended : data.items.map(item => item.slug)).slice(0, innerWidth <= 720 ? 6 : 10);

  function readURL() {
    const p = new URLSearchParams(location.search);
    const offset = Number(p.get('offset'));
    return {version: p.get('version') || '', center: p.get('center') || '', theme: p.get('theme') || '',
      q: (p.get('q') || '').slice(0, 100), group: p.get('group') || (p.get('center') ? 'all' : ''), kind: p.get('kind') || '',
      inference: p.get('inference') === '1', offset: Number.isFinite(offset) ? Math.min(10000, Math.max(0, Math.floor(offset))) : 0,
      view: p.get('view') === 'list' ? 'list' : 'graph', to: p.get('to') || '',
      picks: p.has('pick') ? p.getAll('pick').filter(Boolean).slice(0, GRAPH_CAP) : null};
  }
  function saveURL(replace = false) {
    const p = new URLSearchParams();
    for (const key of ['version', 'center', 'theme', 'q', 'group', 'kind', 'to']) if (state[key]) p.set(key, state[key]);
    if (state.inference) p.set('inference', '1');
    if (state.offset) p.set('offset', state.offset);
    if (state.center && !state.to) p.set('view', state.view);
    if (isGraph()) {
      p.set('view', 'graph');
      if (state.picks?.length) state.picks.forEach(slug => p.append('pick', slug));
      else p.set('pick', '');
    }
    const url = '/dictionary/map?' + p;
    if (location.pathname + location.search !== url) history[replace ? 'replaceState' : 'pushState'](null, '', url);
  }
  async function fetchData(path, params, signal) {
    const search = new URLSearchParams(params);
    search.delete('picks');
    for (const pick of params.picks || []) search.append('pick', pick);
    const response = await fetch(path + '?' + search, {
      credentials: 'same-origin', headers: {Accept: 'application/json'}, signal
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(response.status === 401 || response.status === 403 ? '请登录具有辞典访问权限的账号后重试。' :
        payload.error || '地图暂不可用，请返回词条目录继续阅读。');
      error.code = response.status; throw error;
    }
    return payload;
  }
  function change(patch, moveFocus = false) {
    state = {...state, ...patch};
    return load(true, moveFocus);
  }
  function explore(slug) {
    return change({center: slug, group: 'all', offset: 0, q: '', to: '', view: 'graph', picks: null}, true);
  }
  function destroyGraph() {
    drawing++;
    if (cy) { cy.destroy(); cy = null; }
    $('dmPng').hidden = true;
    $('dmGraphDetail').hidden = true;
  }
  async function load(push = false, moveFocus = false) {
    const seq = ++requestNo;
    const focusedGroup = document.activeElement?.dataset.group;
    controller?.abort(); controller = new AbortController();
    destroyGraph(); data = null;
    $('dmWorkspace').hidden = true;
    $('dmRetry').hidden = true; $('dmLatest').hidden = true;
    status('正在读取' + (state.center ? '关系与依据…' : '主题目录…'));
    $('dmFilters').hidden = !state.center; $('dmPath').hidden = !state.center;
    $('dmInference').checked = state.inference; $('dmKind').value = state.kind;
    const params = {version: state.version, inference: state.inference ? '1' : '0', kind: state.kind};
    let endpoint;
    if (isPath()) {
      endpoint = '/api/dictionary/path'; Object.assign(params, {from: state.center, to: state.to});
    } else if (state.center) {
      endpoint = '/api/dictionary/relations';
      Object.assign(params, {center: state.center, group: state.group, q: state.q, offset: state.offset, limit: 20, picks: state.picks || []});
    } else {
      endpoint = '/api/dictionary/graph';
      Object.assign(params, {theme: state.theme, q: state.q, offset: state.offset, limit: 30});
    }
    try {
      const payload = await fetchData(endpoint, params, controller.signal);
      if (seq !== requestNo) return;
      data = payload; state.version = data.version;
      if (!isPath()) state.offset = data.offset;
      if (state.center && !isPath()) {
        state.group = data.group;
        state.picks = state.picks === null ? defaultPicks() :
          state.picks.filter(slug => graphItems().some(i => i.slug === slug));
      }
      $('dmWorkspace').hidden = false;
      render(); saveURL(!push);
      if (!moveFocus && focusedGroup) {
        [...$('dmGroups').children].find(el => el.dataset.group === state.group)?.focus({preventScroll: true});
      }
      if (moveFocus) {
        $('dmTitle').tabIndex = -1; $('dmTitle').focus({preventScroll: true});
        $('dmTitle').scrollIntoView({block: 'start', behavior: 'auto'});
      }
    } catch (error) {
      if (seq !== requestNo || error.name === 'AbortError') return;
      status(error.message); $('dmLatest').hidden = error.code !== 409; $('dmRetry').hidden = error.code === 409;
      if (push) saveURL();
    }
  }
  function renderThemes() {
    $('dmThemes').replaceChildren();
    const themes = [{theme: '', count: data.coverage.entries}, ...data.themes];
    for (const group of themes) {
      const b = button('', () => change({theme: group.theme, center: '', to: '', q: '', group: '', offset: 0, view: 'list', picks: null}, true));
      b.append(make('span', group.theme || '全部主题'), make('span', group.count, 'dm-theme-count'));
      b.setAttribute('aria-pressed', String(state.theme === group.theme)); $('dmThemes').append(b);
    }
  }
  function render() {
    renderThemes();
    $('dmShareFallback').hidden = true;
    const centered = !!state.center, path = isPath();
    document.querySelector('.dm-main').classList.toggle('dm-is-centered', centered);
    $('dmThemeDisclosure').open = innerWidth > 720 || !centered;
    $('dmSearchDisclosure').open = innerWidth > 720 || !centered;
    $('dmHome').hidden = !centered; $('dmRelations').hidden = !path;
    $('dmReadCenter').hidden = !centered; $('dmGroups').hidden = !centered || path;
    $('dmViewControls').hidden = !centered || path; $('dmDirectory').hidden = centered;
    $('dmReadingPanel').hidden = !centered; $('dmReadingPanel').open = !isGraph();
    $('dmQueryForm').hidden = path;
    $('dmPagination').hidden = path; $('dmRelationNote').hidden = !centered;
    $('dmGraphPanel').hidden = !isGraph();
    $('dmCsv').hidden = !centered || !data.edges.length;
    $('dmCsv').textContent = path ? '导出当前路径关系' : '导出本页关系';
    $('dmQuery').value = state.q;
    $('dmQueryLabel').textContent = centered ? '筛选相关词' : '筛选词目';
    $('dmQuery').placeholder = centered ? '在相关词条中查找' : '在当前主题内查找';
    $('dmCoverage').textContent = `覆盖 ${data.coverage.entries.toLocaleString()} 个词条 · ${data.coverage.relations.toLocaleString()} 条关系 · 地图版本 ${data.version}`;
    if (centered) {
      const current = node(state.center);
      $('dmTerm').value = current.title; suggestions.dmTerm.set(current.title, current.slug);
      $('dmReadCenter').href = current.url;
      $('dmTitle').textContent = path ? `${current.title} — ${title(state.to)}` : current.title;
      $('dmContext').textContent = path ? '两词联系路径' : '词条联系 · ' + current.theme;
      if (path) {
        $('dmTarget').value = title(state.to); suggestions.dmTarget.set(title(state.to), state.to);
        renderPath();
      } else {
        $('dmTarget').value = '';
        const active = data.groups.find(g => g.id === state.group);
        $('dmSummary').textContent = `第 ${current.start_page}–${current.end_page} 页 · 当前筛选共 ${data.total_relations} 条关系`;
        $('dmGroups').replaceChildren();
        for (const group of data.groups.filter(g => g.count || g.id === state.group)) {
          const b = button(`${group.label} · ${group.count}`, () => change({group: group.id, offset: 0, picks: null}));
          b.dataset.group = group.id; b.setAttribute('aria-pressed', String(group.id === state.group));
          b.title = `${group.count} 个相关词条，${group.relations} 条关系`; $('dmGroups').append(b);
        }
        status(`${active.label} · ${data.total} 个相关词条 · 每页 20 条`);
        $('dmReadingSummary').textContent = `关系依据与更多词条 · ${active.label} / ${data.total} 个相关词条`;
        renderRelations();
      }
    } else {
      $('dmTerm').value = ''; $('dmTarget').value = '';
      $('dmContext').textContent = '主题目录'; $('dmTitle').textContent = state.theme || '全部主题';
      $('dmSummary').textContent = `共 ${data.total} 个词条 · 完整词目与原书页码`;
      $('dmDirectory').replaceChildren();
      for (const item of data.nodes) {
        const article = make('article', undefined, 'dm-entry');
        article.append(make('h3', item.title), make('p', `${item.theme} · 第 ${item.start_page}–${item.end_page} 页`, 'dm-muted'));
        const actions = make('div', undefined, 'dm-actions');
        const mapButton = button('查看概念地图', () => explore(item.slug)); mapButton.className = 'dm-primary-action';
        actions.append(mapButton, link('阅读词条内容', item.url)); article.append(actions);
        $('dmDirectory').append(article);
      }
      if (!data.nodes.length) $('dmDirectory').append(make('p', '没有匹配的词条。请调整关键词或选择其他主题。', 'dm-empty'));
      status(`${state.theme || '全部主题'} · ${data.total} 个词条 · 每页 30 条`);
    }
    if (!path) {
      $('dmPrev').disabled = !data.offset;
      $('dmNext').disabled = data.offset + data.limit >= data.total;
      $('dmPageLabel').textContent = data.total ? `第 ${Math.floor(data.offset / data.limit) + 1} / ${Math.ceil(data.total / data.limit)} 页 · ${data.offset + 1}–${Math.min(data.total, data.offset + data.limit)} / ${data.total}` : '暂无结果';
    }
    $('dmListView').setAttribute('aria-pressed', String(!isGraph()));
    $('dmGraphView').setAttribute('aria-pressed', String(isGraph()));
    if (isGraph()) drawGraph();
  }
  function appendEvidence(container, evidence, excerpt = false) {
    const chars = Array.from(evidence.quote);
    container.append(make('blockquote', excerpt && chars.length > 180 ? chars.slice(0, 180).join('') + '…' : evidence.quote));
    const source = make('p', evidence.citation, 'dm-evidence-source');
    source.append(link('查看来源段落', evidence.url)); container.append(source);
  }
  function appendAllEvidence(container, edges) {
    for (const edge of edges) {
      const section = make('section', undefined, 'dm-evidence');
      section.append(make('p', relationText(edge), 'dm-direction'), make('p', edge.explanation, 'dm-explanation'));
      for (const evidence of edge.evidence) appendEvidence(section, evidence);
      container.append(section);
    }
  }
  function relationArticle(slug, edges, step) {
    const item = node(slug), first = edges[0];
    const article = make('article', undefined, 'dm-relation' + (first.layer === 'inference' ? ' dm-inferred' : ''));
    article.dataset.slug = slug;
    if (step) article.append(make('p', step, 'dm-path-step'));
    const header = make('div', undefined, 'dm-relation-header'), heading = make('div');
    heading.append(make('span', first.layer === 'inference' ? 'AI 推断 · ' + first.label : '原文依据 · ' + first.label, 'dm-badge'), make('h3', item.title));
    header.append(heading);
    if (isGraph()) {
      const label = make('label', undefined, 'dm-pick'), input = make('input');
      input.type = 'checkbox'; input.checked = state.picks.includes(slug);
      input.addEventListener('change', () => {
        if (input.checked && state.picks.length >= GRAPH_CAP) {
          input.checked = false; status(`概念地图单次最多展示 ${GRAPH_CAP} 个相关词条，请先取消一个已选词条。`); return;
        }
        state.picks = input.checked ? [...state.picks, slug] : state.picks.filter(s => s !== slug);
        drawGraph(); saveURL(true);
      });
      label.append(input, make('span', '加入地图')); header.append(label);
    }
    article.append(header, make('p', relationText(first) + (edges.length > 1 ? ` · 共 ${edges.length} 条关系` : ''), 'dm-direction'));
    if (first.evidence[0]) appendEvidence(article, first.evidence[0], true);
    const actions = make('div', undefined, 'dm-actions');
    actions.append(button('查看它的概念地图', () => explore(slug)), link('阅读词条内容', item.url)); article.append(actions);
    const details = make('details');
    details.append(make('summary', `查看全部依据 · ${edges.length} 条关系 / ${edges.reduce((n, e) => n + e.evidence.length, 0)} 处原文`));
    appendAllEvidence(details, edges); article.append(details);
    return article;
  }
  function renderRelations() {
    $('dmEdges').replaceChildren();
    for (const item of data.items) $('dmEdges').append(relationArticle(item.slug, edgesFor(item)));
    if (!data.items.length) $('dmEdges').append(make('p', '当前分组与筛选下暂无关系。可清除关键词、调整关系类型或选择其他分组。', 'dm-empty'));
  }
  function renderPath() {
    $('dmEdges').replaceChildren();
    const message = !data.found ? '在当前筛选下，四步以内没有找到联系。' : !data.steps.length ? '起点与终点是同一词条，无需经过其他关系。' :
      `找到 ${data.steps.length} 步联系。步骤按探索顺序排列，原始关系方向另行标明。`;
    $('dmSummary').textContent = message; status(message);
    for (const [index, step] of data.steps.entries()) {
      const edge = data.edges.find(e => e.id === step.edge_id);
      $('dmEdges').append(relationArticle(step.to, [edge], `第 ${index + 1} 步 · 探索顺序：${title(step.from)} → ${title(step.to)}`));
    }
    if (!data.steps.length) $('dmEdges').append(make('p', message, 'dm-empty'));
  }
  function loadGraphLibrary() {
    if (typeof window.cytoscape === 'function') return Promise.resolve();
    if (!graphPromise) graphPromise = new Promise((resolve, reject) => {
      const script = document.createElement('script'); script.src = '/static/vendor/cytoscape/cytoscape.min.js';
      script.onload = resolve; script.onerror = () => { script.remove(); graphPromise = null; reject(new Error('图形组件暂不可用，请使用下方关系列表。')); };
      document.head.append(script);
    });
    return graphPromise;
  }
  async function drawGraph() {
    destroyGraph();
    const seq = drawing;
    const picked = graphItems().filter(item => state.picks.includes(item.slug));
    const selectedEdges = picked.flatMap(edgesFor);
    const complete = data.offset === 0 && picked.length === data.total;
    const scope = state.group === 'all' && !state.q && !state.kind ? '' : '当前筛选 · ';
    $('dmGraphNote').textContent = complete ? `${scope}完整展示 · ${picked.length} 个相关词条 / ${selectedEdges.length} 条关系` :
      `${scope}局部展示 · ${picked.length} / ${data.total} 个相关词条 · 可筛选或翻页查看其余联系`;
    $('dmGraphAll').hidden = !data.items.length || data.items.every(item => state.picks.includes(item.slug));
    $('dmGraphBalance').hidden = data.total <= GRAPH_CAP;
    $('dmGraphSelection').hidden = data.total <= GRAPH_CAP;
    $('dmCanvas').hidden = !picked.length;
    if (!picked.length) {
      $('dmGraphNote').textContent = data.total ? '当前未选择词条。请展开下方关系列表选择要加入地图的词条。' :
        '当前筛选下暂无关系。可以阅读词条内容，或调整筛选条件。';
      return;
    }
    try {
      await loadGraphLibrary();
      if (seq !== drawing || !isGraph()) return;
      const width = $('dmCanvas').clientWidth, narrow = width < 650;
      const nodeWidth = narrow ? Math.floor((width - 52) / 2) : Math.min(176, Math.floor((width - 260) / 2) - 28);
      const box = (slug, isCenter = false) => {
        const w = isCenter ? Math.min(220, width - 48) : nodeWidth;
        const font = isCenter ? 16 : 14;
        const columns = Math.max(3, Math.floor((w - 28) / font));
        const chars = Array.from(title(slug)), lines = [];
        for (let i = 0; i < chars.length; i += columns) lines.push(chars.slice(i, i + columns).join(''));
        const item = picked.find(item => item.slug === slug);
        const relations = item ? edgesFor(item) : [];
        const outgoing = relations.some(edge => edge.source === state.center), incoming = relations.some(edge => edge.target === state.center);
        const role = outgoing && incoming ? 'both' : outgoing ? 'outgoing' : 'incoming';
        const label = (isCenter ? '中心词条\n' : '') + lines.join('\n');
        return {data: {id: slug, label, role, center: isCenter ? 'yes' : 'no', w, h: Math.max(76, (lines.length + (isCenter ? 1 : 0)) * 24 + 24)}};
      };
      const main = box(state.center, true), elements = [main];
      const neighbours = picked.map(item => box(item.slug));
      const rows = [];
      for (let i = 0; i < neighbours.length; i += 2) rows.push(neighbours.slice(i, i + 2));
      const rowsHeight = rows.reduce((sum, row) => sum + Math.max(...row.map(el => el.data.h)) + 28, 0);
      const height = narrow ? main.data.h + rowsHeight + 100 : Math.max(360, rowsHeight + 48, main.data.h + 100);
      let y = narrow ? main.data.h + 76 : (height - rowsHeight) / 2;
      for (const row of rows) {
        const rowHeight = Math.max(...row.map(el => el.data.h));
        row.forEach((el, index) => {
          el.position = {x: index === 0 ? nodeWidth / 2 + 24 : width - nodeWidth / 2 - 24, y: y + rowHeight / 2};
          elements.push(el);
        });
        y += rowHeight + 28;
      }
      main.position = {x: width / 2, y: narrow ? main.data.h / 2 + 24 : height / 2};
      $('dmCanvas').style.height = height + 'px';
      // Parallel evidence stays in the list; one line represents each direction and layer.
      const lines = new Map();
      for (const item of picked) for (const edge of edgesFor(item)) {
        const key = JSON.stringify([edge.source, edge.target, edge.layer]);
        if (!lines.has(key)) lines.set(key, {data: {id: 'line-' + lines.size, source: edge.source, target: edge.target, layer: edge.layer, edgeIds: [], label: edge.label}});
        lines.get(key).data.edgeIds.push(edge.id);
      }
      elements.push(...lines.values());
      cy = window.cytoscape({container: $('dmCanvas'), elements, userZoomingEnabled: false, userPanningEnabled: false,
        autoungrabify: true, minZoom: 1, maxZoom: 1, zoom: 1, pan: {x: 0, y: 0},
        layout: {name: 'preset', fit: false, animate: false},
        style: [
          {selector: 'node', style: {shape: 'round-rectangle', width: 'data(w)', height: 'data(h)', label: 'data(label)',
            'background-color': '#f8f4ed', 'border-width': 1.2, 'border-color': '#cfb99e', color: '#463b31', 'font-size': 14,
            'font-family': 'Microsoft YaHei, sans-serif', 'text-wrap': 'wrap', 'text-valign': 'center', 'text-halign': 'center', 'line-height': 1.6}},
          {selector: 'node[role="outgoing"]', style: {'background-color': '#f8eeef', 'border-color': '#c9a1a6', color: '#713e45'}},
          {selector: 'node[role="incoming"]', style: {'background-color': '#eff3f4', 'border-color': '#a5b8be', color: '#435d67'}},
          {selector: 'node[role="both"]', style: {'background-color': '#f8f4ed', 'border-color': '#cfb99e', color: '#463b31'}},
          {selector: 'node[center="yes"]', style: {'background-color': '#7b303b', 'border-width': 3, 'border-color': '#e1c5c8', color: '#fffaf6', 'font-size': 16, 'font-weight': 'bold'}},
          {selector: 'edge', style: {width: 1.5, opacity: .58, 'line-color': '#ae9581', 'target-arrow-color': '#ae9581', 'target-arrow-shape': 'triangle', 'arrow-scale': .9, 'curve-style': 'bezier'}},
          {selector: 'edge[layer="inference"]', style: {'line-style': 'dashed', 'line-color': '#557d83', 'target-arrow-color': '#557d83'}},
          {selector: 'node:selected', style: {'border-width': 2.5, 'border-color': '#7b303b'}},
          {selector: 'edge:selected, edge.dm-active', style: {width: 2.5, opacity: 1, 'line-color': '#7b303b', 'target-arrow-color': '#7b303b', label: 'data(label)', 'font-size': 12,
            color: '#7b303b', 'text-background-opacity': 1, 'text-background-color': '#fffdf9', 'text-background-padding': 4}}
        ]});
      cy.on('tap', 'edge', event => {
        cy.edges().removeClass('dm-active');
        const ids = event.target.data('edgeIds');
        $('dmGraphDetail').replaceChildren(make('h3', '关系依据'));
        appendAllEvidence($('dmGraphDetail'), ids.map(edge).filter(Boolean));
        $('dmGraphDetail').hidden = false;
      });
      cy.on('tap', 'node', event => {
        const slug = event.target.id();
        cy.edges().removeClass('dm-active'); event.target.connectedEdges().addClass('dm-active');
        const item = graphItems().find(item => item.slug === slug);
        if (item) {
          $('dmGraphDetail').replaceChildren(make('h3', title(slug)));
          const actions = make('div', undefined, 'dm-actions');
          actions.append(button('查看它的概念地图', () => explore(slug)), link('阅读词条内容', node(slug).url));
          actions.append(button('移出当前图谱', () => {
            state.picks = state.picks.filter(pick => pick !== slug); renderRelations(); drawGraph(); saveURL(true);
          }));
          $('dmGraphDetail').append(actions);
          appendAllEvidence($('dmGraphDetail'), edgesFor(item)); $('dmGraphDetail').hidden = false;
        } else { $('dmReadCenter').focus({preventScroll: true}); }
      });
      $('dmPng').hidden = false;
    } catch (error) { if (seq === drawing) { $('dmCanvas').hidden = true; $('dmGraphNote').textContent = error.message; } }
  }
  async function suggest(id, list) {
    const seq = ++suggestSeq[id], q = $(id).value.trim();
    if (!q) { suggestions[id].clear(); $(list).replaceChildren(); return; }
    const payload = await fetchData('/api/dictionary/suggest', {q});
    if (seq !== suggestSeq[id]) return;
    suggestions[id].clear(); $(list).replaceChildren();
    for (const n of payload.results) {
      const label = `${n.title} · 第${n.start_page}页`;
      suggestions[id].set(label, n.slug);
      // Bare titles are accepted only when unambiguous.
      if (!suggestions[id].has(n.title)) suggestions[id].set(n.title, n.slug);
      else suggestions[id].set(n.title, null);
      const option = make('option'); option.value = label; $(list).append(option);
    }
  }
  for (const [id, list] of [['dmTerm', 'dmTerms'], ['dmTarget', 'dmTargets']]) {
    let timer;
    $(id).addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => {
      if (!suggestions[id].get($(id).value.trim())) suggest(id, list).catch(e => status(e.message));
    }, 180); });
  }
  async function resolveTerm(id, list) {
    let slug = suggestions[id].get($(id).value.trim());
    if (!slug) { await suggest(id, list); slug = suggestions[id].get($(id).value.trim()); }
    if (!slug) status('请从搜索提示中选择带页码的词条，以区分同名词目。');
    return slug;
  }
  $('dmSearch').addEventListener('submit', async event => {
    event.preventDefault();
    try { const slug = await resolveTerm('dmTerm', 'dmTerms'); if (slug) explore(slug); } catch (error) { status(error.message); }
  });
  $('dmPath').addEventListener('submit', async event => {
    event.preventDefault();
    try { const slug = await resolveTerm('dmTarget', 'dmTargets'); if (slug) change({to: slug, offset: 0, q: '', view: 'list', picks: null}); } catch (error) { status(error.message); }
  });
  $('dmQueryForm').addEventListener('submit', event => { event.preventDefault(); change({q: $('dmQuery').value.trim(), offset: 0, picks: null}); });
  for (const id of ['dmInference', 'dmKind']) $(id).addEventListener('change', () => change({
    inference: $('dmInference').checked, kind: $('dmKind').value, group: 'all', offset: 0, picks: null
  }));
  $('dmHome').onclick = () => change({center: '', to: '', group: '', q: '', offset: 0, view: 'list', picks: null});
  $('dmRelations').onclick = () => change({to: '', offset: 0, picks: null});
  $('dmPrev').onclick = () => change({offset: Math.max(0, state.offset - pageSize()), picks: null}, true);
  $('dmNext').onclick = () => change({offset: state.offset + pageSize(), picks: null}, true);
  for (const [id, view] of [['dmListView', 'list'], ['dmGraphView', 'graph']]) $(id).onclick = () => {
    destroyGraph(); state.view = view; render(); saveURL();
  };
  $('dmGraphAll').onclick = () => {
    state.picks = data.items.slice(0, GRAPH_CAP).map(item => item.slug);
    renderRelations(); drawGraph(); saveURL(true);
  };
  $('dmGraphBalance').onclick = () => change({offset: 0, picks: null});
  $('dmLatest').onclick = () => { state.version = ''; load(true); };
  $('dmRetry').onclick = () => load();
  $('dmShare').onclick = async () => {
    try { await navigator.clipboard.writeText(location.href); status('页面链接已复制；访问者仍需具备辞典权限。'); }
    catch { const input = make('input', undefined, 'dm-share-url'); input.readOnly = true; input.value = location.href; input.setAttribute('aria-label', '当前页面链接');
      $('dmShareFallback').replaceChildren(make('p', '复制以下页面链接：'), input); $('dmShareFallback').hidden = false; input.select(); }
  };
  function download(blob, name) {
    const url = URL.createObjectURL(blob), a = link('', url); a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  $('dmPng').onclick = () => { if (cy) download(cy.png({output: 'blob', bg: '#fffdf9', full: true, scale: 2}), '辞典概念地图.png'); };
  $('dmCsv').onclick = () => {
    const cell = value => '"' + String(value ?? '').replace(/^[=+@-]/, "'$&").replace(/"/g, '""') + '"';
    const scope = isPath() ? '当前路径' : `${data.groups.find(g => g.id === state.group).label} · 第 ${Math.floor(data.offset / data.limit) + 1} 页`;
    const rows = [['导出范围', '来源词条', '目标词条', '关系', '依据层', '说明', '依据原句', '出处', '来源链接', '地图版本']];
    for (const e of data.edges) rows.push([scope, title(e.source), title(e.target), e.label, e.layer === 'evidence' ? '原文依据' : 'AI 推断', e.explanation,
      e.evidence.map(x => x.quote).join('\n'), e.evidence.map(x => x.citation).join('\n'), e.evidence.map(x => new URL(x.url, location.origin).href).join('\n'), data.version]);
    download(new Blob(['\ufeff' + rows.map(row => row.map(cell).join(',')).join('\r\n')], {type: 'text/csv;charset=utf-8'}), isPath() ? '辞典当前路径与出处.csv' : '辞典本页关系与出处.csv');
  };
  let resizeTimer;
  window.addEventListener('resize', () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { if (data && isGraph()) drawGraph(); }, 150); });
  window.addEventListener('popstate', () => { state = readURL(); load(); });
  state = readURL(); load();
})();
