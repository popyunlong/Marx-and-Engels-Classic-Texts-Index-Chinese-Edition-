// New protected-member scenarios: real API rights, empty money wallet, mock generation.
const {test,before,after}=require('node:test');
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const path=require('node:path');
const fs=require('node:fs');
const {chromium}=require('playwright');
let server,browser,base;
before(async()=>{
  server=spawn(process.env.PYTHON,['-u',path.join(__dirname,'serve_legacy_promise.py')],{stdio:['ignore','pipe','pipe'],windowsHide:true});
  base=await new Promise((resolve,reject)=>{
    let out='',err='';const timer=setTimeout(()=>reject(new Error(err||out||'fixture timeout')),45000);
    server.stderr.on('data',d=>err+=d);
    server.stdout.on('data',d=>{out+=d;const match=out.match(/READY=(http:\/\/[^\s]+)/);if(match){clearTimeout(timer);resolve(match[1]);}});
    server.on('exit',code=>{clearTimeout(timer);reject(new Error('fixture '+code+': '+err));});
  });
  browser=await chromium.launch({channel:process.env.BROWSER_CHANNEL||'chrome',headless:true});
});
after(async()=>{if(browser)await browser.close();if(server)server.kill();});

test('protected member sees actual quota and gift with exhausted money, then resumes saved comprehensive reply',async()=>{
  const context=await browser.newContext({viewport:{width:1280,height:900}});
  try{
    const page=await context.newPage();const errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto(base+'/fixture-legacy-login');
    await page.locator('[data-legacy-promise]').waitFor();
    assert.match(await page.locator('[data-legacy-promise]').innerText(),/770000/);
    assert.match(await page.locator('[data-legacy-promise]').innerText(),/赠送/);
    const config=await page.evaluate(async()=>await(await fetch('/api/ai/assistant-config')).json());
    assert.equal(config.ai_entitlements.legacy_promise.eligible,true);
    assert.equal(config.ai_entitlements.wallets.length,0);
    assert.equal(config.deepseek_web_quota.gift_remaining,20);
    // Keep authoritative real entitlements; only advertise our mocked model service.
    await page.route('**/api/ai/assistant-config',route=>route.fulfill({json:{...config,runtime:{...config.runtime,enabled:true}}}));
    const requests=[];
    await page.route('**/api/ai/comprehensive-chat',async route=>{
      const body=route.request().postDataJSON();requests.push(body);
      await route.fulfill({json:{ok:true,answer_markdown:'旧会员已核验正文。',citations:[],sources:[],warnings:[],
        completion_status:body.continue_request_id?'complete':'partial',
        continue_request_id:body.continue_request_id?null:body.request_id,
        stop_reason:body.continue_request_id?'complete':'weekly_quota',
        recoverable:false,notice:'旧会员本周额度不足，下周一恢复。'}});
    });
    await page.goto(base+'/v2/ai');
    await page.waitForFunction(()=>!document.getElementById('aipSend').disabled);
    assert.match(await page.locator('#aipTokenQuota').innerText(),/旧会员每周 AI 额度/);
    const options=await page.locator('#aipModel option:enabled').evaluateAll(xs=>xs.map(x=>x.value));
    assert.deepEqual(options,['deepseek|deepseek-v4-flash|off']);
    await page.locator('#aipPrompt').fill('使用旧会员承诺额度回答');
    await page.locator('#aipSend').click();
    await page.locator('[data-continue-index]').waitFor();
    await page.locator('[data-continue-index]').click();
    await page.waitForFunction(()=>!document.getElementById('aipSend').disabled);
    assert.equal(requests.length,2);
    assert.equal(requests[1].continue_request_id,requests[0].request_id);
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
    if(process.env.AI_COMPREHENSIVE_OUTPUT){fs.mkdirSync(process.env.AI_COMPREHENSIVE_OUTPUT,{recursive:true});await page.screenshot({path:path.join(process.env.AI_COMPREHENSIVE_OUTPUT,'legacy-quota-mobile.png'),fullPage:true});}
    assert.deepEqual(errors,[]);
  }finally{await context.close();}
});
