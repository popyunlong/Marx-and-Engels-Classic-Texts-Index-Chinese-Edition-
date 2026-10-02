// Real browser regression checks. All API traffic is intercepted; no AI quota or live stores are used.
// Run: node --test tests/browser/ai_controls.cjs (requires playwright and Python with Flask).
// AI_CONTROLS_BASE_URL optionally points at a release candidate; API requests remain intercepted.
const {test, before, after} = require('node:test');
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const {chromium} = require('playwright');
let server, browser, base;
const config = {
  ok: true, access: true, research_access: true,
  runtime: {enabled: true}, quota: {unlimited: true}, research_quota: {unlimited: true},
  scopes: [{id:'auto',label:'自动'}, {id:'all',label:'全部著作'}, {id:'marx',label:'马克思·恩格斯'}, {id:'lenin',label:'列宁'}],
  book_scope_tree: [{id:'marx',label:'马克思·恩格斯',books:[
    {key:'文集',label:'马克思恩格斯文集',volumes:[1,2,3,4,5]},
    {key:'全集',label:'马克思恩格斯全集',volumes:[1,2,3]},
  ]}],
};
before(async () => {
  base=process.env.AI_CONTROLS_BASE_URL;
  if(!base) {
    server = spawn(process.env.PYTHON || 'python', ['-u', path.join(__dirname,'serve_ai_fixture.py')], {stdio:['ignore','pipe','pipe']});
    base = await new Promise((resolve,reject) => {
    let output='', errors='';
    const timer=setTimeout(()=>reject(new Error('fixture startup timed out: '+errors)),20000);
    server.stderr.on('data', d=>{ errors += d; });
    server.stdout.on('data', d=>{ output+=d; const match=output.match(/READY=(http:\/\/[^\s]+)/); if(match){clearTimeout(timer);resolve(match[1]);} });
    server.on('error', reject);
    server.on('exit', code=>{clearTimeout(timer);reject(new Error('fixture exited '+code+': '+errors));});
    });
  }
  browser = await chromium.launch({headless:true, ...(process.env.BROWSER_CHANNEL ? {channel:process.env.BROWSER_CHANNEL} : {})});
});
after(async () => { if(browser) await browser.close(); if(server) server.kill(); });

async function setup(t, {width=768,height=800,legacy=false,access=true,seed=true,guest=false}={}) {
  const context=await browser.newContext({viewport:{width,height}});
  const page=await context.newPage();
  const errors=[], requests=[];
  let pending=null, hold=false;
  page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/api/**', async route=>{
    const url=new URL(route.request().url());
    let payload={ok:true,enabled:false,eligible:false};
    if(url.pathname==='/api/ai/assistant-config') payload={...config,access,research_access:access,access_gate:'login'};
    if(['/api/ai/search-chat','/api/search/associative'].includes(url.pathname)) {
      requests.push(route.request().postDataJSON());
      if(hold) { pending=route; return; }
      payload={ok:true,answer_markdown:'测试回答',review_markdown:'测试研究综述',citations:[],review_citations:[]};
    }
    await route.fulfill({json:payload});
  });
  if(seed) await context.addInitScript(()=>{
    if(localStorage.getItem('fixture-seeded')) return;
    const now=Date.now();
    const sessions=Array.from({length:30},(_,i)=>({id:'fixture-'+i,title:'历史会话 '+i,updatedAt:now-i*60000,
      messages:[{role:'user',content:'问题 '+i},{role:'assistant',content:('这是用于验证长对话滚动和操作入口的测试正文。\n\n').repeat(i===0?90:3)}]}));
    localStorage.setItem('marx-ai-sessions-v2',JSON.stringify({'90001':{current:'fixture-0',sessions},'_guest':{current:'fixture-0',sessions}}));
    localStorage.setItem('fixture-seeded','1');
  });
  t.after(async()=>{ if(pending) await pending.abort().catch(()=>{}); await context.close(); assert.deepEqual(errors,[]); });
  await page.goto(base+(legacy?'/ai':'/v2/ai')+(guest?'?guest=1':''));
  if(access) await page.waitForFunction(()=>!document.getElementById('aipSend').disabled);
  else await page.locator('#aipLock').waitFor({state:'visible'});
  return {page,requests,hold:()=>{hold=true;},release:async()=>{hold=false;if(pending){await pending.fulfill({json:{ok:true,answer_markdown:'已完成'}}).catch(()=>{});pending=null;}}};
}
async function visibleAndClickable(page, selector) {
  const result=await page.locator(selector).evaluate(el=>{
    const r=el.getBoundingClientRect(), hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
    return {visible:r.width>0&&r.height>0&&r.top>=0&&r.bottom<=innerHeight&&r.left>=0&&r.right<=innerWidth,hit:el===hit||el.contains(hit)};
  });
  if (!result.visible || !result.hit) {
    console.error(selector, await page.locator(selector).evaluate(el=>{
      const r=el.getBoundingClientRect();
      return {rect:r.toJSON(),scroll:scrollY,viewport:[innerWidth,innerHeight],hit:document.elementFromPoint(r.left+r.width/2,r.top+r.height/2)?.outerHTML.slice(0,250)};
    }));
  }
  assert.deepEqual(result,{visible:true,hit:true},selector);
}
async function noOverflow(page) {
  if(await page.evaluate(()=>document.documentElement.scrollWidth>document.documentElement.clientWidth+1)) {
    console.error(await page.evaluate(()=>Array.from(document.querySelectorAll('body *')).map(el=>({tag:el.tagName,id:el.id,cls:el.className,right:el.getBoundingClientRect().right,width:el.getBoundingClientRect().width})).filter(r=>r.right>innerWidth+1&&r.width>0).slice(0,20)));
  }
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=document.documentElement.clientWidth+1),'horizontal overflow');
}
async function snapshot(page,name) {
  if(!process.env.AI_CONTROLS_SCREENSHOTS) return;
  fs.mkdirSync(process.env.AI_CONTROLS_SCREENSHOTS,{recursive:true});
  await page.screenshot({path:path.join(process.env.AI_CONTROLS_SCREENSHOTS,name+'.png')});
}
for(const [width,height] of [[390,844],[640,460],[768,700],[960,900],[1180,800]]) {
  test(`drawer remains reachable and unobscured at ${width}x${height}`, async t=>{
    const {page}=await setup(t,{width,height});
    for(const fraction of [0,.5,1]) {
      await page.evaluate(f=>scrollTo(0,(document.documentElement.scrollHeight-innerHeight)*f),fraction);
      await visibleAndClickable(page,'#aipSessionsToggle');
      const scroll=await page.evaluate(()=>scrollY);
      await page.locator('#aipSessionsToggle').click();
      await page.locator('#aipSessions').evaluate(el=>el.getAnimations().forEach(a=>a.finish()));
      await visibleAndClickable(page,'#aipNewChat');
      await visibleAndClickable(page,'#aipSessionsClose');
      await visibleAndClickable(page,'.aip-session:first-child .aip-session-title');
      if(fraction===1) await snapshot(page,`sessions-${width}`);
      await page.locator('#aipSessionsList').evaluate(el=>{el.scrollTop=el.scrollHeight;});
      await visibleAndClickable(page,'#aipNewChat');
      await page.keyboard.press('Escape');
      await page.locator('#aipSessions').evaluate(el=>el.getAnimations().forEach(a=>a.finish()));
      assert.equal(await page.locator('#aipSessionsToggle').getAttribute('aria-expanded'),'false');
      assert.ok(Math.abs(await page.evaluate(()=>scrollY)-scroll)<2,'closing changed page position');
      assert.equal(await page.evaluate(()=>document.activeElement.id),'aipSessionsToggle');
    }
    await noOverflow(page);
    await snapshot(page,`composer-${width}`);
    await page.locator('#aipSessionsToggle').click();
    await page.locator('.aip-session[data-sid="fixture-1"] .aip-session-title').click();
    assert.match(await page.locator('#aipMessages').innerText(),/问题 1/);
    await page.locator('#aipSessionsToggle').click();
    await page.locator('#aipNewChat').click();
    assert.equal(await page.locator('#aipMessages .msg').count(),0);
  });
}
test('wide sidebar and breakpoint transitions preserve scroll and restore interaction',async t=>{
  const {page}=await setup(t,{width:1440,height:900});
  assert.equal(await page.locator('#aipSessionsToggle').isVisible(),false);
  await page.evaluate(()=>scrollTo(0,document.body.scrollHeight/2));
  await visibleAndClickable(page,'#aipNewChat');
  await page.setViewportSize({width:1180,height:800});
  await page.locator('#aipSessionsToggle').click();
  await page.setViewportSize({width:1181,height:800});
  await page.waitForFunction(()=>!document.documentElement.classList.contains('aip-overlay-open'));
  assert.equal(await page.locator('.aip-stage').evaluate(el=>el.inert),false);
  await visibleAndClickable(page,'#aipNewChat');
  await noOverflow(page);
});
for(const width of [390,960,1181]) test(`guest navigation and full production introduction fit at ${width}px`,async t=>{
  const {page}=await setup(t,{width,height:844,guest:true});
  await noOverflow(page);
  if(width<=1180) await visibleAndClickable(page,'#aipSessionsToggle');
  else await visibleAndClickable(page,'#aipNewChat');
  await visibleAndClickable(page,'.v2acct');
});
for(const legacy of [false,true]) test(`retrieval controls synchronize and send exact scope (${legacy?'legacy':'v2'})`,async t=>{
  const {page,requests}=await setup(t,{legacy});
  await page.locator('#aipComposerGrounding').click();
  assert.equal(await page.locator('#aipGrounding').isChecked(),false);
  assert.equal(await page.locator('#aipScopeToggle').isDisabled(),true);
  await page.locator('#aipGrounding').check();
  assert.equal(await page.locator('#aipComposerGrounding').getAttribute('aria-pressed'),'true');
  await page.locator('#aipScopeToggle').click();
  await visibleAndClickable(page,'#aipScopeClose');
  await page.locator('#aipComposerScopeChips [data-scope="marx"]').click();
  assert.equal(await page.locator('#aipScopeChips [data-scope="marx"]').getAttribute('aria-pressed'),'true');
  await page.locator('#aipComposerBookScopeMount [data-key="文集"] .bscope-vol-toggle').click();
  await page.locator('#aipComposerBookScopeMount [data-key="文集"] .bscope-vol-cb[value="2"]').check();
  await page.waitForFunction(()=>{
    const r=document.getElementById('aipScopePanel').getBoundingClientRect();
    return r.bottom<=innerHeight && r.top>=0;
  });
  assert.equal(await page.locator('#aipScopeChips [aria-pressed="true"]').count(),0);
  assert.equal(await page.locator('#aipComposerScopeChips [aria-pressed="true"]').count(),0);
  await visibleAndClickable(page,'#aipScopePanel');
  await snapshot(page,legacy?'scope-legacy':'scope-v2');
  assert.equal(await page.locator('#aipBookScopeMount [data-key="文集"] .bscope-vol-cb[value="2"]').isChecked(),true);
  await page.keyboard.press('Escape');
  assert.equal(await page.evaluate(()=>document.activeElement.id),'aipScopeToggle');
  await page.locator('#aipPrompt').fill('检查范围');
  await page.locator('#aipSend').click();
  await page.waitForFunction(()=>!document.getElementById('aipSend').disabled);
  assert.deepEqual(requests.at(-1).scope,['vol:文集:2']);
  assert.equal(requests.at(-1).grounding,true);
  await page.locator('#aipScopeChips [data-scope="lenin"]').click();
  assert.equal(await page.locator('#aipComposerBookScopeMount .bscope-book-cb:checked').count(),0);
  await page.locator('#aipBookScopeMount .bscope-trigger').click();
  await page.locator('#aipBookScopeMount [data-key="全集"] .bscope-book-cb').check();
  assert.equal(await page.locator('#aipComposerBookScopeMount [data-key="全集"] .bscope-book-cb').isChecked(),true);
  await page.locator('#aipScopeChips [data-scope="all"]').click();
  await page.locator('#aipComposerGrounding').click();
  await page.reload();
  await page.waitForFunction(()=>!document.getElementById('aipSend').disabled);
  assert.equal(await page.locator('#aipComposerGrounding').getAttribute('aria-pressed'),'false');
  assert.match(await page.locator('#aipScopeSummary').innerText(),/全部/);
  await page.locator('[data-depth="research"]').click();
  assert.equal(await page.locator('#aipComposerGrounding').isDisabled(),true);
  assert.equal(await page.locator('#aipComposerGrounding').getAttribute('aria-pressed'),'true');
  assert.equal(await page.locator('#aipScopeToggle').isEnabled(),true);
  await page.locator('#aipPrompt').fill('研究范围');
  await page.locator('#aipSend').click();
  await page.waitForFunction(()=>!document.getElementById('aipSend').disabled);
  assert.equal(requests.at(-1).mode,'research');
  assert.equal(requests.at(-1).scope,'all');
  await page.locator('#aipClear').click();
  assert.equal(await page.locator('#aipMessages .msg').count(),0);
});
test('short viewport, focus trap, backdrop, and zoom-sized layouts keep the panel visible',async t=>{
  const {page}=await setup(t,{width:640,height:460});
  for(const zoom of [1,1.25,1.5]) {
    // Browser zoom reduces the available CSS viewport; use equivalent dimensions in headless runs.
    await page.setViewportSize({width:Math.floor(640/zoom),height:Math.floor(460/zoom)});
    await page.locator('#aipScopeToggle').click();
    await visibleAndClickable(page,'#aipScopeClose');
    await visibleAndClickable(page,'#aipScopePanel');
    await page.keyboard.press('Shift+Tab');
    assert.equal(await page.evaluate(()=>document.getElementById('aipScopePanel').contains(document.activeElement)),true);
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(()=>document.activeElement.id),'aipScopeClose');
    await page.locator('#aipScopeScrim').click({position:{x:1,y:1}});
    assert.equal(await page.locator('#aipScopePanel').isVisible(),false);
    await noOverflow(page);
  }
});
test('changing retrieval settings during generation only affects the next request; stop still works',async t=>{
  const fixture=await setup(t);
  const {page,requests}=fixture;
  fixture.hold();
  await page.locator('#aipPrompt').fill('正在生成');
  await page.locator('#aipSend').click();
  await page.waitForFunction(()=>window.__marxBusy());
  await page.locator('#aipComposerGrounding').click();
  assert.equal(requests[0].grounding,true);
  await page.locator('#aipStop').click();
  await page.waitForFunction(()=>!window.__marxBusy());
  await fixture.release();
  await page.locator('#aipPrompt').fill('下一问');
  await page.locator('#aipSend').click();
  await page.waitForFunction(()=>!document.getElementById('aipSend').disabled);
  assert.equal(requests.at(-1).grounding,false);
  assert.doesNotMatch(await page.locator('#aipMessages').innerText(),/请求失败/);
});
test('locked accounts do not expose enabled composer controls',async t=>{
  const {page}=await setup(t,{access:false});
  assert.equal(await page.locator('#aipComposer').isVisible(),false);
  assert.equal(await page.locator('#aipControls').isVisible(),false);
  assert.equal(await page.locator('#aipScopePanel').isVisible(),false);
});
test('shared book selector keeps its default popup behavior alongside an inline selector',async t=>{
  const {page}=await setup(t);
  await page.evaluate(tree=>{
    const container=document.createElement('div'); container.id='otherBookScope';
    document.getElementById('aipControls').appendChild(container);
    window.BookScope.mount(container,tree,{dark:true,initialTokens:['vol:文集:3']});
  },config.book_scope_tree);
  assert.equal(await page.locator('#otherBookScope .bscope-panel').isVisible(),false);
  await page.locator('#otherBookScope .bscope-trigger').click();
  assert.equal(await page.locator('#otherBookScope .bscope-panel').isVisible(),true);
  assert.equal(await page.locator('#otherBookScope .bscope-vol-cb[value="3"]').first().isChecked(),true);
  await page.locator('#otherBookScope .bscope-clear').click();
  assert.equal(await page.locator('#otherBookScope input:checked').count(),0);
  await page.locator('#aipScopeToggle').click();
  assert.equal(await page.locator('#otherBookScope .bscope-panel').isVisible(),false);
  assert.equal(await page.locator('#aipComposerBookScopeMount .bscope-panel').isVisible(),true);
});
