/* Local acceptance against a real, immutable dictionary artifact. */
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const fs=require('node:fs/promises');
const path=require('node:path');
const os=require('node:os');
const {chromium}=require('playwright');

(async()=>{
  assert(process.env.DICTIONARY_GRAPH_FIXTURE,'Set DICTIONARY_GRAPH_FIXTURE to a real local artifact');
  assert(process.env.DICTIONARY_ACCEPTANCE_OUTPUT,'Set a local D-drive output directory');
  const output=process.env.DICTIONARY_ACCEPTANCE_OUTPUT;
  await fs.mkdir(output,{recursive:true});
  const server=spawn(process.env.PYTHON||'python',['-B','-u',path.join(__dirname,'../tests/browser/serve_dictionary_fixture.py')],
    {env:{...process.env,DICTIONARY_FIXTURE_PORT:'0'},stdio:['ignore','pipe','pipe']});
  let browser;
  try{
    const base=await new Promise((resolve,reject)=>{
      let out='',err='';const timer=setTimeout(()=>reject(new Error('fixture timeout: '+err)),30000);
      server.stderr.on('data',d=>err+=d);
      server.stdout.on('data',d=>{out+=d;const m=out.match(/READY=(http:\/\/[^\s]+)/);if(m){clearTimeout(timer);resolve(m[1]);}});
      server.once('exit',code=>{clearTimeout(timer);reject(new Error('fixture exited '+code));});
    });
    browser=await chromium.launch({headless:true,...(process.env.BROWSER_CHANNEL?{channel:process.env.BROWSER_CHANNEL}:{})});
    const results=[];
    for(const width of [1440,390]){
      const ctx=await browser.newContext({viewport:{width,height:960}});
      const p=await ctx.newPage();const errors=[];p.on('pageerror',e=>errors.push(e.message));
      const samples=[];
      for(let i=0;i<21;i++){
        const start=performance.now();
        await p.goto(base+'/dictionary/map?center='+encodeURIComponent('资本-121')+'&limit=59');
        await p.waitForFunction(()=>document.getElementById('dmStatus').textContent.includes('60 个节点'));
        assert.equal(await p.locator('#dmNodes button').count(),60);
        await p.locator('#dmEdges button').first().click();
        assert(await p.locator('#dmDetail blockquote').count()>0);
        if(i>0)samples.push(performance.now()-start);
      }
      assert.deepEqual(errors,[]);
      assert(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
      await p.goto(base+'/dictionary/map?center='+encodeURIComponent('资本-121'));
      await p.waitForFunction(()=>document.getElementById('dmStatus').textContent.includes('13 个节点'));
      await p.locator('#dmEdges button').first().click();
      await p.evaluate(()=>window.scrollTo(0,0));
      await p.waitForFunction(()=>window.scrollY===0);
      await p.screenshot({path:path.join(output,`map-${width}.png`),fullPage:true});
      samples.sort((a,b)=>a-b);
      const p95=samples[18];
      results.push({viewport_width:width,samples:20,p95_interactive_ms:p95,max_ms:samples[19],nodes:60});
      // Timed through opening a relation's evidence, not merely receiving JSON.
      assert(p95<=2000,JSON.stringify(results));
      await ctx.close();
    }
    const report={cpu:os.cpus()[0].model,platform:os.platform(),browser:await browser.version(),
      graph:JSON.parse(await fs.readFile(path.join(process.env.DICTIONARY_GRAPH_FIXTURE,'binding.json'),'utf8')).id,
      scope:'local browser with isolated fixture; no production writes or model calls',results};
    await fs.writeFile(path.join(output,'browser-performance.json'),JSON.stringify(report,null,2)+'\n');
    process.stdout.write(JSON.stringify(report)+'\n');
  }finally{if(browser)await browser.close();server.kill();}
})().catch(e=>{process.stderr.write(e.stack+'\n');process.exitCode=1;});
