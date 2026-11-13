const {test,before,after}=require('node:test');
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const path=require('node:path');
const fs=require('node:fs');
const {chromium}=require('playwright');
let server,browser,base;
before(async()=>{
 server=spawn(process.env.PYTHON,['-u',path.join(__dirname,'serve_membership_refresh.py')],{stdio:['ignore','pipe','pipe'],windowsHide:true});
 base=await new Promise((resolve,reject)=>{
  let out='',err='';const timer=setTimeout(()=>reject(Error(err||out||'fixture timeout')),60000);
  server.stderr.on('data',d=>err+=d);server.stdout.on('data',d=>{out+=d;const m=out.match(/READY=(http:\/\/[^\s]+)/);if(m){clearTimeout(timer);resolve(m[1]);}});
  server.on('exit',code=>{clearTimeout(timer);reject(Error('fixture '+code+': '+err));});
 });
 browser=await chromium.launch({channel:process.env.BROWSER_CHANNEL||'chrome',headless:true});
});
after(async()=>{if(browser)await browser.close();if(server)server.kill();});

test('member center, selection persistence, plans and mobile layout',async()=>{
 const ctx=await browser.newContext({viewport:{width:1280,height:900}});const page=await ctx.newPage();const errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 try{
  await page.goto(base+'/fixture-login/plus');
  assert.match(await page.locator('body').innerText(),/1000000/);
  assert.doesNotMatch(await page.locator('body').innerText(),/MiMo/);
  await page.locator('[data-journals=none]').click();
  await page.locator('#researchPreferences details').first().locator('summary').click();
  await page.locator('.journal-choice input').first().check();
  await page.locator('#saveResearchPreferences').click();
  await page.getByText('订阅设置已保存。',{exact:true}).waitFor();
  await page.reload();assert.equal(await page.locator('.journal-choice input:checked').count(),1);
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
  await page.screenshot({path:path.join(process.env.MEMBERSHIP_REFRESH_OUTPUT,'account-mobile.png'),fullPage:true});
  await page.goto(base+'/pricing');assert.match(await page.locator('body').innerText(),/¥20.00/);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
  await page.goto(base+'/fixture-login/max');assert.match(await page.locator('body').innerText(),/最高优先/);
  assert.deepEqual(errors,[]);
 }finally{await ctx.close();}
});

test('public sample uses current reader, filters, citation download and mobile layout',async()=>{
 const ctx=await browser.newContext({viewport:{width:1280,height:900}});const page=await ctx.newPage();
 try{
  await page.goto(base+'/research-updates/sample');assert.match(await page.locator('body').innerText(),/公开样刊/);
  assert.equal(await page.locator('.catalogue-entry').count(),12);
  await page.locator('[data-fold=expand]').click();
  await page.locator('.article-details').first().locator('summary').click();
  const download=page.waitForEvent('download');await page.getByText('RIS',{exact:true}).first().click();
  assert.match((await download).suggestedFilename(),/\.ris$/);
  await page.locator('.catalogue-entry h3 a').first().click();assert.equal(await page.locator('.catalogue-entry').count(),1);
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
  await page.screenshot({path:path.join(process.env.MEMBERSHIP_REFRESH_OUTPUT,'sample-mobile.png'),fullPage:true});
 }finally{await ctx.close();}
});

test('knowledge base exports Word, Markdown, JSON and exposes original page link',async()=>{
 const ctx=await browser.newContext({viewport:{width:1280,height:900},acceptDownloads:true});const page=await ctx.newPage();
 try{
  await page.goto(base+'/fixture-login/plus');await page.goto(base+'/knowledge-base');
  await page.locator('.mxn-card').first().waitFor();
  const controls=page.locator('.mxn-kb-actions .mxn-export-controls');
  for(const fmt of ['docx','md','json']){
   await controls.locator('select').selectOption(fmt);const pending=page.waitForEvent('download');
   await controls.locator('button').click();const file=await pending;
   await file.saveAs(path.join(process.env.MEMBERSHIP_REFRESH_OUTPUT,'note-preview.'+fmt));
  }
  await page.locator('.mxn-act-jump').first().click();await page.waitForURL(/\/viewer\?/);
 }finally{await ctx.close();}
});
