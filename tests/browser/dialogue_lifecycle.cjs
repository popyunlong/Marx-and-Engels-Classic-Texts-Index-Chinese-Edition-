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
for(const mode of ['quick','research','comprehensive']) test('late cloud list preserves first answer: '+mode,async t=>{
 const context=await browser.newContext(),page=await context.newPage(),errors=[];
 const cloud=deferred(),reply=deferred(),cloudStarted=deferred(),modelStarted=deferred();
 t.after(async()=>{cloud.resolve();reply.resolve();await context.close();assert.deepEqual(errors,[]);});
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/api/**',async route=>{
  const pathname=new URL(route.request().url()).pathname;let data={ok:true,enabled:false,eligible:false};
  if(pathname==='/api/ai/assistant-config')data=config;
  if(pathname==='/api/ai/conversations/status')data={ok:true,available:true,enabled:false,eligible:false};
  if(pathname==='/api/ai/conversations'){cloudStarted.resolve();await cloud.promise;data={ok:true,conversations:[],deleted:[]};}
  if(['/api/ai/search-chat','/api/ai/comprehensive-chat','/api/search/associative'].includes(pathname)){modelStarted.resolve();await reply.promise;data={ok:true,answer_markdown:'Visible answer.',review_markdown:'Visible answer.',completion_status:'complete'};}
  await route.fulfill({json:data});
 });
 await page.goto(base+'/v2/ai');await cloudStarted.promise;
 await page.waitForFunction(()=>!document.getElementById('aipSend').disabled);
 await page.locator(`.aip-mode-button[data-depth="${mode}"]`).click();
 await page.locator('#aipPrompt').fill('First request');await page.locator('#aipSend').click();await modelStarted.promise;
 const received=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/ai/conversations');cloud.resolve();await received;
 await page.locator('.pending .msg-title').waitFor();assert.equal(await page.locator('.pending').count(),1);
 reply.resolve();await page.locator('.msg-body').filter({hasText:'Visible answer.'}).waitFor();
 await page.waitForFunction(()=>document.getElementById('aipStop').hidden);
 await page.reload();await page.locator('.msg-body').filter({hasText:'Visible answer.'}).waitFor();
});

test('done with CRLF finishes before connection closes and saves checkpoint',async t=>{
 const context=await browser.newContext(),page=await context.newPage();t.after(()=>context.close());
 await page.route('**/api/**',route=>{
  const p=new URL(route.request().url()).pathname;
  if(p==='/api/ai/comprehensive-chat')return route.continue({url:base+'/fixture-lifecycle-stream'});
  return route.fulfill({json:p==='/api/ai/assistant-config'?config:{ok:true,available:false}});
 });
 await page.goto(base+'/v2/ai');await page.waitForFunction(()=>!document.getElementById('aipSend').disabled);
 await page.locator('.aip-mode-button[data-depth="comprehensive"]').click();await page.locator('#aipPrompt').fill('Stream');await page.locator('#aipSend').click();
 await page.locator('.msg-body').filter({hasText:'Verified checkpoint.'}).waitFor();
 // The fixture holds its connection for 5 seconds AFTER done. This must finish beforehand.
 await page.waitForFunction(()=>document.getElementById('aipStop').hidden,{},{timeout:2500});
 await page.reload();await page.locator('.msg-body').filter({hasText:'Verified checkpoint.'}).waitFor();
});

test('late DB read cannot detach pending message; newer checkpoint wins merge',async()=>{
 const source=fs.readFileSync(path.join(__dirname,'../../static/ai-page/ai-page.js'),'utf8');
 const extract=(name,next)=>source.slice(source.indexOf('  function '+name+'('),source.indexOf('  function '+next+'('));
 const d=deferred(),pending={role:'assistant',content:'',pending:true};
 const c={sessionDb:{},sessionDbFailed:false,streaming:false,sessionEpoch:0,sessionsData:{current:'a'},messages:[pending],sessionRefreshPending:false,readDbSessions:()=>d.promise};
 vm.createContext(c);vm.runInContext(extract('refreshSessionsFromDb','scheduleSessionRefresh'),c);
 c.refreshSessionsFromDb();c.streaming=true;c.sessionEpoch++;d.resolve([]);await Promise.resolve();await Promise.resolve();
 assert.equal(c.messages[0],pending);assert.equal(c.sessionRefreshPending,true);
 c.payloadScore=v=>JSON.stringify(v||'').length;
 vm.runInContext(extract('mergeMessageVersions','mergeMessages'),c);
 const merged=c.mergeMessageVersions({_mid:'a',content:'full text',_revision:4},{_mid:'a',content:'short',_revision:1});
 assert.equal(merged.content,'full text');
});
