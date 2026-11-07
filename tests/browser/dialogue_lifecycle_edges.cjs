const {test,before,after}=require('node:test');
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const {chromium}=require('playwright');
const path=require('node:path'),fs=require('node:fs'),vm=require('node:vm');
let server,browser,base;
const config={ok:true,access:true,research_access:true,comprehensive_access:true,runtime:{enabled:true},quota:{unlimited:true},research_quota:{unlimited:true},ai_entitlements:{models:{'deepseek-v4-flash':['off'],'deepseek-v4-pro':['high']},defaults:{quick:{model:'deepseek-v4-flash',reasoning_effort:'off'},research:{model:'deepseek-v4-pro',reasoning_effort:'high'}}},scopes:[{id:'auto',label:'auto'}],book_scope_tree:[]};
before(async()=>{
 server=spawn(process.env.PYTHON,['-u',path.join(__dirname,'serve_ai_fixture.py')],{stdio:['ignore','pipe','pipe'],windowsHide:true});
 base=await new Promise((resolve,reject)=>{let out='',err='';const t=setTimeout(()=>reject(Error(err||'fixture timeout')),20000);server.stderr.on('data',d=>err+=d);server.stdout.on('data',d=>{out+=d;let m=out.match(/READY=(http:\/\/[^\s]+)/);if(m){clearTimeout(t);resolve(m[1]);}});});
 browser=await chromium.launch({headless:true,channel:process.platform==='win32'?'chrome':undefined});
});
after(async()=>{if(browser)await browser.close();if(server)server.kill();});
function deferred(){let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};}

test('silent connection warns and stops without sending another paid request',async t=>{
 const context=await browser.newContext(),page=await context.newPage(),gate=deferred();let calls=0;
 t.after(async()=>{gate.resolve();await context.close();});
 await page.clock.install();
 await page.route('**/api/**',async route=>{
  const p=new URL(route.request().url()).pathname;
  if(p==='/api/ai/comprehensive-chat'){calls++;await gate.promise;}
  try {await route.fulfill({json:p==='/api/ai/assistant-config'?config:{ok:true,available:false,answer_markdown:'late'}});} catch(_) {}
 });
 await page.goto(base+'/v2/ai');await page.waitForFunction(()=>!document.getElementById('aipSend').disabled);
 await page.locator('.aip-mode-button[data-depth="comprehensive"]').click();await page.locator('#aipPrompt').fill('Silent request');await page.locator('#aipSend').click();
 await page.clock.fastForward(31000);
 assert.match(await page.locator('.pending .msg-title').textContent(),/连接等待/);
 await page.clock.fastForward(60000);
 await page.waitForFunction(()=>document.getElementById('aipStop').hidden);
 assert.match(await page.locator('.msg-body').textContent(),/长时间未返回数据/);
 assert.equal(calls,1);
});
test('stop preserves checkpoint and a late old request cannot clear the next request',async t=>{
 const context=await browser.newContext(),page=await context.newPage(),next=deferred();let calls=0;
 t.after(async()=>{next.resolve();await context.close();});
 await page.route('**/api/**',async route=>{
  const p=new URL(route.request().url()).pathname;
  if(p==='/api/ai/comprehensive-chat'){
   calls++;
   if(calls===1)return route.continue({url:base+'/fixture-lifecycle-partial'});
   await next.promise;return route.fulfill({json:{ok:true,answer_markdown:'Second response.',completion_status:'complete'}});
  }
  await route.fulfill({json:p==='/api/ai/assistant-config'?config:{ok:true,available:false}});
 });
 await page.goto(base+'/v2/ai');await page.waitForFunction(()=>!document.getElementById('aipSend').disabled);
 await page.locator('.aip-mode-button[data-depth="comprehensive"]').click();await page.locator('#aipPrompt').fill('First');await page.locator('#aipSend').click();
 await page.locator('.msg-body').filter({hasText:'Saved partial.'}).waitFor();
 await page.locator('#aipStop').click();
 await page.locator('#aipPrompt').fill('Second');await page.locator('#aipSend').click();
 await page.locator('.pending .msg-title').waitFor();
 assert.equal(await page.locator('#aipStop').isVisible(),true);
 next.resolve();await page.locator('.msg-body').filter({hasText:'Second response.'}).waitFor();
 await page.reload();
 await page.locator('.msg-body').filter({hasText:'Saved partial.'}).waitFor();
 await page.locator('.msg-body').filter({hasText:'Second response.'}).waitFor();
 assert.equal(calls,2);
});
