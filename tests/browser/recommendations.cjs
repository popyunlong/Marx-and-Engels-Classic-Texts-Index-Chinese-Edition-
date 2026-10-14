const {test,before,after}=require('node:test');
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const path=require('node:path');
const {chromium}=require('playwright');
let server,browser,base;
before(async()=>{
  server=spawn(process.env.PYTHON||'python',['-u',path.join(__dirname,'serve_recommendations_fixture.py')]);
  base=await new Promise((resolve,reject)=>{
    let output='',errors='';
    const timer=setTimeout(()=>reject(new Error(errors||'fixture timeout')),20000);
    server.stdout.on('data',d=>{output+=d;const m=output.match(/READY=(http:\/\/[^\s]+)/);if(m){clearTimeout(timer);resolve(m[1]);}});
    server.stderr.on('data',d=>errors+=d);
    server.on('error',reject);
    server.on('exit',code=>{clearTimeout(timer);reject(new Error(`fixture ${code}: ${errors}`));});
  });
  browser=await chromium.launch({headless:true,...(process.env.BROWSER_CHANNEL?{channel:process.env.BROWSER_CHANNEL}:{})});
});
after(async()=>{if(browser)await browser.close();if(server)server.kill();});
for(const width of [1280,390])test(`recommendation controls and layout at ${width}px`,async t=>{
  const context=await browser.newContext({viewport:{width,height:900}});
  t.after(()=>context.close());
  const page=await context.newPage();await page.goto(base);
  assert.equal(await page.getByRole('button',{name:'审核通过并处理'}).count(),1);
  assert.equal(await page.getByRole('button',{name:'重试处理'}).count(),1);
  assert.equal(await page.getByRole('button',{name:'退回',exact:true}).count(),2);
  assert.equal(await page.getByRole('link',{name:'打开公开书目'}).getAttribute('href'),'/v2/read?book=public-book');
  assert.equal(await page.locator('form').evaluateAll(forms=>forms.every(f=>f.method==='post'&&f.querySelector('[name=csrf_token]').value==='fixture-csrf')),true);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  assert.equal(await page.locator('input[name=reason]').first().getAttribute('required'),'');
  if(process.env.RECOMMENDATION_SCREENSHOTS)await page.screenshot({path:path.join(process.env.RECOMMENDATION_SCREENSHOTS,`admin-${width}.png`),fullPage:true});
});
