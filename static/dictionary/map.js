/* Local graph interaction only: no model calls or shared user-state writes. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  if (!$('dmCanvas')) return;
  let cy, version='', center='', theme='', offset=0, limit=12, data={nodes:[],edges:[]}, requestNo=0;
  const suggestions={dmTerm:new Map(),dmTarget:new Map()};
  const suggestSeq={dmTerm:0,dmTarget:0};
  const text=(tag,value,cls)=>{const e=document.createElement(tag);e.textContent=value;if(cls)e.className=cls;return e;};
  const status=s=>{$('dmStatus').textContent=s;};
  const button=(label,fn)=>{const b=text('button',label);b.type='button';b.addEventListener('click',fn);return b;};
  const title=slug=>data.nodes.find(n=>n.slug===slug)?.title || slug;
  const params=()=>({inference:$('dmInference').checked?'1':'0',kind:$('dmKind').value,version});
  function setURL(extra={}) {
    const p=new URLSearchParams({...params(),center,theme,offset:String(offset),limit:String(limit),...extra});
    for(const [k,v] of [...p]) if(!v||v==='0')p.delete(k);
    const url='/dictionary/map?'+p;
    if(location.pathname+location.search!==url)history.pushState(null,'',url);
  }
  async function fetchData(path,p={}){
    const r=await fetch(path+'?'+new URLSearchParams(p),{credentials:'same-origin',headers:{Accept:'application/json'}});
    const payload=await r.json().catch(()=>({}));
    if(!r.ok){$('dmLatest').hidden=r.status!==409;throw new Error(payload.error||'地图暂不可用，请返回词条目录继续阅读。');}
    $('dmLatest').hidden=true;
    return payload;
  }
  function showDetail(edge){
    const box=$('dmDetail');box.replaceChildren();
    box.append(text('span',edge.layer==='evidence'?'原文依据':'AI 推断','dm-badge'));
    box.append(text('h3',title(edge.source)+' → '+title(edge.target)),text('p',edge.label+'：'+edge.explanation));
    for(const ev of edge.evidence){
      box.append(text('blockquote',ev.quote),text('p',ev.citation,'dm-muted'));
      const a=text('a','查看来源词条与段落');a.href=ev.url;box.append(a);
    }
    box.append(text('p',edge.layer==='inference'?'原句已校验；关系属于 AI 解释，可供探索，不代表人工确认。':'“正文提及”仅表示词条出现于原文，不代表概念等同或存在因果。','dm-muted'));
    if(innerWidth<=720)box.parentElement.scrollIntoView({behavior:'auto',block:'start'});
  }
  function draw(){
    if(cy){cy.destroy();cy=null;}
    if(typeof cytoscape!=='function') {status('图形组件暂不可用，请使用关系列表。');return;}
    const els=data.nodes.map(n=>({data:{id:n.slug,label:n.title.length>22?n.title.slice(0,21)+'…':n.title,center:n.slug===center?'yes':'no'}}));
    els.push(...data.edges.map(e=>({data:{id:e.id,source:e.source,target:e.target,label:e.label,layer:e.layer}})));
    cy=cytoscape({container:$('dmCanvas'),elements:els,wheelSensitivity:.18,minZoom:.25,maxZoom:2.5,
      style:[
        {selector:'node',style:{'background-color':'#a88564',label:'data(label)',color:'#34291f','font-size':12,'text-valign':'bottom','text-margin-y':9,'text-wrap':'wrap','text-overflow-wrap':'anywhere','text-max-width':105,width:20,height:20}},
        {selector:'node[center="yes"]',style:{'background-color':'#8f1d1d',width:34,height:34,'font-weight':'bold'}},
        {selector:'edge',style:{width:1.3,'line-color':'#c7b69f','target-arrow-color':'#c7b69f','target-arrow-shape':'triangle','curve-style':'bezier'}},
        {selector:'edge[layer="inference"]',style:{'line-style':'dashed','line-color':'#6b8e93','target-arrow-color':'#6b8e93'}},
        {selector:'edge:selected',style:{width:3,'line-color':'#8f1d1d',label:'data(label)','font-size':12,'text-background-opacity':1,'text-background-color':'#fffdf9'}}
      ],layout:{name:'concentric',animate:false,padding:45,minNodeSpacing:40,concentric:n=>n.id()===center?100:1,levelWidth:()=>2}});
    cy.on('tap','node',ev=>loadCenter(ev.target.id()));
    cy.on('tap','edge',ev=>{const edge=data.edges.find(e=>e.id===ev.target.id());if(edge)showDetail(edge);});
  }
  function render(payload){
    data=payload;version=payload.version;
    $('dmThemes').replaceChildren();
    for(const group of payload.themes||[])$('dmThemes').append(button(`${group.theme} · ${group.count}`,()=>{theme=group.theme;offset=0;overview();}));
    const coverage=payload.coverage;
    $('dmCoverage').textContent=`覆盖 ${coverage.entries} 个词条 · AI 已分析 ${coverage.analyzed_entries} 个 · ${coverage.relations} 条关系 · 地图版本 ${version}`;
    $('dmEdges').replaceChildren();
    for(const edge of data.edges){
      const b=button(`${title(edge.source)} → ${title(edge.target)} · ${edge.label}${edge.layer==='inference'?'（AI 推断）':''}`,()=>showDetail(edge));
      if(edge.layer==='inference')b.className='dm-inferred';
      $('dmEdges').append(b);
    }
    if(!data.edges.length)$('dmEdges').append(text('p',center?'当前筛选下暂无可靠关系。可选择其他词条，或开启 AI 推断层。':'选择一个词条，开始探索它的关系。','dm-muted'));
    $('dmNodes').replaceChildren();
    for(const node of data.nodes){
      $('dmNodes').append(button(node.title,()=>loadCenter(node.slug)));
      const a=text('a',`阅读词条 · 第 ${node.start_page}–${node.end_page} 页`);a.href=node.url;$('dmNodes').append(a);
    }
    $('dmExpand').hidden=!center||!data.truncated||limit>=59;
    $('dmNext').hidden=!!center||data.nodes.length<60;
    $('dmListTitle').textContent=center?'当前词条与邻居':'词条';
    $('dmDetail').replaceChildren(text('p','点击连线或关系列表查看出处。'));
    draw();
  }
  async function loadCenter(slug,push=true){
    const seq=++requestNo;center=slug;theme='';offset=0;
    status('正在读取关系…');
    try{
      const payload=await fetchData('/api/dictionary/graph',{...params(),center,limit});
      if(seq!==requestNo)return;
      render(payload);const node=data.nodes.find(n=>n.slug===center);
      if(node){$('dmTerm').value=node.title;suggestions.dmTerm.set(node.title,node.slug);}
      status(`${node?.title||'词条'} · 当前显示 ${data.nodes.length} 个节点、${data.edges.length} 条关系${payload.truncated?'，可展开更多':''}`);
      if(push)setURL();
    }catch(e){if(seq===requestNo)status(e.message);}
  }
  async function overview(push=true){
    const seq=++requestNo;center='';$('dmTerm').value='';status('正在读取主题…');
    try{
      const payload=await fetchData('/api/dictionary/graph',{...params(),theme,offset});
      if(seq!==requestNo)return;
      render(payload);
      status((theme||'全部主题')+' · 选择词条后查看联系');
      if(push)setURL();
    }catch(e){if(seq===requestNo)status(e.message);}
  }
  async function suggest(id,list){
    const seq=++suggestSeq[id],q=$(id).value.trim();
    if(!q)return;
    try{
      const payload=await fetchData('/api/dictionary/suggest',{q});
      if(seq!==suggestSeq[id])return;
      $(list).replaceChildren();
      for(const n of payload.results){
        const label=`${n.title} · 第${n.start_page}页`;suggestions[id].set(label,n.slug);
        if(!suggestions[id].has(n.title))suggestions[id].set(n.title,n.slug);
        const op=document.createElement('option');op.value=label;$(list).append(op);
      }
    }catch(e){status(e.message);}
  }
  for(const [id,list] of [['dmTerm','dmTerms'],['dmTarget','dmTargets']]){
    let timer;$(id).addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(()=>suggest(id,list),180);});
  }
  $('dmSearch').addEventListener('submit',async e=>{e.preventDefault();await suggest('dmTerm','dmTerms');const slug=suggestions.dmTerm.get($('dmTerm').value.trim());if(slug){limit=12;loadCenter(slug);}else status('请从搜索提示中选择一个词条。');});
  async function findPath(target,push=true){
    if(!center){status('请先选择中心词条。');return;}
    const seq=++requestNo;status('正在查找联系路径…');
    try{
      const payload=await fetchData('/api/dictionary/path',{...params(),from:center,to:target});
      if(seq!==requestNo)return;
      render(payload);status(payload.found?`找到 ${payload.edges.length} 步联系；连线方向保留原始关系方向。`:'在当前筛选下，四步以内没有找到联系。');
      if(push)setURL({to:target});
    }catch(e){if(seq===requestNo)status(e.message);}
  }
  $('dmPath').addEventListener('submit',async e=>{e.preventDefault();await suggest('dmTarget','dmTargets');const target=suggestions.dmTarget.get($('dmTarget').value.trim());if(target)findPath(target);else status('请从搜索提示中选择目标词条。');});
  for(const id of ['dmKind','dmInference'])$(id).addEventListener('change',()=>{const to=new URLSearchParams(location.search).get('to');if(center&&to)findPath(to);else if(center)loadCenter(center);else overview();});
  $('dmHome').onclick=()=>{theme='';offset=0;overview();};
  $('dmReset').onclick=()=>{if(center)loadCenter(center);else cy?.fit(undefined,45);};
  $('dmExpand').onclick=()=>{limit=Math.min(59,limit+12);loadCenter(center);};
  $('dmNext').onclick=()=>{offset+=60;overview();};
  $('dmLatest').onclick=()=>{version='';const p=new URLSearchParams(location.search);p.delete('version');history.replaceState(null,'','/dictionary/map?'+p);restore();};
  $('dmShare').onclick=async()=>{try{await navigator.clipboard.writeText(location.href);status('地图链接已复制；访问者仍需具备辞典权限。');}catch{const inp=document.createElement('input');inp.className='dm-share-url';inp.readOnly=true;inp.value=location.href;$('dmDetail').replaceChildren(text('p','复制以下地图链接：'),inp);inp.select();}};
  function download(blob,name){const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  $('dmPng').onclick=()=>{if(cy)download(cy.png({output:'blob',bg:'#fffdf9',full:true,scale:2}),'辞典概念地图.png');};
  $('dmCsv').onclick=()=>{
    const cell=v=>'"'+String(v??'').replace(/^[=+@-]/,"'$&").replace(/"/g,'""')+'"';
    const rows=[['来源词条','目标词条','关系','依据层','说明','依据原句','出处','来源链接','地图版本']];
    for(const e of data.edges)rows.push([title(e.source),title(e.target),e.label,e.layer==='evidence'?'原文依据':'AI推断',e.explanation,e.evidence.map(x=>x.quote).join('\n'),e.evidence.map(x=>x.citation).join('\n'),e.evidence.map(x=>new URL(x.url,location.origin).href).join('\n'),version]);
    download(new Blob(['\ufeff'+rows.map(r=>r.map(cell).join(',')).join('\r\n')],{type:'text/csv;charset=utf-8'}),'辞典关系与出处.csv');
  };
  function restore(){const p=new URLSearchParams(location.search);version=p.get('version')||'';center=p.get('center')||'';theme=p.get('theme')||'';offset=Math.max(0,Number(p.get('offset'))||0);limit=Math.min(59,Math.max(1,Number(p.get('limit'))||12));$('dmInference').checked=p.get('inference')==='1';$('dmKind').value=p.get('kind')||'';if(center&&p.get('to'))findPath(p.get('to'),false);else if(center)loadCenter(center,false);else overview(false);}
  window.addEventListener('popstate',restore);restore();
})();
