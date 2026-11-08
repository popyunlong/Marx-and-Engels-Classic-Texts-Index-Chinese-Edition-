const {test,before,after}=require('node:test');
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const {chromium}=require('playwright');
const fs=require('node:fs');
const path=require('node:path');
let server,browser,base;
const output=process.env.AI_COMPREHENSIVE_OUTPUT;
const source='交换的深度、广度和方式都是由生产的发展和结构决定的。';
const near=source.replace('方式','形式');
const config={ok:true,access:true,research_access:true,comprehensive_access:true,deepseek_web_access:true,
  runtime:{enabled:true},quota:{unlimited:true},research_quota:{unlimited:true},
  ai_entitlements:{models:{'deepseek-v4-flash':['off'],'deepseek-v4-pro':['high']},defaults:{quick:{model:'deepseek-v4-flash',reasoning_effort:'off'},research:{model:'deepseek-v4-pro',reasoning_effort:'high'}}},
  scopes:[{id:'auto',label:'自动'},{id:'all',label:'全部著作'}],book_scope_tree:[]};
before(async()=>{
  server=spawn(process.env.PYTHON,['-u',path.join(__dirname,'serve_ai_fixture.py')],{stdio:['ignore','pipe','pipe'],windowsHide:true});
  base=await new Promise((resolve,reject)=>{let txt='',err='';const timer=setTimeout(()=>reject(new Error(err||'fixture timeout')),20000);
    server.stderr.on('data',d=>err+=d);server.stdout.on('data',d=>{txt+=d;const m=txt.match(/READY=(http:\/\/[^\s]+)/);if(m){clearTimeout(timer);resolve(m[1]);}});server.on('error',reject);});
  browser=await chromium.launch({headless:true,channel:process.env.BROWSER_CHANNEL||(process.platform==='win32'?'chrome':undefined)});
});
after(async()=>{if(browser)await browser.close();if(server)server.kill();});
async function pageFor(t,width=1280,admin=true,stream=false){
  const context=await browser.newContext({viewport:{width,height:1000},acceptDownloads:true});
  const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
  t.after(async()=>{await context.close();assert.deepEqual(errors,[]);});
  await page.route('**/api/**',async route=>{
    const url=new URL(route.request().url());
    let result={ok:true,enabled:false,eligible:false};
    if(url.pathname==='/api/ai/assistant-config')result={...config,comprehensive_access:admin};
    if(['/api/ai/comprehensive-chat','/api/ai/search-chat','/api/search/associative'].includes(url.pathname)){
      if(stream)return route.continue({url:base+'/fixture-polish-stream'});
      const cards=[{grounding_index:1,review_index:1,source_file:'edition-a.pdf',citation:'《测试原文》第 1 页',context:source,
        evidence:[{kind:'approximate_quote',answer_text:near,quote:near,source_text:source,text_verified:false,location_status:'verified',differences:[{answer:'形',source:'方'}]}]}];
      result={ok:true,answer_markdown:'“'+near+'”[1]',review_markdown:'“'+near+'”[1]',citations:cards,review_citations:cards,warnings:[],completion_status:'complete'};
    }
    await route.fulfill({json:result});
  });
  await page.goto(base+'/v2/ai');await page.waitForFunction(()=>!document.getElementById('aipSend').disabled);return page;
}
test('new equal-width mode layout at three widths and two access levels',async t=>{
  for(const width of [1280,390,320])for(const admin of [true,false]){
    const page=await pageFor(t,width,admin);
    for(const selector of ['.aip-depth-tab:visible','.aip-mode-button:visible']){
      const boxes=await page.locator(selector).evaluateAll(els=>els.map(e=>{const b=e.getBoundingClientRect();return {y:b.y,width:b.width};}));
      assert.equal(boxes.length,admin?3:2);
      assert.ok(boxes.every(b=>Math.abs(b.y-boxes[0].y)<2&&Math.abs(b.width-boxes[0].width)<2));
    }
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
    if(output)await page.screenshot({path:path.join(output,`layout-${width}-${admin?'admin':'member'}.png`),fullPage:true});
  }
});
test('new variant badges font details persistence and Word notes in every mode',async t=>{
  for(const mode of ['quick','research','comprehensive']){
    const page=await pageFor(t);await page.locator(`.aip-mode-button[data-depth="${mode}"]`).click();
    await page.locator('#aipPrompt').fill('近似引文呈现验收');await page.locator('#aipSend').click();
    await page.locator('.aip-variant-label').first().waitFor();
    assert.ok((await page.locator('.aip-direct-quote').first().textContent()).includes(near));
    await page.locator('.aip-variant-label').first().click();
    assert.ok((await page.locator('.aip-variant-detail').first().textContent()).includes(source));
    const downloadPromise=page.waitForEvent('download');await page.locator('[data-export-word="footnote"]').first().click();
    const download=await downloadPromise, bytes=fs.readFileSync(await download.path());
    for(const text of ['近似引文','检索原文',source,'楷体'])assert.ok(bytes.includes(Buffer.from(text)),text);
    if(output)await download.saveAs(path.join(output,`variant-${mode}.docx`));
    await page.reload();await page.locator('.aip-variant-label').first().waitFor();
    assert.ok((await page.locator('.aip-direct-quote').first().textContent()).includes(near));
  }
});
test('legacy projection is immutable and labels approximate evidence without model access',async t=>{
  const page=await pageFor(t);
  const result=await page.evaluate(({near,source})=>{
    const m={content:'“'+near+'”[1]',citations:[{grounding_index:1,source_file:'a.pdf',evidence:[{kind:'paraphrase',context:source}]}]};
    const before=JSON.stringify(m), projected=AipQuoteVariants.project(m);
    return {unchanged:before===JSON.stringify(m),kinds:projected.citations[0].evidence.map(e=>e.kind),marked:AipQuoteVariants.mark(projected.content,projected.citations)};
  },{near,source});
  assert.ok(result.unchanged);assert.ok(result.kinds.includes('approximate_quote'));assert.ok(result.marked.includes('近似引文'));
});
test('heartbeat keeps existing paragraph DOM and progress visible until done',async t=>{
  const page=await pageFor(t,390,true,true);
  await page.locator('#aipPrompt').fill('分段状态展示验收');await page.locator('#aipSend').click();
  await page.locator('.aip-progress').waitFor();
  await page.evaluate(()=>window.firstAnswerNode=document.querySelector('.msg-body'));
  await page.waitForFunction(()=>document.querySelector('.aip-progress')?.textContent.includes('核验第二段'));
  assert.ok(await page.evaluate(()=>window.firstAnswerNode===document.querySelector('.msg-body')));
  await page.waitForFunction(()=>!document.querySelector('.aip-progress'));
  assert.ok((await page.locator('.msg-body').textContent()).includes('第二段'));
});
