import {test} from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';
import {NetworkGate,nextSlot,claimRun,hostBusy,permitted,sleep,writeJSON,readJSON} from '../src/lib/control.js';
import {planAlerts,acknowledge,statusOf} from '../src/lib/alerts.js';
import {renderHtml} from '../src/lib/report.js';

test('schedule, deduplication, host pressure, durable state',()=>{
 const now=Date.parse('2026-11-07T19:00:00Z'),n=nextSlot(now,1);
 assert.equal(n.mode,'daily');assert.equal(n.at,Date.parse('2026-11-07T19:19:00Z'));
 assert.equal(nextSlot(n.at,0).mode,'light');
 const s={};assert.equal(claimRun(s,{now,mode:'daily'}),'');assert.equal(claimRun(s,{now:now+1,mode:'light'}),'frequency_limit');assert.equal(claimRun(s,{now:now+1800000,mode:'daily'}),'daily_already_attempted');
 assert.equal(claimRun(s,{now:now+1800000,mode:'light',slot:7}),'');assert.equal(claimRun(s,{now:now+3600000,mode:'light',slot:7}),'duplicate_slot');
 assert.equal(hostBusy(0,'MemAvailable: 4194304 kB'),'');assert.ok(hostBusy(4,'MemAvailable: 4194304 kB'));assert.ok(hostBusy(0,'MemAvailable: 100 kB'));
 const file=path.join(process.env.MONITOR_TEST_ARTIFACTS,'state.json');writeJSON(file,s);assert.deepEqual(readJSON(file),s);
});
test('alerts distinguish failure, coverage, recovery, unsuccessful delivery and legacy state',()=>{
 const failed={key:'ai',title:'AI',status:'failed',category:'script',message:'hidden selector'};
 const a=planAlerts([failed],{},10000);assert.equal(a.pending.length,0);
 const b=planAlerts([failed],a.state,20000);assert.equal(b.pending.length,1);assert.equal(b.state.items.ai.lastAlert,undefined);
 const c=planAlerts([failed],b.state,30000);assert.equal(c.pending.length,1);
 const sent=acknowledge(c.state,c.pending,30000);assert.equal(planAlerts([failed],sent,40000).pending.length,0);
 const skip=planAlerts([{...failed,status:'skipped'}],sent,50000);assert.ok(skip.state.items.ai);assert.equal(skip.pending.length,0);
 const skip2=planAlerts([{...failed,status:'skipped'}],skip.state,60000);assert.ok(skip2.pending.some(p=>p.key==='coverage'));assert.ok(!skip2.pending.some(p=>p.type==='recovered'&&p.key==='ai'));
 const recovered=planAlerts([{...failed,status:'passed'}],sent,70000);assert.equal(recovered.pending[0].type,'recovered');assert.ok(recovered.state.items.ai);assert.equal(acknowledge(recovered.state,recovered.pending,70000).items.ai,undefined);
 assert.equal(planAlerts([{...failed,immediate:true}],{},10000).pending.length,1);
 const migrated=planAlerts([{key:'old',status:'passed'}],{old:{since:'2026-01-01',lastAlert:'2026-01-01'}},80000);assert.equal(migrated.pending[0].type,'recovered');assert.equal(statusOf({ok:false}),'failed');
 const html=renderHtml({results:[{key:'x',title:'x',status:'skipped',message:'busy'}],totalMs:0});assert.match(html,/跳过/);assert.doesNotMatch(html,/本次覆盖项正常/);
});
test('stage allowlists block covers, writes, unselected images and external URLs',()=>{
 for(const stage of ['site','ai','payment','reader','chapter','citation'])for(const path of ['/reader/cover','/checkout/basic','/api/ai/comprehensive-chat','/api/community/weekly','/api/community/registry-geo'])assert.equal(permitted({stage,path,method:'GET',type:'fetch'}),false);
 const g=new NetworkGate({baseUrl:'http://127.0.0.1:9999'});g.stage='reader';g.target={file:'test.pdf',page:'18'};
 assert.equal(g.allow('http://127.0.0.1:9999/page-image?file=test.pdf&page=18','GET','image'),true);assert.equal(g.allow('http://127.0.0.1:9999/page-image?file=test.pdf&page=19','GET','image'),false);assert.equal(g.allow('https://third-party.invalid/','GET','document'),false);g.close();
});
test('network admission enforces concurrency, spacing, budget and deadline cancellation',async()=>{
 const g=new NetworkGate({baseUrl:'http://localhost',limits:{requests:4,ms:10000}});
 await Promise.all(Array.from({length:4},async()=>{await g.acquire();await sleep(450);g.release();}));
 assert.ok(g.metrics.peakConcurrency<=2);assert.equal(g.metrics.requests,4);for(let i=1;i<4;i++)assert.ok(g.metrics.starts[i]-g.metrics.starts[i-1]>=300);
 await assert.rejects(g.acquire(),/request_budget/);g.close();
 const d=new NetworkGate({baseUrl:'http://localhost',limits:{requests:10,ms:40}});await d.acquire();await sleep(60);await assert.rejects(d.acquire(),/round_deadline/);d.release();d.close();
});
// A deterministic 64x64 PNG, with no downloaded fixture or image library.
function png(){const crc=b=>{let c=0xffffffff;for(const x of b){c^=x;for(let i=0;i<8;i++)c=(c>>>1)^((c&1)?0xedb88320:0);}return (c^0xffffffff)>>>0;};const chunk=(t,b)=>{const h=Buffer.alloc(4);h.writeUInt32BE(b.length);const tb=Buffer.concat([Buffer.from(t),b]),end=Buffer.alloc(4);end.writeUInt32BE(crc(tb));return Buffer.concat([h,tb,end]);};const ih=Buffer.alloc(13);ih.writeUInt32BE(64);ih.writeUInt32BE(64,4);ih[8]=8;ih[9]=2;return Buffer.concat([Buffer.from('89504e470d0a1a0a','hex'),chunk('IHDR',ih),chunk('IDAT',zlib.deflateSync(Buffer.alloc(64*(64*3+1),0))),chunk('IEND',Buffer.alloc(0))]);}

test('real browser offline daily flow: hidden AI control, no orders/AI/covers, valid search and exact page decode',async()=>{
 const requests=[];const image=png();let active=0,peak=0;
 const server=http.createServer(async(req,res)=>{
  active++;peak=Math.max(peak,active);res.on('close',()=>active--);const u=new URL(req.url,'http://localhost');requests.push({path:u.pathname,method:req.method,time:Date.now()});
  res.setHeader('Server-Timing','cfOrigin;dur=1');
  const html=s=>{res.setHeader('Content-Type','text/html; charset=utf-8');res.end(`<html><head><meta charset="utf-8"><link rel="stylesheet" href="/static/a.css"><script src="/static/a.js"></script></head><body>${s}</body></html>`);};
  if(u.pathname==='/static/a.css'){res.setHeader('Content-Type','text/css');return res.end('body { color: black }');}
  if(u.pathname==='/static/a.js'){res.setHeader('Content-Type','application/javascript');return res.end('');}
  if(u.pathname==='/login'&&req.method==='POST'){req.resume();res.writeHead(302,{'Location':'/account','Set-Cookie':'session=fixture; Path=/; HttpOnly'});return res.end();}
  if(u.pathname==='/login')return html('<form action="/login" method="POST"><input name="email"><input type="password" name="password"><button>登录</button></form>');
  if(u.pathname==='/account')return html(req.headers.cookie?.includes('session=fixture')?'<form action="/logout"><button type="submit">退出</button></form>':'login required');
  if(u.pathname==='/register')return html(['email','password','confirm_password','email_code'].map(n=>`<input name="${n}">`).join(''));
  if(u.pathname==='/pricing')return html('<form action="/checkout/basic"><button>支付</button></form><img src="http://127.0.0.1:9/qr.png"><canvas width="200" height="200"></canvas>');
  if(u.pathname==='/v2/ai')return html('<input id="aipGrounding" type="checkbox" checked hidden><textarea id="aipPrompt"></textarea><button id="aipSend">发送</button><script>fetch("/api/ai/assistant-config"); fetch("/api/ai/comprehensive-chat",{method:"POST"}).catch(()=>{});</script>');
  if(u.pathname==='/api/ai/assistant-config'){res.setHeader('Content-Type','application/json');return res.end('{"ok":true}');}
  if(u.pathname==='/')return html(`<textarea id="q"></textarea><button id="submitBtn">检索</button><div id="results">正在搜索</div>${Array.from({length:70},(_,i)=>`<img src="/reader/cover?file=${i}">`).join('')}<script>fetch('/api/community/weekly').catch(()=>{});document.querySelector('#submitBtn').onclick=async()=>{const r=await fetch('/api/search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({q:document.querySelector('#q').value})}); const b=await r.json();if(b.ok)document.querySelector('#results').innerHTML='<div class="group-card">匹配结果</div>';}</script>`);
  if(u.pathname==='/api/search'){req.resume();res.setHeader('Content-Type','application/json');return res.end(JSON.stringify({ok:true,count:1,results:[{hits:[{viewer_available:true,viewer_url:'/viewer?file=test.pdf&page=18'}]}]}));}
  if(u.pathname==='/v2/read')return html('<input id="chapterSearchInput"><div id="chapterSearchResults"></div><script>document.querySelector("input").oninput=async()=>{const r=await fetch("/api/library/toc-suggest?q="+encodeURIComponent(document.querySelector("input").value));const b=await r.json();if(b.results.length){const a=document.createElement("a");a.href="/viewer?file=test.pdf&page=18";a.textContent="共产党宣言";document.querySelector("#chapterSearchResults").replaceChildren(a);}}</script>');
  if(u.pathname==='/api/library/toc-suggest'){res.setHeader('Content-Type','application/json');return res.end('{"ok":true,"results":[{"title":"共产党宣言"}]}');}
  if(u.pathname==='/viewer')return html('<canvas width="200" height="200"></canvas><img id="pageImage" src="/page-image?file=test.pdf&page=18"><img src="/page-image?file=test.pdf&page=19">');
  if(u.pathname==='/page-image'){res.setHeader('Content-Type','image/png');return res.end(image);}
  res.writeHead(404);res.end('unexpected');
 });
 await new Promise(r=>server.listen(0,'127.0.0.1',r));process.env.BASE_URL=`http://127.0.0.1:${server.address().port}`;process.env.MEMBER_EMAIL='member@test.invalid';process.env.MEMBER_PASSWORD='fixture';process.env.NONMEMBER_EMAIL='nonmember@test.invalid';process.env.NONMEMBER_PASSWORD='fixture';process.env.ALERT_CHANNEL='none';
 try{
  const {checks}=await import('../src/checks/index.js');const {runChecks}=await import('../src/lib/runner.js');
  const run=await runChecks(checks,{mode:'daily'});writeJSON(path.join(process.env.MONITOR_TEST_ARTIFACTS,'browser-fixture.json'),{run,requests,peak});
  assert.deepEqual(run.results.filter(r=>!r.ok),[]);assert.equal(requests.filter(r=>r.path==='/reader/cover').length,0);assert.equal(requests.filter(r=>r.path.startsWith('/checkout')).length,0);assert.equal(requests.filter(r=>r.path==='/api/ai/comprehensive-chat').length,0);
  assert.equal(requests.filter(r=>r.path==='/api/search').length,1);assert.equal(requests.filter(r=>r.path==='/page-image').length,1);assert.equal(requests.filter(r=>r.path==='/login'&&r.method==='POST').length,2);assert.ok(run.network.requests<=120);assert.ok(peak<=2);assert.ok(run.network.peakConcurrency<=2);assert.ok(run.network.cacheHits>0);
  for(let i=1;i<run.network.starts.length;i++)assert.ok(run.network.starts[i]-run.network.starts[i-1]>=300);
  assert.equal(checks.filter(c=>!c.daily).some(c=>['citation','chapter','reader'].includes(c.key)),false);
 }finally{await new Promise(r=>server.close(r));}
});

test('semantic failures do not pass on loading text, arbitrary canvas or failed responses',async()=>{
 const {checks}=await import('../src/checks/index.js');
 const fakeResponse={status:()=>200,json:async()=>({ok:false,count:0,results:[]}),headers:()=>({'content-type':'text/html'}),body:async()=>Buffer.from('bad')};
 const locator={count:async()=>1,fill:async()=>{},click:async()=>{},waitFor:async()=>{},first(){return this;}};
 const page={goto:async()=>({status:()=>200}),locator:()=>locator,waitForResponse:async()=>fakeResponse,waitForFunction:async()=>{throw new Error('loading text is not results');},evaluate:async()=>({width:999,height:999})};
 await assert.rejects(checks.find(c=>c.key==='citation').run(page,{}),/search_response_invalid/);
 await assert.rejects(checks.find(c=>c.key==='reader').run(page,{target:{file:'test.pdf',page:'18'},gate:{},viewer:'/viewer?file=test.pdf&page=18'}),/page_image_failed/);
 await assert.rejects(checks.find(c=>c.key==='chapter').run(page,{}),/chapter_response_invalid/);
});
