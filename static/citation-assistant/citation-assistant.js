(function(){
  'use strict';
  const root=document.getElementById('citationAssistant'); if(!root)return;
  const csrf=root.dataset.csrf||'';
  const citationAccess=root.dataset.access==='1';
  let job=parse(root.dataset.job,{}), jobs=parse(root.dataset.jobs,[]), tree=parse(root.dataset.scope,[]),gb2025Approved=root.dataset.gb2025Approved==='1';
  const citationStyleGroups=parse(root.dataset.citationStyleGroups,[]);
  let scopeCtl=null,page=1,pageSize=40,total=0,items=[],pollTimer=null,reviewDecision='pending';
  let summary={total:0,accepted:0,pending:0,rejected:0,auto_accepted:0};
  const $=s=>document.querySelector(s);
  const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function parse(v,f){try{return JSON.parse(v||'')}catch(_){return f}}
  function notice(text,error){const el=$('#caNotice');if(!el)return;el.textContent=text||'';el.hidden=!text;el.classList.toggle('error',!!error);if(text)setTimeout(()=>{el.hidden=true},6000)}
  async function api(url,opt){
    opt=opt||{};opt.headers=Object.assign({'X-CSRF-Token':csrf},opt.headers||{});
    const res=await fetch(url,opt);let data=null;try{data=await res.json()}catch(_){data={}}
    if(!res.ok){let msg=data.error||data.message||'';if(!msg){const text=(await res.text().catch(()=>''));msg=text.replace(/<[^>]+>/g,' ').replace(/\s+/g,' ').trim()}throw new Error(msg||('请求失败（'+res.status+'）'))}
    return data;
  }
  const statusLabels={extracting:'正在安全读取文档',awaiting_sections:'等待确认扫描章节',queued:'已进入分析队列',matching:'正在与原著逐字核对',review_ready:'等待你审核候选内容',exporting:'正在制作输出文件',complete:'输出文件已生成',failed:'任务未完成',expired:'已过期',deleted:'已删除'};
  const modeLabels={both:'插注＋校注',generate:'插注',audit:'校注'};
  function exportButtonText(){if(!job)return'生成输出文件';if(job.mode==='generate')return'生成浅蓝色插注 Word';if(job.mode==='audit')return'生成校注 Word 与批注 PDF';return'生成插注校注 Word 与批注 PDF'}

  function renderHistory(){
    const box=$('#caJobList');if(!box)return;
    if(!jobs.length){box.innerHTML='<div class="ca-empty">'+(citationAccess?'还没有任务。上传一篇论文后，它会显示在这里。':'会员的近期校注任务会显示在这里。')+'</div>';return}
    box.innerHTML=jobs.map(j=>'<a class="ca-job" href="'+esc(j.page_url)+'"><span><b>'+esc(j.original_filename)+'</b><small>'+esc(modeLabels[j.mode]||j.mode||'')+' · '+esc((j.created_at||'').replace('T',' ').slice(0,16))+' · '+Number(j.candidate_count||0)+' 条候选</small></span><span class="ca-pill">'+esc(statusLabels[j.status]||j.status)+'</span></a>').join('');
  }

  function setupUpload(){
    const form=$('#caUploadForm');if(!form)return;
    const holder=$('#caBookScope');if(holder&&window.BookScope)scopeCtl=BookScope.mount(holder,tree,{persist:false});
    const modeSelect=$('#caModeSelect'),noteKindField=$('#caNoteKindField');
    const syncMode=()=>{if(noteKindField)noteKindField.hidden=modeSelect&&modeSelect.value==='audit'};
    if(modeSelect){modeSelect.addEventListener('change',syncMode);syncMode()}
    const file=$('#caFile'),name=$('#caFileName'),drop=$('#caFileDrop');
    file.addEventListener('change',()=>{name.textContent=file.files[0]?file.files[0].name:'选择 .docx 论文'});
    ['dragenter','dragover'].forEach(evt=>drop.addEventListener(evt,e=>{e.preventDefault();drop.classList.add('drag')}));
    ['dragleave','drop'].forEach(evt=>drop.addEventListener(evt,e=>{e.preventDefault();drop.classList.remove('drag')}));
    drop.addEventListener('drop',e=>{if(e.dataTransfer.files.length){file.files=e.dataTransfer.files;file.dispatchEvent(new Event('change'))}});
    form.addEventListener('submit',async e=>{
      e.preventDefault();const chosen=file.files[0];if(!chosen){notice('请先选择 .docx 文件。',true);return}
      if(!chosen.name.toLowerCase().endsWith('.docx')){notice(chosen.name.toLowerCase().endsWith('.doc')?'旧 .doc 请先在 Word/WPS 中另存为 .docx。':'目前只接收 .docx。',true);return}
      const button=form.querySelector('button[type=submit]');button.disabled=true;button.textContent='正在安全上传…';
      const data=new FormData(form);data.set('scope',JSON.stringify(scopeCtl?scopeCtl.getTokens():[]));
      try{const out=await api('/api/citation-assistant/jobs',{method:'POST',body:data});location.href=out.job.page_url}
      catch(err){notice(err.message,true);button.disabled=false;button.textContent='上传并读取章节'}
    });
  }

  function progressHtml(j){const total=Math.max(1,Number(j.progress_total||1)),done=Math.max(0,Number(j.progress_done||0)),pct=Math.min(100,Math.round(done*100/total));return '<div class="ca-status"><h3>'+esc(statusLabels[j.status]||j.status)+'</h3><div class="ca-progress"><i style="width:'+pct+'%"></i></div><p>'+pct+'% · 文件会在后台处理，你可以稍后再回来。</p></div>'}
  function renderSections(){
    const box=$('#caSectionsPanel');if(!box)return;box.hidden=false;
    const selected=new Set(job.selected_sections||[]),sections=job.sections||[];
    const inherited=(job.scope||[]).length?'已指定 '+(job.scope||[]).length+' 项范围；如不重新选择将仅扫描这些书库。':'未指定著作，将扫描站内全部公开书库。';
    box.innerHTML='<div class="ca-status"><h3>请选择实际需要扫描的章节</h3><p>系统已默认排除文档开头、目录和参考文献；请在开始分析前核对一次。</p></div><div class="ca-section-list">'+sections.map(s=>'<label class="ca-section"><input type="checkbox" value="'+esc(s.id)+'" '+(selected.has(s.id)?'checked':'')+'><span>'+esc(s.title||'未命名章节')+'</span><small>段落 '+(Number(s.start||0)+1)+'—'+(Number(s.end||0)+1)+'</small></label>').join('')+'</div><div class="ca-scope-block"><span>可选：限定著作 / 卷</span><div id="caAnalysisScope"></div><small>'+inherited+' 只有明确知道出处时才建议限定。</small></div><button type="button" class="ca-primary" id="caStartAnalysis">开始逐字核对</button>';
    const holder=$('#caAnalysisScope');if(holder&&window.BookScope)scopeCtl=BookScope.mount(holder,tree,{persist:false});
    $('#caStartAnalysis').addEventListener('click',async()=>{
      const ids=[...box.querySelectorAll('.ca-section input:checked')].map(x=>x.value);if(!ids.length){notice('请至少选择一个章节。',true);return}
      const btn=$('#caStartAnalysis');btn.disabled=true;btn.textContent='正在加入队列…';
      const scope=scopeCtl&&scopeCtl.hasSelection()?scopeCtl.getTokens():(job.scope||[]);
      try{await api('/api/citation-assistant/jobs/'+job.id+'/analyze',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sections:ids,scope:scope})});job.status='queued';renderJob();startPolling()}
      catch(err){notice(err.message,true);btn.disabled=false;btn.textContent='开始逐字核对'}
    });
  }

  async function loadCandidates(){
    const q=new URLSearchParams({page:String(page),page_size:String(pageSize)});const k=$('#caFilterKind'),s=$('#caFilterSection'),i=$('#caFilterIssue');if(k&&k.value)q.set('kind',k.value);if(s&&s.value)q.set('section',s.value);if(i&&i.value)q.set('issue',i.value);if(reviewDecision)q.set('decision',reviewDecision);
    try{const data=await api('/api/citation-assistant/jobs/'+job.id+'/candidates?'+q);items=data.items||[];total=Number(data.total||0);summary=Object.assign(summary,data.summary||{});renderCandidates()}
    catch(err){notice(err.message,true)}
  }
  function optionLabel(o,n){const title=o.display_title||o.citation_title||o.source_file||('出处 '+(n+1));const pp=(o.printed_pages||[]).join('、'),pdf=(o.pdf_pages||[]).join('、');return title+(pp?' · 第'+pp+'页':(pdf?' · PDF '+pdf+'页':''))+(o.private_source?' · 私有来源':'')}
  function renderCandidates(){
    const box=$('#caCandidateList');if(!box)return;$('#caCandidateCount').textContent='当前筛选 '+total+' 条';renderReviewSummary();
    if(!items.length){box.innerHTML='<div class="ca-empty">当前筛选下没有候选内容。</div>'}else box.innerHTML=items.map(c=>{
      const options=c.source_options||[],chosen=Math.max(0,Math.min(Number(c.selected_option||0),Math.max(0,options.length-1))),o=options[chosen]||{};
      const pageConfidence=o.personal?'<span class="ca-tag '+(Number(o.page_confidence||0)<.7?'warn':'')+'">页码置信度 '+Math.round(Number(o.page_confidence||0)*100)+'%</span>':'';
      const sourceSelect=options.length?'<select class="ca-source-select" data-field="option">'+options.map((x,n)=>'<option value="'+n+'" '+(n===chosen?'selected':'')+'>'+esc(optionLabel(x,n))+'</option>').join('')+'</select>':'';
      const sourceUrl=o.viewer_url||((o.source_file&&(o.pdf_pages||[]).length)?'/reader?mode=reader&file='+encodeURIComponent(o.source_file)+'&page='+encodeURIComponent(o.pdf_pages[0]):'');
      const score=Math.round(Number(c.score||0));
      const matchBadge=c.auto_selected?'<span class="ca-tag exact">逐字一致 · 100%</span>':('<span class="ca-tag '+(c.match_type==='fuzzy'?'warn':'')+'">'+(c.match_type==='fuzzy'?'近似匹配':'需审核')+' · '+score+'%</span>');
      const autoBadge=c.auto_selected?'<span class="ca-auto-badge">已自动采用</span>':'';
      const resetButton=c.decision!=='pending'?'<button type="button" class="ca-reset-decision" data-decision-action="pending">重新审核</button>':'';
      const actions='<div class="ca-decision"><button type="button" class="ca-accept-button '+(c.decision==='accepted'?'selected':'')+'" data-decision-action="accepted">'+(c.decision==='accepted'?'已采信':'采信此条')+'</button><button type="button" class="ca-reject-button '+(c.decision==='rejected'?'selected':'')+'" data-decision-action="rejected">'+(c.decision==='rejected'?'已弃用':'弃用此条')+'</button>'+resetButton+'</div>';
      return '<article class="ca-candidate '+esc(c.decision)+'" data-id="'+c.id+'"><div class="ca-cand-head"><strong>'+esc(c.issue_label||c.issue_code)+'</strong>'+matchBadge+autoBadge+pageConfidence+actions+'</div><dl><dt>论文原句</dt><dd class="ca-quote">'+esc(c.paper_text||'（无）')+'</dd>'+(c.existing_note_text?'<dt>现有注释</dt><dd>'+esc(c.existing_note_text)+'</dd>':'')+'<dt>原著出处</dt><dd class="ca-source">'+sourceSelect+'<div>'+esc(o.context||'本站没有可显示的原文上下文。')+'</div>'+(sourceUrl?'<a class="ca-source-link" target="_blank" rel="noopener" href="'+esc(sourceUrl)+'">打开原文 ↗</a>':'')+'</dd><dt>建议注释</dt><dd><textarea class="ca-citation-edit" data-field="citation">'+esc(c.proposed_citation||'')+'</textarea></dd></dl></article>'}).join('');
    box.querySelectorAll('[data-decision-action]').forEach(button=>button.addEventListener('click',()=>decideCandidate(button).catch(e=>notice(e.message,true))));
    box.querySelectorAll('[data-field=option]').forEach(sel=>sel.addEventListener('change',()=>{const c=items.find(x=>String(x.id)===sel.closest('.ca-candidate').dataset.id),o=(c.source_options||[])[Number(sel.value)]||{};const text=(o.citations||{})[job.resolved_style||job.citation_style]||(o.citations||{}).gb2025||(o.citations||{}).gb2015||o.citation||'';sel.closest('.ca-candidate').querySelector('[data-field=citation]').value=text}));
    const pages=Math.max(1,Math.ceil(total/pageSize));$('#caPageText').textContent='第 '+page+' / '+pages+' 页';$('#caPrevPage').disabled=page<=1;$('#caNextPage').disabled=page>=pages;
  }
  function renderReviewSummary(){
    const values={caAutoCount:summary.auto_accepted,caPendingCount:summary.pending,caAcceptedCount:summary.accepted,caPendingTabCount:summary.pending,caAcceptedTabCount:summary.accepted,caRejectedTabCount:summary.rejected,caAllTabCount:summary.total};Object.keys(values).forEach(id=>{const el=document.getElementById(id);if(el)el.textContent=Number(values[id]||0)});
    document.querySelectorAll('[data-review-decision]').forEach(button=>button.classList.toggle('active',button.dataset.reviewDecision===reviewDecision));
    const pendingActions=$('#caPendingActions');if(pendingActions)pendingActions.hidden=reviewDecision!=='pending'||Number(summary.pending||0)===0;
    const exportButton=$('#caExport');if(exportButton)exportButton.textContent=exportButtonText();
  }
  function decisionPayload(card,decision){return {id:Number(card.dataset.id),decision:decision,selected_option:Number((card.querySelector('[data-field=option]')||{}).value||0),proposed_citation:(card.querySelector('[data-field=citation]')||{}).value||''}}
  function collectDecisions(){return [...document.querySelectorAll('.ca-candidate')].map(card=>decisionPayload(card,card.classList.contains('accepted')?'accepted':(card.classList.contains('rejected')?'rejected':'pending')))}
  async function saveDecisions(show=true){const data=collectDecisions();if(!data.length)return;await api('/api/citation-assistant/jobs/'+job.id+'/decisions',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({decisions:data})});if(show)notice('审核结果已保存。')}
  async function decideCandidate(button){
    const card=button.closest('.ca-candidate'),decision=button.dataset.decisionAction;card.querySelectorAll('button').forEach(x=>x.disabled=true);
    await api('/api/citation-assistant/jobs/'+job.id+'/decisions',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({decisions:[decisionPayload(card,decision)]})});
    notice(decision==='accepted'?'已采信这条建议。':(decision==='rejected'?'已弃用这条建议。':'已改回待审核。'));await loadCandidates();
  }
  async function decideAllPending(decision){
    const buttons=[$('#caAcceptPending'),$('#caRejectPending')].filter(Boolean);buttons.forEach(x=>x.disabled=true);
    try{const out=await api('/api/citation-assistant/jobs/'+job.id+'/decisions/pending',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({decision:decision})});notice(decision==='accepted'?'已一键采信 '+Number(out.updated||0)+' 条待审核结果。':'已一键弃用 '+Number(out.updated||0)+' 条待审核结果。');page=1;await loadCandidates()}finally{buttons.forEach(x=>x.disabled=false)}
  }
  function setupReview(){
    $('#caReviewPanel').hidden=false;const sectionFilter=$('#caFilterSection');if(sectionFilter&&sectionFilter.options.length===1){(job.sections||[]).forEach(section=>{const option=document.createElement('option');option.value=section.id;option.textContent=section.title||'未命名章节';sectionFilter.appendChild(option)})}['#caFilterKind','#caFilterSection','#caFilterIssue'].forEach(s=>$(s).addEventListener('change',()=>{page=1;loadCandidates()}));
    $('#caPrevPage').addEventListener('click',()=>{if(page>1){page--;loadCandidates()}});$('#caNextPage').addEventListener('click',()=>{if(page*pageSize<total){page++;loadCandidates()}});
    document.querySelectorAll('[data-review-decision]').forEach(button=>button.addEventListener('click',()=>{reviewDecision=button.dataset.reviewDecision||'';page=1;loadCandidates()}));
    $('#caAcceptPending').addEventListener('click',()=>decideAllPending('accepted').catch(e=>notice(e.message,true)));
    $('#caRejectPending').addEventListener('click',()=>decideAllPending('rejected').catch(e=>notice(e.message,true)));
    $('#caExport').addEventListener('click',async()=>{const btn=$('#caExport');btn.disabled=true;try{await saveDecisions(false);await api('/api/citation-assistant/jobs/'+job.id+'/export',{method:'POST'});job.status='exporting';renderJob();startPolling()}catch(e){notice(e.message,true);btn.disabled=false}});loadCandidates();
  }
  function setupRequiredStyle(){
    const host=$('#caRequiredStyle');if(!host)return;
    const select=$('#caRequiredStyleSelect'),button=$('#caRequiredStyleSave'),exportButton=$('#caExport');
    if(exportButton)exportButton.disabled=true;
    button.addEventListener('click',async()=>{button.disabled=true;try{const out=await api('/api/citation-assistant/jobs/'+job.id+'/citation-style',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({citation_style:select.value})});job=out.job;page=1;renderJob();notice('引文格式已确认，候选注释已更新。')}catch(e){notice(e.message,true);button.disabled=false}});
  }
  function renderDownloads(){const box=$('#caDownloads');box.hidden=false;box.className='ca-downloads';const wordText=job.mode==='generate'?'新增上标、标号和注文均为浅蓝色':(job.mode==='audit'?'原脚注保持不变，校注意见位于正文批注': '同一副本含浅蓝色插注和正文校注批注');const counts='自动插注 '+Number(job.inserted_count||0)+' 条，未插入 '+Number(job.not_inserted_count||0)+' 条；正文批注 '+Number(job.commented_count||0)+' 条，只读 '+Number(job.readonly_count||0)+' 条。';const pdfFailed=['failed','position_failed'].includes(job.pdf_export_status),pdfConverting=job.pdf_export_status==='converting',heading=pdfFailed?'Word 已完成，PDF 未完成':(pdfConverting?'Word 已完成，PDF 正在生成':'已完成');let html='<div style="width:100%"><h3>'+heading+'</h3><p>请在到期前下载；上传的原始文件始终保持不变。'+counts+'</p>'+(job.error?'<p class="ca-warning">'+esc(job.error)+'</p>':'')+'</div>';if(job.docx_url)html+='<a class="ca-download" href="'+esc(job.docx_url)+'"><b>下载 '+esc(modeLabels[job.mode]||'处理')+' Word</b><span>'+wordText+'</span></a>';else html+='<div class="ca-warning" style="width:100%">当前任务尚未生成 Word 副本，请重新点击生成；如仍失败，请查看任务错误提示。</div>';if(job.pdf_url)html+='<a class="ca-download" href="'+esc(job.pdf_url)+'"><b>下载批注 PDF</b><span>从最终 Word 转换，页边批注已通过定位校验</span></a>';if(pdfConverting)html+='<div class="ca-rule-note" style="width:100%">PDF 正在从已生成的最终 Word 单独重建。</div>';if(job.docx_url&&job.mode!=='generate'&&pdfFailed)html+='<button type="button" id="caRetryPdf">单独重试 PDF</button>';box.innerHTML=html;const retry=$('#caRetryPdf');if(retry)retry.addEventListener('click',async()=>{retry.disabled=true;try{const out=await api('/api/citation-assistant/jobs/'+job.id+'/retry-pdf',{method:'POST'});job=out.job||job;job.status='exporting';job.pdf_export_status='converting';renderJob();startPolling()}catch(e){notice(e.message,true);retry.disabled=false}})}
  function renderJob(){
    const status=$('#caStatusPanel'),sections=$('#caSectionsPanel'),review=$('#caReviewPanel'),downloads=$('#caDownloads');if(!status)return;
    sections.hidden=true;review.hidden=true;downloads.hidden=true;review.innerHTML=review.innerHTML;
    if(['extracting','queued','matching','exporting'].includes(job.status)){status.innerHTML=progressHtml(job);if(job.status==='exporting'&&job.word_export_status==='ready')renderDownloads()}
    else if(job.status==='awaiting_sections'){status.innerHTML='';renderSections()}
    else if(job.status==='review_ready'){const needsStyle=job.citation_style==='auto'&&Number(job.style_confidence||0)<.8;const styleOptions=citationStyleGroups.map(g=>'<optgroup label="'+esc(g.label)+'">'+(g.styles||[]).map(s=>'<option value="'+esc(s.key)+'">'+esc(s.label)+'</option>').join('')+'</optgroup>').join('');status.innerHTML=needsStyle?'<div class="ca-warning" id="caRequiredStyle"><b>需要你选择引文格式</b><p>现有注释少于 3 条、格式混用，或主格式占比不足 80%，系统不会替你猜测。</p><select id="caRequiredStyleSelect">'+styleOptions+'</select> <button type="button" id="caRequiredStyleSave">确认格式</button></div>':'';setupReview();if(needsStyle)setupRequiredStyle()}
    else if(job.status==='complete'){status.innerHTML='';renderDownloads()}
    else if(job.status==='failed'){status.innerHTML='<div class="ca-errorbox"><b>任务未完成</b><p>'+esc(job.error||'处理时发生错误。')+'</p></div>'}
    else status.innerHTML='<div class="ca-status"><h3>'+esc(statusLabels[job.status]||job.status)+'</h3></div>';
  }
  async function refresh(){try{const out=await api('/api/citation-assistant/jobs/'+job.id);const old=job.status;job=out.job;renderJob();if(!['extracting','queued','matching','exporting'].includes(job.status)){clearInterval(pollTimer);pollTimer=null}else if(old!==job.status)renderJob()}catch(e){clearInterval(pollTimer);notice(e.message,true)}}
  function startPolling(){if(pollTimer)return;pollTimer=setInterval(refresh,1800)}
  function setupJob(){
    renderJob();if(['extracting','queued','matching','exporting'].includes(job.status))startPolling();
    const del=$('#caDeleteJob');if(del)del.addEventListener('click',async()=>{if(!confirm('确定删除这个任务及其上传、候选和导出文件吗？删除后不能恢复。'))return;del.disabled=true;try{await api('/api/citation-assistant/jobs/'+job.id,{method:'DELETE'});location.href='/citation-assistant'}catch(e){notice(e.message,true);del.disabled=false}})
  }
  if(job&&job.id)setupJob();else{if(citationAccess)setupUpload();renderHistory()}
})();
