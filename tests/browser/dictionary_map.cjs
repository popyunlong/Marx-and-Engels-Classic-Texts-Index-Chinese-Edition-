const {test,before,after}=require('node:test');
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const fs=require('node:fs/promises');
const path=require('node:path');
const {chromium}=require('playwright');
let server,browser,base,center;
before(async()=>{
  server=spawn(process.env.PYTHON||'python',['-B','-u',path.join(__dirname,'serve_dictionary_fixture.py')],{stdio:['ignore','pipe','pipe']});
  base=await new Promise((resolve,reject)=>{
    let out='',err='';const timer=setTimeout(()=>reject(new Error('fixture timeout '+err)),30000);
    server.stderr.on('data',d=>err+=d);
    server.stdout.on('data',d=>{out+=d;const m=out.match(/READY=(http:\/\/[^\s]+)/);if(m){clearTimeout(timer);resolve(m[1]);}});
    server.on('exit',code=>{clearTimeout(timer);reject(new Error('fixture exit '+code+': '+err));});
  });
  browser=await chromium.launch({headless:true,...(process.env.BROWSER_CHANNEL?{channel:process.env.BROWSER_CHANNEL}:{})});
  const ctx=await browser.newContext();
  center=(await (await ctx.request.get(base+'/api/dictionary/suggest?q='+encodeURIComponent('资本'))).json()).results[0].slug;
  await ctx.close();
});
after(async()=>{if(browser)await browser.close();if(server)server.kill();});
const ready=p=>p.waitForFunction(()=>!document.getElementById('dmWorkspace').hidden);
const centerURL=()=>base+'/dictionary/map?center='+encodeURIComponent(center);
async function open(t,width=1400){const ctx=await browser.newContext({viewport:{width,height:960},acceptDownloads:true});t.after(()=>ctx.close());return ctx.newPage();}

for(const width of [390,720,820,821,900,1024,1100,1140,1200,1280,1600]){
  test('dictionary navigation remains accessible at '+width,async t=>{
    const p=await open(t,width);await p.goto(base+'/dictionary');
    assert.equal(await p.locator('.v2nav').count(),1);
    assert.equal(await p.locator('[aria-current="page"]').count(),2);
    assert(await p.locator('.v2brand').getAttribute('href')==='/?restore=1');
    const nav=width<=820?p.locator('.v2tabbar'):p.locator('.v2tabs-top');
    const links=nav.locator('a');
    for(let i=0;i<await links.count();i++){
      const link=links.nth(i);await link.scrollIntoViewIfNeeded();
      const hit=await link.evaluate(el=>{const r=el.getBoundingClientRect(),found=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);return {ok:el.contains(found),label:el.textContent};});
      assert(hit.ok,JSON.stringify(hit));
    }
    assert(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  });
}

test('directory puts concept map first and opens the graph directly',async t=>{
  const p=await open(t);const requests=[];p.on('request',r=>requests.push(r.url()));
  await p.goto(base+'/dictionary/map');await ready(p);
  assert.equal(await p.locator('.dm-entry').count(),30);
  assert(await p.locator('#dmGraphPanel').isHidden());assert(await p.locator('#dmViewControls').isHidden());
  assert.equal(await p.locator('.dm-entry .dm-actions').first().locator(':scope > :first-child').textContent(),'查看概念地图');
  assert.equal(await p.locator('.dm-entry .dm-actions a').first().textContent(),'阅读词条内容');
  const first=await p.locator('.dm-entry h3').allTextContents();
  await p.locator('#dmNext').click();await ready(p);
  const second=await p.locator('.dm-entry h3').allTextContents();
  assert.equal(second.length,30);assert.equal(new Set([...first,...second]).size,60);
  await p.reload();await ready(p);assert.equal(new URL(p.url()).searchParams.get('offset'),'30');
  await p.locator('#dmPrev').click();await ready(p);assert.deepEqual(await p.locator('.dm-entry h3').allTextContents(),first);
  assert(!requests.some(u=>u.includes('cytoscape')));
  await p.locator('#dmQuery').fill('8条关系词条');await p.locator('#dmQueryForm button').click();await ready(p);
  assert.equal(await p.locator('.dm-entry').count(),1);
  await p.locator('.dm-entry button').click();await ready(p);
  await p.locator('#dmPng').waitFor({state:'visible'});
  assert(await p.locator('#dmGraphPanel').isVisible());
  assert((await p.locator('#dmGraphNote').textContent()).includes('完整展示 · 8'));
  assert.equal(await p.locator('.dm-pick input:checked').count(),8);
  assert.equal(new URL(p.url()).pathname,'/concept-map');
});

test('concept map is an independent column and dictionary retains its original index',async t=>{
  const p=await open(t);await p.goto(base+'/concept-map?center=fixture-small&group=all&view=list&offset=0');await ready(p);
  assert.equal(await p.title(),'概念地图 · 马克思主义理论研究辅助程序');
  const active=p.locator('.v2tabs-top [aria-current="page"]');
  assert.equal((await active.textContent()).trim(),'概念地图');
  assert.equal(await active.getAttribute('href'),'/concept-map');
  assert.equal(await p.locator('.v2tabs-top a[href="/dictionary"].active').count(),0);
  assert(await p.locator('#dmGraphPanel').isHidden());
  await p.locator('.v2tabs-top a[href="/dictionary"]').click();
  assert(await p.locator('#dictSearchInput').isVisible());
  assert(await p.locator('.letters').isVisible());assert(await p.locator('.term').count()>1000);
  assert.equal(await p.locator('#dmWorkspace').count(),0);assert.equal(await p.locator('.dict-nav a[href="/concept-map"]').count(),0);
  await p.locator('.v2tabs-top a[href="/concept-map"]').click();await ready(p);
  assert.equal(await p.locator('.dm-entry').count(),30);
  await p.goto(base+'/dictionary/map?center=fixture-small&group=incoming&view=graph&pick=fixture-8');await ready(p);
  await p.locator('#dmPng').waitFor({state:'visible'});
  assert.equal(new URL(p.url()).pathname,'/concept-map');
  assert.equal(new URL(p.url()).searchParams.get('group'),'incoming');
  assert.equal(new URL(p.url()).searchParams.get('pick'),'fixture-8');
});

test('graph cards expose pages and evidence through keyboard and PNG export',async t=>{
  const p=await open(t,390);const errors=[];p.on('pageerror',e=>errors.push(e.message));
  await p.goto(base+'/concept-map?center=fixture-small');await ready(p);await p.locator('#dmPng').waitFor({state:'visible'});
  const cards=await p.locator('#dmCanvas').evaluate(el=>el._cyreg.cy.nodes().map(n=>decodeURIComponent(n.data('card'))));
  assert(cards.every(svg=>svg.includes('页')&&svg.includes('<svg')));
  assert(cards.slice(1).every(svg=>svg.includes('条关系')));
  assert(cards.some(svg=>svg.includes('指向中心词')));assert(cards.some(svg=>svg.includes('从中心词出发')));
  await p.locator('#dmGraphIndexSummary').click();
  const first=p.locator('#dmGraphNodes button').first();await first.focus();await p.keyboard.press('Enter');
  assert(await p.locator('#dmGraphDetail blockquote').first().isVisible());
  assert((await p.locator('#dmGraphDetail a').last().getAttribute('href')).includes('#paragraph-1'));
  const [download]=await Promise.all([p.waitForEvent('download'),p.locator('#dmPng').click()]);
  const png=await fs.readFile(await download.path());assert.equal(png.subarray(1,4).toString(),'PNG');assert(png.length>10000);
  assert.deepEqual(errors,[]);
});

test('reading groups, full pagination, evidence, inference, CSV and history',async t=>{
  const p=await open(t);const errors=[],requests=[];p.on('pageerror',e=>errors.push(e.message));p.on('request',r=>requests.push(r.url()));
  await p.goto(centerURL()+'&limit=59&view=list&group=outgoing');await ready(p);
  assert.equal(await p.locator('.dm-relation').count(),20);
  assert.equal(await p.locator('#dmEdges .dm-inferred').count(),0);
  assert(!requests.some(u=>u.includes('cytoscape')));
  const slugs=[];
  while(true){
    slugs.push(...await p.locator('.dm-relation').evaluateAll(els=>els.map(e=>e.dataset.slug)));
    if(await p.locator('#dmNext').isDisabled())break;
    await p.locator('#dmNext').click();await ready(p);
  }
  assert.equal(slugs.length,72);assert.equal(new Set(slugs).size,72);
  await p.locator('#dmEdges summary').first().click();
  assert(await p.locator('#dmEdges details[open] blockquote').count()>0);
  assert((await p.locator('#dmEdges details[open] a').first().getAttribute('href')).endsWith('#paragraph-1'));
  const [download]=await Promise.all([p.waitForEvent('download'),p.locator('#dmCsv').click()]);
  const csv=await fs.readFile(await download.path(),'utf8');assert(csv.includes('导出范围'));assert(csv.includes('第 4 页'));
  await p.locator('#dmHome').click();await ready(p);await p.goBack();await ready(p);
  assert.equal(new URL(p.url()).searchParams.get('offset'),'60');
  await p.locator('#dmInference').check();await ready(p);
  await p.locator('[data-group="inference_related"]').click();await ready(p);
  assert.equal(await p.locator('.dm-inferred').count(),1);
  await p.locator('#dmEdges summary').click();
  assert.equal(await p.locator('#dmEdges details .dm-evidence').count(),2);
  await p.locator('#dmInference').uncheck();await ready(p);
  assert.equal(await p.locator('.dm-inferred').count(),0);
  assert.deepEqual(errors,[]);assert(!requests.some(u=>/api\/ai\/|xiaomimimo/.test(u)));
});

for(const width of [320,390,820,1440]){
  test('readable local diagram and complete titles at '+width,async t=>{
    const p=await open(t,width);await p.goto(centerURL());await ready(p);
    await p.locator('#dmPng').waitFor({state:'visible'});
    const expected=width<=720?6:10;
    assert.equal(await p.locator('#dmCanvas').evaluate(el=>el._cyreg.cy.nodes().length),expected+1);
    await p.locator('#dmReadingSummary').click();
    await p.locator('#dmGraphAll').click();await p.locator('#dmPng').waitFor({state:'visible'});
    assert.equal(await p.locator('.dm-pick input:checked').count(),20);
    const metrics=await p.locator('#dmCanvas').evaluate(el=>{
      const cy=el._cyreg.cy;
      return {zoom:cy.zoom(),nodes:cy.nodes().length,font:cy.nodes().map(n=>n.renderedStyle('font-size')),overlap:cy.nodes().some((a,i)=>cy.nodes().some((b,j)=>{
        if(j<=i)return false;const x=a.renderedBoundingBox(),y=b.renderedBoundingBox();return x.x1<y.x2&&x.x2>y.x1&&x.y1<y.y2&&x.y2>y.y1;
      }))};
    });
    assert.equal(metrics.zoom,1);assert.equal(metrics.nodes,21);assert(metrics.font.every(f=>parseFloat(f)>=14));assert.equal(metrics.overlap,false);
    if(width<=720)assert(await p.locator('#dmCanvas').evaluate(el=>el._cyreg.cy.edges().every(e=>{
      const leaf=e.source().data('center')==='yes'?e.target():e.source();
      const endpoint=e.source().data('center')==='yes'?e.targetEndpoint():e.sourceEndpoint();
      return Math.abs(endpoint.y-leaf.position('y'))<1;
    })), 'mobile arrows meet each leaf at its own side port');
    assert(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    const shared=p.url();await p.reload();await ready(p);await p.locator('#dmPng').waitFor({state:'visible'});
    assert.equal(await p.locator('.dm-pick input:checked').count(),20);assert.equal(p.url(),shared);
    await p.locator('#dmListView').click();assert(await p.locator('#dmGraphPanel').isHidden());assert(await p.locator('#dmPng').isHidden());
    await p.locator('#dmQuery').fill('讲话');await p.locator('#dmQueryForm button').click();await ready(p);
    const long=await p.locator('.dm-relation h3').textContent();assert(long.length>40);assert(!long.includes('…'));
  });
}

test('ordered paths retain arrows, same term and no path',async t=>{
  const p=await open(t);
  await p.goto(base+'/dictionary/map?center=fixture-3&to=fixture-2');await ready(p);
  assert.equal(await p.locator('.dm-relation').count(),2);
  const directions=await p.locator('.dm-direction').allTextContents();
  assert(directions[0].includes('资本 → 关联概念 3'));
  assert((await p.locator('.dm-path-step').first().textContent()).includes('关联概念 3 → 资本'));
  assert(await p.locator('#dmViewControls').isHidden());
  await p.goto(centerURL()+'&to='+encodeURIComponent(center));await ready(p);
  assert((await p.locator('#dmSummary').textContent()).includes('同一词条'));
  await p.goto(centerURL()+'&to=fixture-74');await ready(p);
  assert((await p.locator('#dmSummary').textContent()).includes('没有找到'));
});

test('mobile keyboard evidence, unavailable graph and stale version recovery',async t=>{
  const p=await open(t,390);await p.goto(centerURL()+'&view=list');await ready(p);
  const summary=p.locator('#dmEdges summary').first();await summary.focus();await p.keyboard.press('Enter');
  assert(await p.locator('#dmEdges details[open] blockquote').first().isVisible());
  await p.goto(base+'/dictionary/map?unavailable=1');assert(await p.getByText('概念地图暂不可用',{exact:true}).isVisible());
  await p.getByText('返回词条目录',{exact:true}).click();assert(await p.locator('#dictSearchInput').isVisible());
  await p.goto(base+'/dictionary/map?version=older-release');await p.locator('#dmLatest').waitFor({state:'visible'});
  await p.locator('#dmLatest').click();await ready(p);
  assert(await p.locator('#dmLatest').isHidden());assert.equal(new URL(p.url()).searchParams.get('version'),'fixture-v1');
});

test('failed diagram load leaves evidence readable; failed API hides stale content',async t=>{
  const p=await open(t);await p.route('**/cytoscape.min.js',r=>r.abort());
  await p.goto(centerURL());await ready(p);await p.locator('#dmGraphView').click();
  await p.waitForFunction(()=>document.getElementById('dmGraphNote').textContent.includes('暂不可用'));
  assert.equal(await p.locator('.dm-relation').count(),20);
  await p.route('**/api/dictionary/relations?**',r=>r.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'测试故障'})}));
  await p.locator('#dmNext').click();await p.locator('#dmRetry').waitFor({state:'visible'});
  assert(await p.locator('#dmWorkspace').isHidden());
  await p.unroute('**/api/dictionary/relations?**');await p.locator('#dmRetry').click();await ready(p);
});

for(const width of [390,1440]){
  test('small mixed-direction maps show every neighbour and place reading under title at '+width,async t=>{
    const p=await open(t,width);await p.goto(base+'/dictionary/map?center=fixture-small');await ready(p);
    await p.locator('#dmPng').waitFor({state:'visible'});
    assert((await p.locator('#dmGraphNote').textContent()).includes('完整展示 · 8 个相关词条 / 8 条关系'));
    assert.equal(await p.locator('#dmCanvas').evaluate(el=>el._cyreg.cy.nodes().length),9);
    assert.deepEqual(await p.locator('#dmCanvas').evaluate(el=>{
      const nodes=el._cyreg.cy.nodes().filter(n=>n.data('center')!=='yes');
      return {outgoing:nodes.filter(n=>n.data('role')==='outgoing').length,incoming:nodes.filter(n=>n.data('role')==='incoming').length};
    }),{outgoing:4,incoming:4});
    const title=await p.locator('#dmTitle').boundingBox(),read=await p.locator('#dmReadCenter').boundingBox();
    assert(read.y>=title.y+title.height);assert(Math.abs(read.x-title.x)<=1);
    assert.equal(await p.locator('#dmReadCenter').getAttribute('href'),'/dictionary/entry/fixture-small');
    assert(await p.locator('#dmReadingPanel').evaluate(el=>!el.open));
    await p.locator('#dmListView').click();assert(await p.locator('#dmGraphPanel').isHidden());
    assert.equal(await p.locator('.dm-relation').count(),8);
    await p.reload();await ready(p);assert(await p.locator('#dmGraphPanel').isHidden());
    assert.equal(new URL(p.url()).searchParams.get('view'),'list');
    await p.goto(base+'/dictionary/map?center=fixture-74');await ready(p);
    assert((await p.locator('#dmGraphNote').textContent()).includes('暂无关系'));
  });
}
