const {test,before,after}=require('node:test');
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const fs=require('node:fs'),path=require('node:path');
const {chromium}=require('playwright');
let server,browser,base;
const output=process.env.ADMIN_OUTPUT;
before(async()=>{
  server=spawn(process.env.PYTHON,['-B','-u',path.join(__dirname,'../membership_admin_fixture_server.py')],{windowsHide:true,stdio:['ignore','pipe','pipe']});
  const log=fs.createWriteStream(path.join(output,'browser-fixture.log'));server.stdout.pipe(log);server.stderr.pipe(log);
  base=await new Promise((resolve,reject)=>{
    let all='';const timer=setTimeout(()=>reject(Error('fixture timeout '+all)),60000);
    server.stdout.on('data',d=>{all+=d;const m=all.match(/READY=(http:\/\/[^\s]+)/);if(m){clearTimeout(timer);resolve(m[1]);}});
    server.on('error',reject);server.on('exit',code=>{clearTimeout(timer);reject(Error('fixture exited '+code));});
  });
  browser=await chromium.launch({...(process.env.BROWSER_CHANNEL ? {channel:process.env.BROWSER_CHANNEL} : {}),headless:true});
});
after(async()=>{
  if(browser)await browser.close();
  if(server&&server.exitCode===null){
    if(base)await fetch(base+'/fixture/shutdown',{method:'POST'});
    await new Promise(resolve=>{const timer=setTimeout(()=>{server.kill();resolve();},10000);server.once('exit',()=>{clearTimeout(timer);resolve();});});
  }
});
for(const width of [1280,390])test('admin membership editor and independent statistics '+width,async()=>{
  const ctx=await browser.newContext({viewport:{width,height:900}}),page=await ctx.newPage(),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  try{
    await page.goto(base+'/fixture/login/admin');
    await page.locator('#model-policies').waitFor();
    const policy=page.locator('#model-policies form').filter({has:page.locator('input[value="support_plus_20"]')});
    assert.equal(await policy.locator('input[name="pro"]').isChecked(),false);
    assert.equal(await policy.locator('select[name="research"]').inputValue(),'flash');
    const copy=page.locator('.membership-copy-form').filter({has:page.locator('input[name="group"][value="support_plus_20"]')});
    const text='本轮会员文案 '+width;
    await copy.locator('textarea[name$=".description"]').fill(text);
    await copy.locator('[data-copy-preview]').click();
    await page.locator('#membership-copy-preview').waitFor({state:'visible'});
    await page.frameLocator('#membership-copy-preview').getByText(text,{exact:true}).waitFor();
    await copy.getByRole('button',{name:'保存此组',exact:true}).click();
    await page.waitForURL('**/admin/members#membership-copy');
    const visible=await page.locator('textarea[name$="support_plus_20.description"]').inputValue();
    assert.equal(visible,text);
    await page.screenshot({path:path.join(output,'member-editor-'+width+'.png')});
    const overflow=await page.evaluate(()=>({width:innerWidth,scrollWidth:document.documentElement.scrollWidth,
      elements:[...document.querySelectorAll('body *')].filter(e=>e.getBoundingClientRect().right>innerWidth+1)
        .slice(0,40).map(e=>({tag:e.tagName,id:e.id,classes:e.className,right:e.getBoundingClientRect().right}))}));
    fs.writeFileSync(path.join(output,'member-layout-'+width+'.json'),JSON.stringify(overflow,null,2));
    assert.equal(overflow.scrollWidth>width+1,false);
    await page.goto(base+'/pricing');
    assert.ok((await page.locator('body').innerText()).includes(text));
    assert.equal(await page.locator('.notice ol li').count(),6);
    await page.screenshot({path:path.join(output,'pricing-'+width+'.png'),fullPage:true});
    await page.goto(base+'/admin/overview');
    await page.locator('[data-section="summary"][data-loaded="1"]').waitFor();
    assert.equal(await page.locator('[data-section="history"]').getAttribute('data-loaded'),null);
    await page.locator('[data-section="history"] [data-admin-load]').click();
    await page.locator('[data-section="history"][data-loaded="1"]').waitFor();
    assert.ok(await page.locator('[data-section="history"] tbody tr').count()>=30);
    await page.screenshot({path:path.join(output,'overview-'+width+'.png')});
    await page.route('**/admin/overview/data?section=readers*',route=>route.fulfill({status:503,body:'unavailable'}));
    await page.locator('[data-section="readers"] [data-admin-load]').click();
    await page.locator('[data-section="readers"] .admin-data-status').filter({hasText:'加载失败'}).waitFor();
    assert.ok(await page.locator('[data-section="summary"] .stats-grid').count());
    await page.goto(base+'/admin/content');
    assert.ok(await page.getByRole('link',{name:'编辑会员卡片与黄框文案'}).count());
    assert.ok(await page.locator('#copy textarea,#copy input[name^="site_text__"]').count()<=40);
    assert.deepEqual(errors,[]);
  }finally{await ctx.close();}
});
