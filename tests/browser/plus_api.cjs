const {test,before,after}=require('node:test');
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const path=require('node:path');
const fs=require('node:fs');
const {chromium}=require('playwright');
let server,browser,base;
const output=process.env.PLUS_API_OUTPUT;
before(async()=>{
  server=spawn(process.env.PYTHON,['-B','-u',path.join(__dirname,'../plus_api_fixture_server.py')],
    {windowsHide:true,stdio:['ignore','pipe','pipe']});
  const log=fs.createWriteStream(path.join(output,'browser-fixture.log'));
  server.stdout.pipe(log);server.stderr.pipe(log);
  base=await new Promise((resolve,reject)=>{
    let out='',err='';
    const timer=setTimeout(()=>reject(Error(err||out||'fixture timeout')),60000);
    server.stdout.on('data',d=>{out+=d;const m=out.match(/READY=(http:\/\/[^\s]+)/);if(m){clearTimeout(timer);resolve(m[1]);}});
    server.stderr.on('data',d=>err+=d);
    server.on('error',e=>{clearTimeout(timer);reject(e);});
    server.on('exit',code=>{clearTimeout(timer);reject(Error('fixture '+code+': '+err));});
  });
  browser=await chromium.launch({channel:process.env.BROWSER_CHANNEL||'chrome',headless:true});
});
after(async()=>{
  if(browser)await browser.close();
  if(server && server.exitCode===null){
    if(base)await fetch(base+'/fixture/shutdown',{method:'POST'});
    await new Promise(resolve=>{
      const timer=setTimeout(()=>{server.kill();resolve();},10000);
      server.once('exit',()=>{clearTimeout(timer);resolve();});
    });
  }
});
for(const width of [1280,390]){
  test('Plus API entitlement and tier copy at width '+width,async()=>{
    const ctx=await browser.newContext({viewport:{width,height:900}});
    const page=await ctx.newPage(),errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    const layout=async()=>assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
    try{
      await page.goto(base+'/pricing');
      let text=await page.locator('body').innerText();
      for(const wording of ['Plus 普通排队','Pro 优先排队','Max 最高优先排队','¥20.00','¥45.00','¥75.00'])
        assert.ok(text.includes(wording),wording);
      assert.doesNotMatch(text,/不含 API|增加 API/);
      await layout();
      await page.screenshot({path:path.join(output,'pricing-'+width+'.png'),fullPage:true});
      for(const [tier,label] of [['plus','普通'],['pro','优先'],['max','最高优先'],['basic','普通'],['oldplus','普通'],['legacy','普通']]){
        await page.goto(base+'/fixture-login/'+tier);
        text=await page.locator('body').innerText();
        assert.ok(text.includes('API/MCP：'+label+'排队。'),tier);
        const benefit=page.locator('.benefit').filter({has:page.locator('.benefit-name',{hasText:'API/MCP 数据调用'})});
        assert.equal(await benefit.locator('.benefit-state').innerText(),'已开放');
        await layout();
        if(['plus','pro','max'].includes(tier))
          await page.screenshot({path:path.join(output,'account-'+tier+'-'+width+'.png'),fullPage:true});
        await page.goto(base+'/api-access');
        await page.locator('#key-name').waitFor();
        assert.ok((await page.locator('#limits').innerText()).includes('三档接口功能与调用额度相同'));
        await layout();
      }
      await page.goto(base+'/fixture-login/plus');
      await page.goto(base+'/api-access');
      await page.locator('#key-name').fill('browser-'+width);
      await page.getByRole('button',{name:'创建 Key',exact:true}).click();
      await page.getByRole('heading',{name:'保存你的新 Key',exact:true}).waitFor();
      // Remove the newly-created fixture key, then capture the clean management page.
      await page.goto(base+'/api-access');
      await page.locator('.key').filter({hasText:'browser-'+width}).getByRole('button',{name:'撤销',exact:true}).click();
      assert.equal(await page.locator('.key').filter({hasText:'browser-'+width}).count(),0);
      await page.screenshot({path:path.join(output,'api-'+width+'.png'),fullPage:true});
      assert.deepEqual(errors,[]);
    }finally{await ctx.close();}
  });
}
