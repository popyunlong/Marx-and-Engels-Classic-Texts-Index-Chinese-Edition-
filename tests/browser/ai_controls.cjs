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

async function setup(t, {width=768,height=800,legacy=false,access=true,researchAccess=access,entitlements,seed=true,guest=false}={}) {
  const context=await browser.newContext({viewport:{width,height}});
  const page=await context.newPage();
  const errors=[], requests=[];
  let pending=null, hold=false, fail=false;
  page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/api/**', async route=>{
    const url=new URL(route.request().url());
    let payload={ok:true,enabled:false,eligible:false};
    if(url.pathname==='/api/ai/assistant-config') payload={...config,access,research_access:researchAccess,ai_entitlements:entitlements,access_gate:'login'};
    if(['/api/ai/search-chat','/api/search/associative'].includes(url.pathname)) {
      requests.push(route.request().postDataJSON());
      if(hold) { pending=route; return; }
      payload={ok:true,answer_markdown:'测试回答',review_markdown:'测试研究综述',citations:[],review_citations:[]};
      if(fail) { payload={ok:false,error:'测试失败'}; fail=false; }
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
  if(access || researchAccess) await page.waitForFunction(()=>!document.getElementById('aipSend').disabled);
  else await page.locator('#aipLock').waitFor({state:'visible'});
  return {page,requests,fail:()=>{fail=true;},hold:()=>{hold=true;},release:async()=>{hold=false;if(pending){await pending.fulfill({json:{ok:true,answer_markdown:'已完成',review_markdown:'研究已完成'}}).catch(()=>{});pending=null;}}};
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
      assert.ok(Math.abs(await page.evaluate(()=>scrollY)-scroll)<2,`closing changed page position: width=${width}, fraction=${fraction}, before=${scroll}, after=${await page.evaluate(()=>scrollY)}`);
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
  await page.locator('.aip-depth-tab[data-depth="research"]').click();
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
for(const legacy of [false,true]) test(`composer mode preserves drafts, scope, preferences and routes (${legacy?'legacy':'v2'})`,async t=>{
  const {page,requests}=await setup(t,{legacy});
  const quick=page.locator('.aip-mode-button[data-depth="quick"]');
  const research=page.locator('.aip-mode-button[data-depth="research"]');
  await page.locator('#aipScopeChips [data-scope="all"]').click();
  await page.locator('#aipComposerGrounding').click();
  await page.locator('#aipPrompt').fill('保留这段草稿');
  const history=await page.locator('#aipMessages').innerText();
  await research.click();
  assert.equal(await page.locator('#aipPrompt').inputValue(),'保留这段草稿');
  assert.equal(await page.locator('#aipMessages').innerText(),history);
  assert.equal(requests.length,0);
  assert.equal(await research.getAttribute('aria-pressed'),'true');
  assert.equal(await page.locator('.aip-depth-tab[data-depth="research"]').getAttribute('aria-selected'),'true');
  assert.equal(await page.locator('#aipComposerGrounding').isDisabled(),true);
  await research.press('ArrowLeft');
  assert.equal(await quick.getAttribute('aria-pressed'),'true');
  assert.equal(await page.locator('#aipGrounding').isChecked(),false);
  await quick.press('End');
  await page.locator('#aipSend').click();
  await page.waitForFunction(()=>!window.__marxBusy());
  assert.equal(requests.at(-1).mode,'research');
  assert.equal(requests.at(-1).scope,'all');
  assert.equal(requests.at(-1).model,'deepseek-v4-pro');
  await page.reload();
  await page.waitForFunction(()=>!document.getElementById('aipSend').disabled);
  assert.equal(await research.getAttribute('aria-pressed'),'true');
  await page.locator('.aip-depth-tab[data-depth="quick"]').click();
  assert.equal(await quick.getAttribute('aria-pressed'),'true');
  await page.locator('#aipPrompt').fill('快速跟进');
  await page.locator('#aipSend').click();
  await page.waitForFunction(()=>!window.__marxBusy());
  assert.equal(requests.at(-1).question,'快速跟进');
  assert.equal(requests.at(-1).model,'deepseek-v4-flash');
  assert.equal(requests.at(-1).grounding,false);
  assert.ok(requests.at(-1).messages.some(m=>m.content.includes('保留这段草稿')));
});

test('reselecting a mode does not reset an entitled custom model',async t=>{
  const {page}=await setup(t,{entitlements:{
    models:{'deepseek-v4-flash':['off'],'deepseek-v4-pro':['on'],'mimo-v2.5':['off']},
    defaults:{quick:{provider:'deepseek',model:'deepseek-v4-flash',reasoning_effort:'off'}},
  }});
  await page.locator('#aipModel').selectOption('mimo|mimo-v2.5|off');
  await page.locator('.aip-mode-button[data-depth="quick"]').click();
  await page.locator('.aip-depth-tab[data-depth="quick"]').click();
  assert.equal(await page.locator('#aipModel').inputValue(),'mimo|mimo-v2.5|off');
});

for(const complete of [true,false]) test(`mode changes leave the in-flight request intact (${complete?'complete':'stop and retry'})`,async t=>{
  const fixture=await setup(t);
  const {page,requests}=fixture;
  fixture.hold();
  await page.locator('#aipPrompt').fill('当前快速问题');
  await page.locator('#aipSend').click();
  await page.waitForFunction(()=>window.__marxBusy());
  await page.locator('.aip-mode-button[data-depth="research"]').click();
  await page.locator('#aipPrompt').fill('下一问研究');
  assert.match(await page.locator('#aipModeHint').innerText(),/下次提问生效/);
  assert.equal(requests.length,1);
  assert.equal(requests[0].model,'deepseek-v4-flash');
  if(!complete) await page.locator('#aipStop').click();
  await fixture.release();
  await page.waitForFunction(()=>!window.__marxBusy());
  assert.equal(await page.locator('#aipModeHint').isVisible(),false);
  if(complete) assert.match(await page.locator('#aipMessages .msg.assistant').last().innerText(),/快速问答[\s\S]*已完成/);
  else fixture.fail();
  await page.locator('#aipSend').click();
  await page.waitForFunction(()=>!window.__marxBusy());
  assert.equal(requests.at(-1).mode,'research');
  if(!complete) {
    assert.match(await page.locator('#aipMessages').innerText(),/测试失败/);
    await page.locator('#aipPrompt').fill('重试研究');
    await page.locator('#aipSend').click();
    await page.waitForFunction(()=>!window.__marxBusy());
    assert.match(await page.locator('#aipMessages .msg.assistant').last().innerText(),/测试研究综述/);
  }
});

for(const researchAccess of [true,false]) test(`mode permissions agree at both entry points (research=${researchAccess})`,async t=>{
  const {page}=await setup(t,{access:!researchAccess,researchAccess});
  const denied=researchAccess?'quick':'research';
  for(const cls of ['.aip-mode-button','.aip-depth-tab']) assert.equal(await page.locator(`${cls}[data-depth="${denied}"]`).isDisabled(),true);
  const allowed=page.locator(`.aip-mode-button[data-depth="${researchAccess?'research':'quick'}"]`);
  await allowed.focus(); await allowed.press('ArrowRight');
  assert.equal(await allowed.getAttribute('aria-pressed'),'true');
});

for(const legacy of [false,true]) test(`composer layout adapts to its own width (${legacy?'legacy':'v2'})`,async t=>{
  const {page}=await setup(t,{legacy});
  for(const width of [320,375,390,640,768,960,1181,1440]) {
    await page.setViewportSize({width,height:900});
    await page.evaluate(()=>scrollTo(0,document.body.scrollHeight));
    await noOverflow(page);
    for(const selector of ['.aip-mode-button[data-depth="quick"]','.aip-mode-button[data-depth="research"]','#aipComposerGrounding','#aipScopeToggle','#aipClear','#aipSend']) await visibleAndClickable(page,selector);
    const r=await page.locator('#aipComposer').evaluate(el=>{
      const rect=s=>el.querySelector(s).getBoundingClientRect().toJSON();
      const css=getComputedStyle(el);
      return {width:el.clientWidth-parseFloat(css.paddingLeft)-parseFloat(css.paddingRight),mode:rect('.aip-mode-switch'),retrieval:rect('.aip-retrieval-actions'),submit:rect('.aip-submit-actions')};
    });
    if(r.width<640) assert.ok(r.mode.bottom<=r.retrieval.top && r.retrieval.bottom<=r.submit.top,JSON.stringify(r));
    else assert.ok(Math.abs((r.mode.top+r.mode.bottom)-(r.submit.top+r.submit.bottom))<2 && r.retrieval.top>=r.mode.bottom,JSON.stringify(r));
    if([320,390,1440].includes(width)) await snapshot(page,`mode-${legacy?'legacy':'v2'}-${width}`);
  }
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


test('refined cover failure remains visible and storage denial does not break controls', {skip:!!process.env.AI_CONTROLS_BASE_URL}, async () => {
  const context = await browser.newContext({reducedMotion:'reduce'});
  try {
    await context.addInitScript(() => {
      Object.defineProperty(window, 'localStorage', {get(){throw new Error('storage denied');}});
    });
    const page = await context.newPage();
    const errors=[]; page.on('pageerror',e=>errors.push(e.message));
    await page.goto(base+'/refined-fixture');
    await page.locator('.is-cover-unavailable').waitFor();
    assert.equal(await page.locator('.v2book-cover img').count(),1);
    assert.equal(await page.getByText('封面暂不可用').isVisible(),true);
    assert.equal(await page.locator('.v2book').getAttribute('href'),'/reader');
    assert.equal(await page.locator('.refined-motion').getAttribute('aria-pressed'),'true');
    await page.locator('.refined-motion').click();
    assert.equal(await page.locator('.refined-motion').getAttribute('aria-pressed'),'false');
    assert.deepEqual(errors,[]);
  } finally {await context.close();}
});

test('production navigation labels never overlap account controls at responsive boundaries', {skip:!!process.env.AI_CONTROLS_BASE_URL}, async () => {
  const context=await browser.newContext();
  try {
    const page=await context.newPage();
    for(const guest of [true,false]) {
      await page.goto(base+'/navigation-fixture?guest='+(guest?'1':'0'));
      for(const width of [390,820,821,900,1000,1024,1100,1140,1180,1200,1201,1280,1400,1401,1440,1600]) {
        await page.setViewportSize({width,height:900});
        const issues=await page.locator('header').evaluate(header=>{
          const rects=[...header.querySelectorAll('.v2brand,.v2acct a,.v2acct button')].map(el=>({el,r:el.getBoundingClientRect()})).filter(x=>x.r.width&&x.r.height);
          const problems=[];
          for(let i=0;i<rects.length;i++) {
            const {el,r}=rects[i];
            if(r.left<0||r.right>innerWidth+1||r.bottom>header.getBoundingClientRect().bottom+1) problems.push('outside: '+el.textContent.trim());
            const hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
            if(hit!==el&&!el.contains(hit)) problems.push('blocked: '+el.textContent.trim());
            for(let j=i+1;j<rects.length;j++) {
              const b=rects[j].r;
              if(Math.min(r.right,b.right)>Math.max(r.left,b.left)+1&&Math.min(r.bottom,b.bottom)>Math.max(r.top,b.top)+1)
                problems.push('overlap: '+el.textContent.trim()+' / '+rects[j].el.textContent.trim());
            }
          }
          return problems;
        });
        assert.deepEqual(issues,[],`guest=${guest}, width=${width}`);
        if(width>820){
          const nav=page.locator('.v2tabs-top');
          const geometry=await nav.evaluate(el=>{
            const r=el.getBoundingClientRect(),account=document.querySelector('.v2acct').getBoundingClientRect();
            return {clear:r.right<=account.left,rows:new Set([...el.querySelectorAll('a')].map(a=>Math.round(a.getBoundingClientRect().top))).size};
          });
          assert(geometry.clear && geometry.rows===1,JSON.stringify({guest,width,...geometry}));
          for(const link of await nav.locator('a').all()){
            await link.scrollIntoViewIfNeeded();
            assert(await link.evaluate(el=>{
              const r=el.getBoundingClientRect(),hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
              return el.contains(hit);
            }),`navigation link blocked: guest=${guest}, width=${width}`);
          }
        }
      }
    }
  } finally {await context.close();}
});
