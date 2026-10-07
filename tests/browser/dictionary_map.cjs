const {test,before,after}=require('node:test');
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const path=require('node:path');
const {chromium}=require('playwright');
let server,browser,base;
before(async()=>{
  server=spawn(process.env.PYTHON||'python',['-u',path.join(__dirname,'serve_dictionary_fixture.py')],{stdio:['ignore','pipe','pipe']});
  base=await new Promise((resolve,reject)=>{
    let out='',err='';const timer=setTimeout(()=>reject(new Error('fixture timeout '+err)),30000);
    server.stderr.on('data',d=>err+=d);
    server.stdout.on('data',d=>{out+=d;const m=out.match(/READY=(http:\/\/[^\s]+)/);if(m){clearTimeout(timer);resolve(m[1]);}});
    server.on('exit',code=>{clearTimeout(timer);reject(new Error('fixture exit '+code+': '+err));});
  });
  browser=await chromium.launch({headless:true,...(process.env.BROWSER_CHANNEL?{channel:process.env.BROWSER_CHANNEL}:{})});
});
after(async()=>{if(browser)await browser.close();if(server)server.kill();});

for(const width of [390,720,820,821,900,1024,1100,1140,1200,1280,1600]){
  test('dictionary navigation remains accessible at '+width,async t=>{
    const ctx=await browser.newContext({viewport:{width,height:900}});t.after(()=>ctx.close());const p=await ctx.newPage();
    await p.goto(base+'/dictionary');
    assert.equal(await p.locator('.v2nav').count(),1);
    assert.equal(await p.locator('[aria-current="page"]').count(),2);
    assert(await p.locator('.v2brand').getAttribute('href')==='/?restore=1');
    const nav=width<=820?p.locator('.v2tabbar'):p.locator('.v2tabs-top');
    const links=nav.locator('a');
    for(let i=0;i<await links.count();i++){
      const link=links.nth(i);await link.scrollIntoViewIfNeeded();
      const hit=await link.evaluate(el=>{const r=el.getBoundingClientRect(),found=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);return {ok:el.contains(found),label:el.textContent,hit:found?.outerHTML.slice(0,250)};});
      assert(hit.ok,JSON.stringify(hit));
    }
    assert(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  });
}

test('graph lazy loading, inference, evidence, export, limits and history',async t=>{
  const ctx=await browser.newContext({viewport:{width:1400,height:1000},acceptDownloads:true});t.after(()=>ctx.close());const p=await ctx.newPage();
  const errors=[],requests=[];p.on('pageerror',e=>errors.push(e.message));p.on('request',r=>requests.push(r.url()));
  await p.goto(base+'/dictionary');
  assert(!requests.some(u=>u.includes('cytoscape.min.js')));
  const suggestion=await (await p.request.get(base+'/api/dictionary/suggest?q='+encodeURIComponent('资本'))).json();
  const center=suggestion.results[0].slug;
  await p.goto(base+'/dictionary/map?center='+encodeURIComponent(center));
  await p.waitForFunction(()=>document.getElementById('dmStatus').textContent.includes('当前显示'));
  assert.equal(await p.locator('#dmNodes button').count(),13);
  assert.equal(await p.locator('#dmEdges .dm-inferred').count(),0);
  await p.locator('#dmInference').check();
  await p.waitForSelector('#dmEdges .dm-inferred');
  await p.locator('#dmEdges .dm-inferred').click();
  assert(await p.locator('#dmDetail blockquote').count()>0);
  assert((await p.locator('#dmDetail a').getAttribute('href')).endsWith('#paragraph-1'));
  const [download]=await Promise.all([p.waitForEvent('download'),p.locator('#dmCsv').click()]);
  assert(download.suggestedFilename().endsWith('.csv'));
  for(let i=0;i<4;i++){await p.locator('#dmExpand').click();await p.waitForTimeout(150);}
  assert.equal(await p.locator('#dmNodes button').count(),60);
  assert(await p.locator('#dmExpand').isHidden());
  await p.locator('#dmHome').click();await p.waitForFunction(()=>document.getElementById('dmStatus').textContent.includes('全部主题'));
  await p.goBack();await p.waitForFunction(()=>document.getElementById('dmStatus').textContent.includes('当前显示'));
  assert.equal(await p.locator('#dmNodes button').count(),60);
  assert.deepEqual(errors,[]);
  assert(!requests.some(u=>/api\/ai\/|xiaomimimo/.test(u)));
});

test('mobile equivalent list and unavailable graph',async t=>{
  const ctx=await browser.newContext({viewport:{width:390,height:844},reducedMotion:'reduce'});t.after(()=>ctx.close());const p=await ctx.newPage();
  const suggestion=await (await p.request.get(base+'/api/dictionary/suggest?q='+encodeURIComponent('资本'))).json();
  await p.goto(base+'/dictionary/map?center='+encodeURIComponent(suggestion.results[0].slug));await p.waitForSelector('#dmEdges button');
  await p.locator('#dmEdges button').first().click();assert(await p.locator('#dmDetail blockquote').isVisible());
  assert(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  await p.goto(base+'/dictionary/map?unavailable=1');assert(await p.getByText('概念地图暂不可用',{exact:true}).isVisible());
  await p.getByText('返回词条目录',{exact:true}).click();assert(await p.locator('#dictSearchInput').isVisible());
});

test('old shared version offers a working latest-map recovery',async t=>{
  const ctx=await browser.newContext();t.after(()=>ctx.close());const p=await ctx.newPage();
  await p.goto(base+'/dictionary/map?version=older-release');
  await p.locator('#dmLatest').waitFor({state:'visible'});
  await p.locator('#dmLatest').click();
  await p.waitForFunction(()=>document.getElementById('dmStatus').textContent.includes('全部主题'));
  assert(await p.locator('#dmLatest').isHidden());
  assert.equal(new URL(p.url()).searchParams.has('version'),false);
});
