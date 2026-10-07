/* Acceptance against the immutable, real dictionary artifact; no app stores or model calls. */
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const fs=require('node:fs/promises');
const path=require('node:path');
const os=require('node:os');
const {chromium}=require('playwright');
(async()=>{
  assert(process.env.DICTIONARY_GRAPH_FIXTURE,'Set DICTIONARY_GRAPH_FIXTURE to the accepted artifact');
  assert(process.env.DICTIONARY_ACCEPTANCE_OUTPUT,'Set a task-owned output directory');
  const output=process.env.DICTIONARY_ACCEPTANCE_OUTPUT;await fs.mkdir(output,{recursive:true});
  const server=spawn(process.env.PYTHON||'python',['-B','-u',path.join(__dirname,'../tests/browser/serve_dictionary_fixture.py')],
    {env:{...process.env,DICTIONARY_FIXTURE_PORT:'0'},stdio:['ignore','pipe','pipe']});
  let browser;
  try{
    const base=await new Promise((resolve,reject)=>{
      let out='',err='';const timer=setTimeout(()=>reject(new Error('fixture timeout: '+err)),30000);
      server.stderr.on('data',d=>err+=d);server.stdout.on('data',d=>{out+=d;const m=out.match(/READY=(http:\/\/[^\s]+)/);if(m){clearTimeout(timer);resolve(m[1]);}});
      server.once('exit',code=>{clearTimeout(timer);reject(new Error('fixture exited '+code));});
    });
    browser=await chromium.launch({headless:true,...(process.env.BROWSER_CHANNEL?{channel:process.env.BROWSER_CHANNEL}:{})});
    const api=await browser.newContext();
    const get=async(endpoint,params={})=>{const r=await api.request.get(base+endpoint+'?'+new URLSearchParams(params));assert.equal(r.status(),200);return r.json();};
    const overview=await get('/api/dictionary/graph');
    const themes=[];
    for(const theme of overview.themes){
      const slugs=[];
      for(let offset=0;offset<theme.count;offset+=30){
        const page=await get('/api/dictionary/graph',{theme:theme.theme,offset,limit:30});
        assert.equal(page.total,theme.count);slugs.push(...page.nodes.map(n=>n.slug));
      }
      assert.equal(new Set(slugs).size,theme.count);assert.equal(slugs.length,theme.count);themes.push(theme);
    }
    const hubs=[];
    for(const [term,expected] of [['社会',1103],['国家',1021],['资本',300]]){
      const choices=await get('/api/dictionary/suggest',{q:term});const slug=choices.results.find(n=>n.title===term).slug;
      const all=await get('/api/dictionary/relations',{center:slug});assert.equal(all.total_relations,expected);
      const edgeIds=[];
      for(const group of all.groups.filter(g=>g.count&&g.id!=='all')){
        const slugs=[];
        for(let offset=0;offset<group.count;offset+=20){
          const page=await get('/api/dictionary/relations',{center:slug,group:group.id,offset,limit:20});
          assert.equal(page.total,group.count);slugs.push(...page.items.map(i=>i.slug));edgeIds.push(...page.edges.map(e=>e.id));
          assert(page.edges.every(e=>e.layer==='evidence'&&e.evidence.every(ev=>ev.quote&&ev.url.includes('#paragraph-'))));
        }
        assert.equal(slugs.length,group.count);assert.equal(new Set(slugs).size,group.count);
      }
      assert.equal(edgeIds.length,expected);assert.equal(new Set(edgeIds).size,expected);hubs.push({term,slug,relations:expected});
    }
    const results=[];
    for(const width of [390,820,1440]){
      const ctx=await browser.newContext({viewport:{width,height:960}});const p=await ctx.newPage();
      const errors=[],requests=[];p.on('pageerror',e=>errors.push(e.message));p.on('request',r=>requests.push(r.url()));
      const ready=()=>p.waitForFunction(()=>!document.getElementById('dmWorkspace').hidden);
      const check=async()=>assert(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
      await p.goto(base+'/dictionary/map?theme='+encodeURIComponent('中国马克思主义'));await ready();await check();
      assert.equal(await p.locator('.dm-entry').count(),30);assert(!requests.some(u=>u.includes('cytoscape')));
      await p.screenshot({path:path.join(output,`directory-${width}.png`)});
      const center=hubs.find(h=>h.term==='资本').slug;
      const samples=[];
      for(let i=0;i<6;i++){
        const start=performance.now();await p.goto(base+'/dictionary/map?center='+encodeURIComponent(center)+'&view=list');await ready();
        await p.locator('#dmEdges summary').first().click();assert(await p.locator('#dmEdges details[open] blockquote').first().isVisible());
        if(i)samples.push(performance.now()-start);
      }
      await p.locator('#dmEdges summary').first().click();
      await p.locator('#dmTitle').evaluate(el=>el.scrollIntoView({block:'start'}));await check();
      await p.screenshot({path:path.join(output,`reading-${width}.png`)});
      assert(!requests.some(u=>u.includes('cytoscape')));
      await p.locator('#dmGraphView').click();await p.locator('#dmPng').waitFor({state:'visible'});await check();
      const metrics=await p.locator('#dmCanvas').evaluate(el=>{
        const cy=el._cyreg.cy, nodes=cy.nodes();
        return {nodes:nodes.length,zoom:cy.zoom(),font:nodes.map(n=>n.renderedStyle('font-size')),
          overlap:nodes.some((a,i)=>nodes.some((b,j)=>{if(i>=j)return false;const x=a.renderedBoundingBox(),y=b.renderedBoundingBox();return x.x1<y.x2&&x.x2>y.x1&&x.y1<y.y2&&x.y2>y.y1;})),
          clipped:nodes.some(n=>{const r=n.renderedBoundingBox();return r.x1<0||r.y1<0||r.x2>el.clientWidth||r.y2>el.clientHeight;})};
      });
      assert.equal(metrics.overlap,false);assert.equal(metrics.clipped,false);assert.equal(metrics.zoom,1);assert(metrics.font.every(f=>parseFloat(f)>=14));
      await p.locator('#dmGraphPanel').screenshot({path:path.join(output,`diagram-${width}.png`)});
      const sample='1954年宪法中的人民民主原则和社会主义原则-798';
      await p.goto(base+'/dictionary/map?center='+encodeURIComponent(sample));await ready();
      await p.locator('#dmPng').waitFor({state:'visible'});
      assert((await p.locator('#dmGraphNote').textContent()).includes('完整展示 · 8 个相关词条 / 8 条关系'));
      assert.equal(await p.locator('#dmCanvas').evaluate(el=>el._cyreg.cy.nodes().length),9);
      const sampleMetrics=await p.locator('#dmCanvas').evaluate(el=>{
        const nodes=el._cyreg.cy.nodes();return {overlap:nodes.some((a,i)=>nodes.some((b,j)=>{
          if(i>=j)return false;const x=a.renderedBoundingBox(),y=b.renderedBoundingBox();return x.x1<y.x2&&x.x2>y.x1&&x.y1<y.y2&&x.y2>y.y1;
        })),clipped:nodes.some(n=>{const r=n.renderedBoundingBox();return r.x1<0||r.y1<0||r.x2>el.clientWidth||r.y2>el.clientHeight;})};
      });
      assert.equal(sampleMetrics.overlap,false);assert.equal(sampleMetrics.clipped,false);
      await p.locator('#dmTitle').evaluate(el=>el.scrollIntoView({block:'start'}));
      await p.screenshot({path:path.join(output,`entry-map-${width}.png`)});
      await p.locator('#dmGraphPanel').screenshot({path:path.join(output,`entry-diagram-${width}.png`)});
      const [source]=await Promise.all([p.waitForNavigation(),p.locator('#dmReadCenter').click()]);
      assert.equal(source.status(),200);assert((await p.locator('body').textContent()).includes('对应书籍页码引文'));
      if(width===1440){
        for(const hub of hubs.filter(h=>h.term!=='资本')){
          await p.goto(base+'/dictionary/map?center='+encodeURIComponent(hub.slug));await ready();
          await p.locator('#dmPng').waitFor({state:'visible'});
          const selected=await p.locator('#dmCanvas').evaluate(el=>el._cyreg.cy.nodes().map(n=>n.id()).sort());
          assert.equal(selected.length,11);
          const payload=await get('/api/dictionary/relations',{center:hub.slug,group:'all'});
          const preferred=payload.focus.recommended.slice(0,10);
          const chosenEdges=payload.focus.edges.filter(e=>preferred.includes(e.source)||preferred.includes(e.target));
          assert(chosenEdges.some(e=>e.source===hub.slug));assert(chosenEdges.some(e=>e.target===hub.slug));
          hub.focus={neighbours:preferred.length,themes:[...new Set(payload.focus.nodes.filter(n=>preferred.includes(n.slug)).map(n=>n.theme))],
            outgoing:chosenEdges.filter(e=>e.source===hub.slug).length,incoming:chosenEdges.filter(e=>e.target===hub.slug).length};
          await p.locator('#dmGraphPanel').screenshot({path:path.join(output,`hub-${hub.slug}.png`)});
          await p.reload();await ready();await p.locator('#dmPng').waitFor({state:'visible'});
          assert.deepEqual(await p.locator('#dmCanvas').evaluate(el=>el._cyreg.cy.nodes().map(n=>n.id()).sort()),selected);
        }
      }
      await p.goto(base+'/dictionary/map?center='+encodeURIComponent(center)+'&view=list');await ready();
      await p.locator('#dmListView').click();await p.locator('#dmInference').check();await ready();
      assert(await p.locator('[data-group^="inference_"]').count()>0);
      await p.locator('[data-group="inference_related"]').click();await ready();
      assert(await p.locator('.dm-inferred').count()>0);
      await p.goto(base+'/dictionary/map?theme='+encodeURIComponent('中国马克思主义')+'&q='+encodeURIComponent('省部级'));await ready();
      assert((await p.locator('.dm-entry h3').first().textContent()).length>40);await check();
      await p.locator('.dm-entry').first().screenshot({path:path.join(output,`long-title-${width}.png`)});
      assert.deepEqual(errors,[]);assert(!requests.some(u=>/api\/ai\/|xiaomimimo/.test(u)));
      samples.sort((a,b)=>a-b);assert(samples.at(-1)<2500);
      results.push({width,samples:5,median_reading_ms:samples[2],max_reading_ms:samples.at(-1),diagram:metrics});await ctx.close();
    }
    await api.close();
    const report={cpu:os.cpus()[0].model,browser:await browser.version(),graph:overview.version,
      scope:'local isolated fixture, immutable real graph, no model calls or production writes',themes,hubs,results};
    await fs.writeFile(path.join(output,'reading-acceptance.json'),JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report));
  }finally{if(browser)await browser.close();server.kill();}
})().catch(e=>{console.error(e);process.exitCode=1;});
