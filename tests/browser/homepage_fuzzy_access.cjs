const {test,before,after}=require('node:test');
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const path=require('node:path');
const fs=require('node:fs');
const {chromium}=require('playwright');
let server,browser,base;
before(async()=>{
  server=spawn(process.env.PYTHON,['-B','-u',path.join(__dirname,'serve_fuzzy_access.py')],{windowsHide:true,stdio:['ignore','pipe','pipe']});
  base=await new Promise((resolve,reject)=>{
    let out='',err='';const timer=setTimeout(()=>reject(Error(err||'fixture timeout')),60000);
    server.stderr.on('data',d=>{err+=d;});
    server.stdout.on('data',d=>{out+=d;const m=out.match(/READY=(http:\/\/[^\s]+)/);if(m){clearTimeout(timer);resolve(m[1]);}});
    server.on('error',reject);server.on('exit',code=>{clearTimeout(timer);reject(Error(code+': '+err));});
  });
  browser=await chromium.launch({channel:process.env.BROWSER_CHANNEL||'chrome',headless:true});
});
after(async()=>{if(browser)await browser.close();if(server)server.kill();});
async function pageFor(t,kind,width=1280){
  const context=await browser.newContext({viewport:{width,height:900}});
  const page=await context.newPage(),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  t.after(async()=>{await context.close();assert.deepEqual(errors,[]);});
  await page.goto(base+'/fixture-login/'+kind);
  return page;
}
async function submit(page,text){
  await page.locator('#q').fill(text);
  const pending=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/search/associative');
  await page.locator('#submitBtn').click();
  const response=await pending;
  assert.equal(response.status(),200);
  await page.locator('.assoc-strategy-results').waitFor();
  return response.json();
}
const forbidden=/"(?:context|textual_differences|citation|citations|pdf_pages|printed_pages|viewer_url|source_file|document_id|chapter_pdf_page)"\s*:/;

test('identity-specific buttons and independent notes on desktop and mobile',async t=>{
  for(const [kind,width,fuzzy,assoc] of [
    ['guest',1280,'登录后使用','登录后使用'],
    ['registered',390,'开始检索','开通会员后使用'],
    ['expired',1280,'开始检索','开通会员后使用'],
    ['member',390,'开始检索','开始检索'],
    ['blocked',1280,'暂不可检索','开通会员后使用'],
  ]){
    const page=await pageFor(t,kind,width);
    await page.locator('[data-mode=fuzzy]').click();
    assert.match(await page.locator('#submitBtn').innerText(),new RegExp(fuzzy));
    assert.match(await page.locator('#searchModeNote').innerText(),/登录后免费使用/);
    assert.doesNotMatch(await page.locator('#searchModeNote').innerText(),/OLD_PAYWALL/);
    assert.equal(await page.locator('#submitBtn').isDisabled(),kind==='blocked');
    await page.locator('[data-mode=associative]').click();
    assert.match(await page.locator('#submitBtn').innerText(),new RegExp(assoc));
    assert.match(await page.locator('#searchModeNote').innerText(),/不消耗个人 AI 额度/);
    assert.equal(await page.locator('[data-mode=associative] small').count(),0);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
    await page.screenshot({path:path.join(process.env.TEST_OUTPUT,kind+'-'+width+'.png'),fullPage:false});
  }
});

test('free automatic fallback preserves chapter scope and history contains only directories',async t=>{
  const page=await pageFor(t,'registered');
  await page.locator('.bscope-trigger').first().click();
  const row=page.locator('.bscope-book[data-key="文集"]').first();
  await row.locator('.bscope-chapter-box > summary').click();
  await row.locator('.bscope-chapter-box details > summary').first().click();
  await row.locator('.bscope-chapter-check').nth(1).check();
  const requests=[];
  page.on('request',r=>{if(['/api/search','/api/search/associative'].includes(new URL(r.url()).pathname)) requests.push(r.postDataJSON());});
  const data=await submit(page,'劳动创造社会财福');
  assert.equal(requests.length,2);
  assert.equal(requests[1].retrieval_strategy,'textual');
  assert.deepEqual(requests[1].scope,requests[0].scope);
  assert.equal(requests[1].scope_version,'fuzzy-v1');
  assert.equal(data.count,1);assert.equal(data.access_level,'summary');
  assert.doesNotMatch(JSON.stringify(data),forbidden);
  assert.match(await page.locator('.assoc-fallback-banner').innerText(),/不会自动进入联想/);
  assert.equal(await page.locator('[data-mode=fuzzy]').getAttribute('aria-pressed'),'true');
  assert.equal(await page.locator('#results .copy-btn').count(),0);
  const storage=await page.evaluate(()=>JSON.stringify({session:{...sessionStorage},local:{...localStorage}}));
  assert.doesNotMatch(storage,/劳动创造社会财富/);
  await page.reload();
  await page.locator('.assoc-strategy-results').waitFor();
  assert.match(await page.locator('.group-card').innerText(),/内部子节/);
  await page.screenshot({path:path.join(process.env.TEST_OUTPUT,'free-chapter-results.png'),fullPage:false});
});

test('free summaries paginate locally and old member history is scrubbed after expiration',async t=>{
  const page=await pageFor(t,'registered',390);
  await page.locator('[data-mode=fuzzy]').click();
  const data=await submit(page,'劳动创造社会财福');
  assert.ok(data.results.length>20);
  assert.equal(await page.locator('#results .group-card').count(),20);
  let extraRequests=0;
  page.on('request',r=>{if(new URL(r.url()).pathname==='/api/search/associative')extraRequests++;});
  await page.locator('[data-action=assoc-page]').filter({hasText:'下一页组'}).first().click();
  assert.equal(await page.locator('#results .group-card').count(),data.results.length-20);
  assert.equal(extraRequests,0);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
  await page.goto(base+'/fixture-login/member');
  await page.locator('[data-mode=fuzzy]').click();
  const full=await submit(page,'劳动创造社会财福');
  assert.equal(full.access_level,'full');
  assert.ok(full.results[0].context);
  const uid=(await (await page.request.get(base+'/fixture-identity')).json()).uid;
  await page.goto(base+'/fixture-expire');
  const entries=await page.evaluate(uid=>JSON.parse(localStorage.getItem('marx-search-history-v1:'+uid)),uid);
  assert.ok(entries.length);
  assert.doesNotMatch(JSON.stringify(entries),forbidden);
  await page.locator('#historyToggle').click();
  await page.locator('.sh-item').first().click();
  await page.locator('.assoc-strategy-results').waitFor();
  assert.equal(await page.locator('#results .ctx').count(),0);
  assert.ok(await page.locator('#results .group-card').count());
  const saved=await page.evaluate(()=>Object.fromEntries(Object.entries(sessionStorage).filter(([k])=>k.startsWith('marx-search-state'))));
  assert.doesNotMatch(JSON.stringify(saved),/劳动创造社会财富/);
});

test('fallback exclusions: guest, exact hit, incomplete scan, cooccurrence and paging',async t=>{
  const page=await pageFor(t,'guest');
  let fuzzyRequests=0;
  page.on('request',r=>{if(new URL(r.url()).pathname==='/api/search/associative')fuzzyRequests++;});
  await page.locator('#q').fill('完全不存在的句子');
  await Promise.all([page.waitForResponse(r=>new URL(r.url()).pathname==='/api/search'),page.locator('#submitBtn').click()]);
  await page.locator('#results .empty').waitFor();
  assert.equal(fuzzyRequests,0);
  await page.goto(base+'/fixture-login/registered');
  const response={ok:true,query:'劳动',count:1,results:[{book:'文集',volume:1,book_title:'文集',section_title:'第一作品',count:1,locked:true}],
    display_mode:'summary',access_level:'summary',group_pages:2,group_page:1,group_count:2,book_counts:[],exact_search_complete:true};
  let scenario='hit';
  await page.route('**/api/search',route=>route.fulfill({json:scenario==='hit'?response:
    {...response,count:0,results:[],exact_search_complete:scenario!=='incomplete',group_pages:1}}));
  await page.locator('#q').fill('劳动创造社会财富');
  await page.locator('#submitBtn').click();await page.locator('#results .group-card').waitFor();
  assert.equal(fuzzyRequests,0);
  scenario='page';
  await page.locator('[data-action=group-list-page]').filter({hasText:'下一组页'}).first().click();
  await page.locator('#results .empty').waitFor();assert.equal(fuzzyRequests,0);
  scenario='incomplete';
  await page.locator('#submitBtn').click();
  await page.getByText('排版补充检索未完成，以下保留已有结果；请稍后重试。',{exact:true}).waitFor();
  assert.equal(fuzzyRequests,0);
  scenario='cooc';
  await page.locator('#coocCheckbox').check();
  await page.locator('#q').fill('劳动 财富');
  await page.locator('#submitBtn').click();await page.locator('#results .empty').waitFor();
  assert.equal(fuzzyRequests,0);
});
