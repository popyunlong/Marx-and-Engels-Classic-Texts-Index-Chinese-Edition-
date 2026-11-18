const {test, before, after} = require('node:test');
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const {chromium} = require('playwright');
let server, browser, base;
before(async () => {
  server = spawn(process.env.PYTHON || 'python', ['-u', path.join(__dirname, 'serve_citation_scope_fixture.py')], {stdio:['ignore','pipe','pipe']});
  base = await new Promise((resolve, reject) => {
    let output='', errors='';
    const timer=setTimeout(()=>reject(new Error(errors || 'fixture timeout')),20000);
    server.stderr.on('data', d=>{errors+=d;});
    server.stdout.on('data', d=>{output+=d;const match=output.match(/READY=(http:\/\/[^\s]+)/);if(match){clearTimeout(timer);resolve(match[1]);}});
    server.on('error', reject);
    server.on('exit', code=>{clearTimeout(timer);reject(new Error(`fixture exited ${code}: ${errors}`));});
  });
  browser=await chromium.launch({headless:true,...(process.env.BROWSER_CHANNEL?{channel:process.env.BROWSER_CHANNEL}:{})});
});
after(async()=>{if(browser)await browser.close();if(server)server.kill();});
async function setup(t, route='/citation-agent', width=1200) {
  const context=await browser.newContext({viewport:{width,height:900}}),page=await context.newPage(),errors=[],requests=[];
  page.on('pageerror', e=>errors.push(e.message));
  await page.route('**/api/**',async route=>{
    requests.push(route.request());
    await route.fulfill({status:400,json:{error:'fixture: request captured'}});
  });
  t.after(async()=>{await context.close();assert.deepEqual(errors,[]);});
  await page.goto(base+route);
  return {page, requests};
}
const category=(page,id)=>page.locator(`[data-category="${id}"]`);
async function pressed(page,id,value){assert.equal(await category(page,id).getAttribute('aria-pressed'),value);}
async function openDetails(page){await page.locator('.bscope-trigger').click();}

test('category union, partial volumes, public-only toggle, keyboard and clear',async t=>{
  const {page}=await setup(t);
  await pressed(page,'all-public','false');
  await category(page,'marx_engels').click();
  await category(page,'lenin').click();
  await pressed(page,'all-public','true');
  await category(page,'mylib').click();
  await category(page,'all-public').click();
  await pressed(page,'mylib','true');
  await pressed(page,'marx_engels','false');
  await category(page,'marx_engels').focus();await page.keyboard.press('Space');
  await openDetails(page);
  const book=page.locator('.bscope-book[data-key="文集"]');
  await book.locator('.bscope-vol-toggle').click();
  await book.locator('.bscope-vol-cb[value="2"]').uncheck();
  await pressed(page,'marx_engels','mixed');
  await pressed(page,'all-public','mixed');
  assert.equal(await book.locator('.bscope-book-cb').evaluate(el=>el.indeterminate),true);
  await openDetails(page);
  await category(page,'marx_engels').click();
  await pressed(page,'marx_engels','true');
  await pressed(page,'lenin','false');
  await page.locator('.bscope-shortcuts-clear').click();
  for(const id of ['all-public','marx_engels','lenin','mylib']) await pressed(page,id,'false');
});

test('upload blocks empty scope and submits explicit book tokens',async t=>{
  const {page,requests}=await setup(t);
  await page.locator('#catFile').setInputFiles({name:'test.docx',mimeType:'application/vnd.openxmlformats-officedocument.wordprocessingml.document',buffer:Buffer.from('fixture')});
  await page.locator('#catUploadForm button[type="submit"]').click();
  assert.equal(requests.length,0);
  await category(page,'lenin').click();
  const sent=page.waitForRequest(r=>r.url().endsWith('/api/citation-agent/jobs'));
  await page.locator('#catUploadForm button[type="submit"]').click();
  const req=await sent;
  assert.ok(req.postDataBuffer().toString().includes('["book:列宁全集"]'));
});

test('saved scope restores exactly, clear blocks analysis, changes submit exact scope',async t=>{
  const {page,requests}=await setup(t,'/citation-agent/jobs/fixture');
  await pressed(page,'marx_engels','mixed');await pressed(page,'lenin','true');await pressed(page,'mylib','true');
  const response=page.waitForResponse(r=>r.url().endsWith('/analyze'));
  await page.locator('#catStartAnalysis').click();
  await response;
  assert.deepEqual(requests[0].postDataJSON().scope,['vol:文集:2','book:列宁全集','book:mylib:fixture']);
  await page.locator('.bscope-shortcuts-clear').click();
  await page.locator('#catStartAnalysis').click();
  assert.equal(requests.length,1);
  await category(page,'marx_engels').click();
  const sent=page.waitForRequest(r=>r.url().endsWith('/analyze'));
  await page.locator('#catStartAnalysis').click();
  assert.deepEqual((await sent).postDataJSON().scope,['book:文集','book:全集']);
});

test('oversized selection is blocked before upload and button remains usable',async t=>{
  const {page,requests}=await setup(t,'/citation-agent?large=1');
  await page.locator('#catFile').setInputFiles({name:'test.docx',mimeType:'application/octet-stream',buffer:Buffer.from('fixture')});
  await category(page,'all-public').click();
  await page.locator('#catUploadForm button[type="submit"]').click();
  assert.equal(requests.length,0);
  assert.match(await page.locator('#catNotice').textContent(),/500/);
  assert.equal(await page.locator('#catUploadForm button[type="submit"]').isEnabled(),true);
});

test('started jobs do not expose editable selection',async t=>{
  const {page}=await setup(t,'/citation-agent/jobs/fixture?status=queued');
  assert.equal(await page.locator('.bscope-category').count(),0);
});

test('shared control keeps shortcuts opt-in and filters stale saved tokens',async t=>{
  const {page}=await setup(t);
  const result=await page.evaluate(()=>{
    const holder=document.createElement('div');document.body.appendChild(holder);
    const ctl=BookScope.mount(holder,[{id:'only',label:'Only',books:[{key:'known',label:'Known',volumes:[]}]}],{initialTokens:['book:known','book:removed']});
    return {categories:holder.querySelectorAll('.bscope-category').length,tokens:ctl.getTokens()};
  });
  assert.deepEqual(result,{categories:0,tokens:['book:known']});
});

for(const width of [390,768,1280]) test(`categories and details fit ${width}px viewport`,async t=>{
  const {page}=await setup(t,'/citation-agent',width);
  await category(page,'marx_engels').click();
  await page.locator('.bscope-shortcuts').scrollIntoViewIfNeeded();
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  for(const button of await page.locator('.bscope-category').all()) {
    const box=await button.boundingBox();assert.ok(box.x>=0&&box.x+box.width<=width+1);
  }
  if(process.env.CITATION_SCOPE_SCREENSHOTS){
    fs.mkdirSync(process.env.CITATION_SCOPE_SCREENSHOTS,{recursive:true});
    await page.screenshot({path:path.join(process.env.CITATION_SCOPE_SCREENSHOTS,`scope-${width}.png`)});
  }
  await openDetails(page);
  const box=await page.locator('.bscope-panel').boundingBox();assert.ok(box.x>=0&&box.x+box.width<=width+1);
});

test('interrupted progress reconnects to partial evidence, then pause and resume use same job',async t=>{
  const {page}=await setup(t,'/citation-agent/jobs/fixture?status=matching');
  let reads=0;
  let current={id:'fixture',status:'matching',mode:'both',partial_results:true,analysis_stage:'agent_planning',candidate_count:1};
  const evidence={paper_text:'劳动过裎',source_text:'劳动过程',differences:[{paper_start:3,paper:'裎',source:'程'}]};
  const candidate={id:9,review_revision:'fixture-revision',evidence_level:'near_text',paper_text:evidence.paper_text,decision:'pending',source_options:[{quote_evidence:evidence,display_title:'原书',printed_pages:['42']}]};
  await page.route(/\/api\/citation-agent\/jobs\/fixture(?:[/?]|$)/, async route=>{
    const pathname=new URL(route.request().url()).pathname;
    if(pathname.endsWith('/candidates'))return route.fulfill({json:{items:[candidate],total:1,summary:{pending:1}}});
    if(pathname.endsWith('/stop'))current={...current,status:'stopped',resumable:true};
    else if(pathname.endsWith('/resume'))current={...current,status:'queued',resumable:false};
    else if(++reads===1)return route.abort('connectionreset');
    return route.fulfill({json:{ok:true,job:current}});
  });
  await page.locator('#catConnection').waitFor({state:'visible'});
  await page.locator('#catReviewPanel .cat-differences').waitFor();
  assert.equal(await page.locator('#catConnection').isHidden(),true);
  assert.match(await page.locator('.cat-differences li').textContent(),/裎.*程/);
  await page.locator('#catStop').click();
  await page.locator('#catResume').waitFor();
  await page.locator('#catResume').click();
  await page.locator('#catResume').waitFor({state:'hidden'});
  assert.equal(new URL(page.url()).pathname,'/citation-agent/jobs/fixture');
});
