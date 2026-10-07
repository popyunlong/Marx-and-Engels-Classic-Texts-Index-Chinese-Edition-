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

test('theme directory paginates without loading graph resources',async t=>{
  const p=await open(t);const requests=[];p.on('request',r=>requests.push(r.url()));
  await p.goto(base+'/dictionary/map');await ready(p);
  assert.equal(await p.locator('.dm-entry').count(),30);
  assert(await p.locator('#dmGraphPanel').isHidden());assert(await p.locator('#dmViewControls').isHidden());
  const first=await p.locator('.dm-entry h3').allTextContents();
  await p.locator('#dmNext').click();await ready(p);
  const second=await p.locator('.dm-entry h3').allTextContents();
  assert.equal(second.length,30);assert.equal(new Set([...first,...second]).size,60);
  await p.reload();await ready(p);assert.equal(new URL(p.url()).searchParams.get('offset'),'30');
  await p.locator('#dmPrev').click();await ready(p);assert.deepEqual(await p.locator('.dm-entry h3').allTextContents(),first);
  await p.locator('#dmQuery').fill('关联概念 74');await p.locator('#dmQueryForm button').click();await ready(p);
  assert.equal(await p.locator('.dm-entry').count(),1);
  await p.locator('.dm-entry button').click();await ready(p);
  assert(await p.locator('#dmEdges').textContent().then(s=>s.includes('暂无关系')));
  assert(!requests.some(u=>u.includes('cytoscape')));
});

test('reading groups, full pagination, evidence, inference, CSV and history',async t=>{
  const p=await open(t);const errors=[],requests=[];p.on('pageerror',e=>errors.push(e.message));p.on('request',r=>requests.push(r.url()));
  await p.goto(centerURL()+'&limit=59');await ready(p); // Old links now open a readable list.
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
    await p.locator('#dmGraphView').click();await p.locator('#dmPng').waitFor({state:'visible'});
    const expected=width<=720?4:6;
    assert.equal(await p.locator('.dm-pick input:checked').count(),expected);
    for(let i=expected;i<8;i++)await p.locator('.dm-pick input:not(:checked)').first().check();
    assert.equal(await p.locator('.dm-pick input:checked').count(),8);
    await p.locator('.dm-pick input:not(:checked)').first().click();
    assert.equal(await p.locator('.dm-pick input:checked').count(),8);
    const metrics=await p.locator('#dmCanvas').evaluate(el=>{
      const cy=el._cyreg.cy;
      return {zoom:cy.zoom(),nodes:cy.nodes().length,font:cy.nodes().map(n=>n.renderedStyle('font-size')),overlap:cy.nodes().some((a,i)=>cy.nodes().some((b,j)=>{
        if(j<=i)return false;const x=a.renderedBoundingBox(),y=b.renderedBoundingBox();return x.x1<y.x2&&x.x2>y.x1&&x.y1<y.y2&&x.y2>y.y1;
      }))};
    });
    assert.equal(metrics.zoom,1);assert.equal(metrics.nodes,9);assert(metrics.font.every(f=>parseFloat(f)>=14));assert.equal(metrics.overlap,false);
    assert(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    const shared=p.url();await p.reload();await ready(p);await p.locator('#dmPng').waitFor({state:'visible'});
    assert.equal(await p.locator('.dm-pick input:checked').count(),8);assert.equal(p.url(),shared);
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
  const p=await open(t,390);await p.goto(centerURL());await ready(p);
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
