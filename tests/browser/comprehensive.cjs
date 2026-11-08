// One-pass browser acceptance; no paid upstream requests or live user stores.
const {test, before, after} = require('node:test');
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {chromium} = require('playwright');
const path = require('node:path');
const fs = require('node:fs');
let server, browser, base;
const output = process.env.AI_COMPREHENSIVE_OUTPUT;
const config = {
  ok:true, access:true, research_access:true, comprehensive_access:true, deepseek_web_access:true,
  runtime:{enabled:true}, quota:{unlimited:true}, research_quota:{unlimited:true},
  ai_entitlements:{models:{'deepseek-v4-flash':['off','high'],'deepseek-v4-pro':['high']},
    defaults:{quick:{model:'deepseek-v4-flash',reasoning_effort:'off'},research:{model:'deepseek-v4-pro',reasoning_effort:'high'}}},
  scopes:[{id:'auto',label:'自动'},{id:'all',label:'全部著作'}], book_scope_tree:[],
};
before(async()=>{
  server=spawn(process.env.PYTHON,['-u',path.join(__dirname,'serve_ai_fixture.py')],{stdio:['ignore','pipe','pipe'],windowsHide:true});
  base=await new Promise((resolve,reject)=>{
    let text='',errors=''; const timer=setTimeout(()=>reject(new Error(errors||'fixture timeout')),20000);
    server.stderr.on('data',d=>errors+=d);
    server.stdout.on('data',d=>{text+=d;const m=text.match(/READY=(http:\/\/[^\s]+)/);if(m){clearTimeout(timer);resolve(m[1]);}});
    server.on('error',reject); server.on('exit',code=>{clearTimeout(timer);reject(new Error('fixture exit '+code+': '+errors));});
  });
  browser=await chromium.launch({headless:true,channel:process.env.BROWSER_CHANNEL||'chrome'});
});
after(async()=>{if(browser)await browser.close();if(server)server.kill();});

async function setup(t,width=1280,paid=true){
  const context=await browser.newContext({viewport:{width,height:1000},acceptDownloads:true});
  const page=await context.newPage(); const calls=[],errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/api/**',async route=>{
    const pathname=new URL(route.request().url()).pathname;
    let response={ok:true,enabled:false,eligible:false};
    if(pathname==='/api/ai/assistant-config')response={...config,deepseek_web_access:paid,
      ai_entitlements:{...config.ai_entitlements,models:{...config.ai_entitlements.models,
        'deepseek-v4-flash':paid?['off','high']:['off']}}};
    if(['/api/ai/comprehensive-chat','/api/ai/search-chat','/api/search/associative'].includes(pathname)){
      const payload=route.request().postDataJSON();calls.push({pathname,payload});
      response={ok:true,answer_markdown:'按需求完成的正文。',review_markdown:'研究综述正文。',citations:[],review_citations:[],warnings:[],
        actual_model:{provider:'deepseek',model:'deepseek-v4-flash',reasoning_effort:'off'},
        completion_status:payload.continue_request_id?'complete':'partial',
        continue_request_id:payload.continue_request_id?null:payload.request_id,
        web_status:'used',sources:[{id:'W1',kind:'web',purpose:'background',title:'研究背景',link:'https://example.org/context',site:'学术网站',verification:'page_read'}]};
    }
    await route.fulfill({json:response});
  });
  t.after(async()=>{await context.close();assert.deepEqual(errors,[]);});
  await page.goto(base+'/v2/ai');
  await page.waitForFunction(()=>!document.getElementById('aipSend').disabled);
  return {page,calls};
}

for(const width of [1280,390])test('comprehensive defaults, scope, sources, continuation and persistence '+width,async t=>{
  const {page,calls}=await setup(t,width);
  assert.equal(await page.locator('.aip-mode-button[data-depth="comprehensive"]').getAttribute('aria-pressed'),'true');
  assert.equal(await page.locator('#aipWebMode').inputValue(),'auto');
  assert.equal(await page.locator('#aipModel').inputValue(),'deepseek|deepseek-v4-flash|off');
  assert.deepEqual(await page.locator('#aipModel option:enabled').evaluateAll(options=>options.map(o=>o.value)),
    ['deepseek|deepseek-v4-flash|off','deepseek|deepseek-v4-flash|high']);
  assert.equal(await page.locator('#aipGroundingMode').inputValue(),'auto');
  await page.locator('#aipPrompt').fill('按需要写文章，不凑引文');
  await page.locator('#aipSend').click();
  await page.locator('[data-continue-index]').waitFor();
  assert.equal(calls[0].pathname,'/api/ai/comprehensive-chat');
  assert.equal(calls[0].payload.web_mode,'auto');
  assert.equal(calls[0].payload.grounding_mode,'auto');
  assert.equal(calls[0].payload.model,'deepseek-v4-flash');
  assert.match(await page.locator('.sources').innerText(),/联网背景参考/);
  await page.locator('[data-continue-index]').click();
  await page.waitForFunction(()=>!document.getElementById('aipSend').disabled);
  assert.equal(calls[1].payload.continue_request_id,calls[0].payload.request_id);
  assert.notEqual(calls[1].payload.request_id,calls[0].payload.request_id);
  assert.equal(calls[1].payload.model,'deepseek-v4-flash');
  await page.reload();
  await page.waitForFunction(()=>!document.getElementById('aipSend').disabled);
  assert.match(await page.locator('#aipMessages').innerText(),/联网背景参考/);
  assert.equal(await page.locator('.aip-mode-button[data-depth="comprehensive"]').getAttribute('aria-pressed'),'true');
  assert.equal(await page.locator('#aipModel').inputValue(),'deepseek|deepseek-v4-flash|off');
  const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1);
  assert.equal(overflow,false);
  if(output){fs.mkdirSync(output,{recursive:true});await page.screenshot({path:path.join(output,'comprehensive-'+width+'.png'),fullPage:true});}
});

test('existing modes default offline and free users cannot enable web',async t=>{
  const {page,calls}=await setup(t,1280,false);
  assert.deepEqual(await page.locator('#aipModel option:enabled').evaluateAll(options=>options.map(o=>o.value)),
    ['deepseek|deepseek-v4-flash|off']);
  assert.equal(await page.locator('#aipWebMode').inputValue(),'off');
  assert.equal(await page.locator('#aipWebMode').isDisabled(),true);
  await page.locator('.aip-mode-button[data-depth="quick"]').click();
  await page.locator('#aipPrompt').fill('简短问题');await page.locator('#aipSend').click();
  await page.waitForFunction(()=>!document.getElementById('aipSend').disabled);
  assert.equal(calls[0].pathname,'/api/ai/search-chat');assert.equal(calls[0].payload.web_mode,'off');
  await page.locator('.aip-mode-button[data-depth="research"]').click();
  assert.equal(await page.locator('#aipWebMode').inputValue(),'off');
});

test('switching from Pro to comprehensive resets to Flash and preserves entitled thinking',async t=>{
  const {page,calls}=await setup(t);
  await page.locator('.aip-mode-button[data-depth="research"]').click();
  assert.equal(await page.locator('#aipModel').inputValue(),'deepseek|deepseek-v4-pro|high');
  await page.locator('.aip-mode-button[data-depth="comprehensive"]').click();
  assert.equal(await page.locator('#aipModel').inputValue(),'deepseek|deepseek-v4-flash|off');
  await page.locator('#aipModel').selectOption('deepseek|deepseek-v4-flash|high');
  await page.locator('#aipPrompt').fill('深入思考');await page.locator('#aipSend').click();
  await page.locator('[data-continue-index]').waitFor();
  assert.equal(calls[0].payload.model,'deepseek-v4-flash');
  assert.equal(calls[0].payload.reasoning_effort,'high');
});

test('Word export retains background source URLs',async t=>{
  const {page}=await setup(t);
  await page.locator('#aipPrompt').fill('写作');await page.locator('#aipSend').click();
  await page.locator('[data-continue-index]').waitFor();
  const downloadPromise=page.waitForEvent('download');
  await page.locator('[data-export-word="footnote"]').first().click();
  const download=await downloadPromise;
  const bytes=fs.readFileSync(await download.path());
  // StoreZip uses uncompressed XML, allowing direct package-content assertions.
  assert.ok(bytes.includes(Buffer.from('https://example.org/context')));
  assert.ok(bytes.includes(Buffer.from('联网背景参考')));
  if(output)await download.saveAs(path.join(output,'comprehensive-export.docx'));
});

test('stopping SSE preserves the checkpoint and continuation action',async t=>{
  const {page}=await setup(t);
  await page.route('**/api/ai/comprehensive-chat',route=>route.continue({url:base+'/fixture-comprehensive-stream'}));
  await page.locator('#aipPrompt').fill('长篇文章');await page.locator('#aipSend').click();
  await page.getByText('已经核验并保存的第一节。',{exact:true}).waitFor();
  await page.locator('#aipStop').click();
  assert.match(await page.locator('#aipMessages').innerText(),/已经核验并保存的第一节/);
  assert.equal(await page.locator('[data-continue-index]').count(),1);
  await page.reload();await page.waitForFunction(()=>!document.getElementById('aipSend').disabled);
  assert.match(await page.locator('#aipMessages').innerText(),/已经核验并保存的第一节/);
});
