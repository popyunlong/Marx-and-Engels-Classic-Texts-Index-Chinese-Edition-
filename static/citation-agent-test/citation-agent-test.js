(function(){
  'use strict';
  const root=document.getElementById('citationAgentTest'); if(!root)return;
  const csrf=root.dataset.csrf||'',apiBase=root.dataset.apiBase||'/api/citation-agent',hasAccess=root.dataset.access==='1';
  let job=parse(root.dataset.job,{}),jobs=parse(root.dataset.jobs,[]),tree=parse(root.dataset.scope,[]);
  let scopeCtl=null,page=1,pageSize=40,total=0,items=[],decision='pending',summary={total:0,accepted:0,pending:0,rejected:0,auto_accepted:0,unresolved:0},poll=null,reviewBound=false;
  const $=s=>document.querySelector(s);
  const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function parse(value,fallback){try{return JSON.parse(value||'')}catch(_){return fallback}}
  function withPreviewIdentity(url){
    const identity=new URLSearchParams(location.search).get('as');if(!identity)return url;
    const target=new URL(url,location.origin);if(target.origin!==location.origin)return url;
    target.searchParams.set('as',identity);return target.pathname+target.search+target.hash;
  }
  function notice(text,error){const el=$('#catNotice');if(!el)return;el.textContent=text||'';el.hidden=!text;el.classList.toggle('error',!!error);if(text)setTimeout(()=>{el.hidden=true},6500)}
  async function api(url,opt){
    opt=opt||{};opt.headers=Object.assign({'X-CSRF-Token':csrf},opt.headers||{});
    const response=await fetch(url,opt);let data={};try{data=await response.json()}catch(_){}
    if(!response.ok)throw new Error(data.error||data.message||('请求失败（'+response.status+'）'));
    return data;
  }
  const statusLabels={extracting:'正在读取论文',awaiting_sections:'等待确认章节和文库',queued:'已进入证据核验队列',matching:'正在本地核验与 Agent 补漏',review_ready:'校注结果等待审核',exporting:'正在生成输出文件',complete:'输出文件已生成',failed:'任务未完成'};
  const modeLabels={both:'插注＋校注',generate:'插注',audit:'校注'};
  function exportButtonText(){if(!job)return'生成输出';if(job.mode==='generate')return'生成浅蓝色插注 Word';if(job.mode==='audit')return'生成校注 Word 与批注 PDF';return'生成插注校注 Word 与批注 PDF'}

  function renderHistory(){
    const box=$('#catJobList');if(!box)return;
    if(!jobs.length){box.innerHTML='<div class="ca-empty">还没有校注任务。上传一篇论文即可开始。</div>';return}
    box.innerHTML=jobs.map(item=>'<a class="ca-job" href="'+esc(withPreviewIdentity(item.page_url))+'"><span><b>'+esc(item.original_filename)+'</b><small>'+esc(modeLabels[item.mode]||item.mode||'')+' · '+esc(item.recognition_depth==='direct_and_paraphrase'?'含观点依据发现':'仅直接引文')+' · '+Number(item.candidate_count||0)+' 条可操作候选</small></span><span class="ca-pill">'+esc(statusLabels[item.status]||item.status)+'</span></a>').join('');
  }

  function setupUpload(){
    const form=$('#catUploadForm');if(!form)return;
    const holder=$('#catBookScope');if(holder&&window.BookScope)scopeCtl=BookScope.mount(holder,tree,{persist:false});
    const modeSelect=$('#catModeSelect'),noteKindField=$('#catNoteKindField');
    const syncMode=()=>{if(noteKindField)noteKindField.hidden=modeSelect&&modeSelect.value==='audit'};
    if(modeSelect){modeSelect.addEventListener('change',syncMode);syncMode()}
    const file=$('#catFile'),name=$('#catFileName'),drop=$('#catFileDrop');
    file.addEventListener('change',()=>{name.textContent=file.files[0]?file.files[0].name:'选择论文 .docx'});
    ['dragenter','dragover'].forEach(type=>drop.addEventListener(type,event=>{event.preventDefault();drop.classList.add('drag')}));
    ['dragleave','drop'].forEach(type=>drop.addEventListener(type,event=>{event.preventDefault();drop.classList.remove('drag')}));
    drop.addEventListener('drop',event=>{if(event.dataTransfer.files.length){file.files=event.dataTransfer.files;file.dispatchEvent(new Event('change'))}});
    form.addEventListener('submit',async event=>{
      event.preventDefault();const chosen=file.files[0];if(!chosen){notice('请先选择 .docx 文件。',true);return}
      if(!chosen.name.toLowerCase().endsWith('.docx')){notice('目前只接收 .docx 文件。',true);return}
      if(!scopeCtl||!scopeCtl.hasSelection()){notice('请先完成“指定著作”；未选范围的任务不会默认扫描全库。',true);return}
      const button=form.querySelector('button[type=submit]');button.disabled=true;button.textContent='正在安全上传…';
      const data=new FormData(form);data.set('scope',JSON.stringify(scopeCtl?scopeCtl.getTokens():[]));
      try{const out=await api(apiBase+'/jobs',{method:'POST',body:data});location.href=withPreviewIdentity(out.job.page_url)}
      catch(error){notice(error.message,true);button.disabled=false;button.innerHTML='<span>开始读取论文</span><small>下一步确认章节与范围</small>'}
    });
  }

  function progressHtml(){
    const max=Math.max(1,Number(job.progress_total||1)),done=Math.max(0,Number(job.progress_done||0)),pct=Math.min(100,Math.round(done*100/max));
    return '<div class="ca-status"><h3>'+esc(statusLabels[job.status]||job.status)+'</h3><div class="ca-progress"><i style="width:'+pct+'%"></i></div><p>'+pct+'% · 正在本地证据链与独立 Agent 队列中处理。</p></div>';
  }
  function renderTimeline(){
    const box=$('#catTimeline');if(!box)return;
    const names=[['deterministic','确定性扫描','本地逐字与页码核验'],['agent_planning','Agent 规划','只读脱敏短片段'],['local_verification','本地复核','拒绝越界并执行工具'],['finalizing','结果分流','可操作与只读隔离']];
    const index=Math.max(0,names.findIndex(stage=>stage[0]===job.analysis_stage));
    box.innerHTML=names.map((stage,n)=>'<div class="cat-stage '+(job.status==='review_ready'||job.status==='complete'||n<index?'done':(n===index&&['queued','matching','exporting'].includes(job.status)?'active':''))+'"><b>'+stage[1]+'</b><span>'+stage[2]+'</span></div>').join('');
  }
  function renderSections(){
    const box=$('#catSectionsPanel');if(!box)return;box.hidden=false;
    const selected=new Set(job.selected_sections||[]),sections=job.sections||[];
    box.innerHTML='<div class="ca-status"><h3>冻结核验范围</h3><p>确认章节和指定著作后，Agent 不能扩大范围。</p></div><div class="ca-section-list">'+sections.map(section=>'<label class="ca-section"><input type="checkbox" value="'+esc(section.id)+'" '+(selected.has(section.id)?'checked':'')+'><span>'+esc(section.title||'未命名章节')+'</span><small>段落 '+(Number(section.start||0)+1)+'—'+(Number(section.end||0)+1)+'</small></label>').join('')+'</div><div class="ca-scope-block"><span>必选步骤：指定著作</span><div id="catAnalysisScope"></div><small>已载入上传时的选择；可在开始前调整，开始后立即冻结。</small></div><button type="button" class="ca-primary" id="catStartAnalysis">开始证据核验</button>';
    const holder=$('#catAnalysisScope');if(holder&&window.BookScope)scopeCtl=BookScope.mount(holder,tree,{persist:false,initialTokens:job.scope||[]});
    $('#catStartAnalysis').addEventListener('click',async()=>{
      const sections=[...box.querySelectorAll('.ca-section input:checked')].map(input=>input.value);if(!sections.length){notice('请至少选择一个章节。',true);return}
      const scope=scopeCtl&&scopeCtl.hasSelection()?scopeCtl.getTokens():[];
      if(!scope.length){notice('请先指定至少一部著作、卷册或个人文库资料。',true);return}
      const button=$('#catStartAnalysis');button.disabled=true;button.textContent='正在加入核验队列…';
      try{await api(apiBase+'/jobs/'+job.id+'/analyze',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sections:sections,scope:scope})});job.status='queued';job.analysis_stage='deterministic';renderJob();startPolling()}
      catch(error){notice(error.message,true);button.disabled=false;button.textContent='开始证据核验'}
    });
  }
  function optionLabel(option,index){const title=option.display_title||option.citation_title||('出处 '+(index+1));const pages=(option.printed_pages||[]).join('、');return title+(pages?' · 第'+pages+'页':'')+(option.private_source?' · 个人文库':'')}
  function evidenceHtml(value,locatorOnly){
    const html=esc(value||'没有可显示的原文上下文。');
    if(locatorOnly)return html.replaceAll('[[H]]','').replaceAll('[[/H]]','');
    return html.replaceAll('[[H]]','<mark class="cat-evidence-mark">').replaceAll('[[/H]]','</mark>');
  }
  function sourceUrl(option,fallback,locatorOnly){
    if(option.private_source||option.personal)return option.viewer_url||'';
    const pages=option.pdf_pages||[];
    if(option.source_file&&pages.length){
      const params=new URLSearchParams({mode:'reader',file:String(option.source_file),page:String(pages[0])});
      const previewIdentity=new URLSearchParams(location.search).get('as');if(previewIdentity)params.set('as',previewIdentity);
      params.set('return_to',location.pathname+location.search);
      const context=String(option.context||'');
      const matches=[...context.matchAll(/\[\[H\]\]([\s\S]*?)\[\[\/H\]\]/g)]
        .map(match=>match[1].replace(/\s+/g,' ').trim()).filter(Boolean);
      const highlight=locatorOnly?'':(matches.length?matches.join(' '):String(fallback||'').replace(/\s+/g,' ').trim());
      if(highlight)params.set('h',highlight.slice(0,320));
      if(option.section_title)params.set('section',String(option.section_title));
      if((option.printed_pages||[]).length)params.set('printed',String(option.printed_pages[0]));
      return '/viewer?'+params.toString();
    }
    return option.viewer_url||'';
  }
  const verificationLabels={full_quote:'引号内完整文字',unquoted_text:'无引号长句或局部文字',note_sentence:'脚注前整句（无外层引号）',locator_only:'仅按原注定位卷页，未完成文字比对',readonly_structure:'已核验原文；段落含复杂 Word 结构，按写回方式处理',paraphrase:'观点转述（不是直接引文）',unknown:'未标明'};
  const textMatchLabels={exact:'逐字一致',near:'近似文字',none:'文字未核验',paraphrase:'观点依据'};
  const sourceResolutionLabels={unique:'唯一出处',reference_disambiguated:'参考文献消歧',multiple:'多个转载版本',locator_only:'仅页码定位',none:'无站内出处'};
  const writebackLabels={footnote:'浅蓝色脚注插入',endnote:'浅蓝色尾注插入',comment:'正文 Word 批注',readonly:'网页只读',none:'不写回文件'};
  const reasonLabels={exact_text:'逐字核对一致',near_text_requires_review:'文字接近，仍需人工复核',paraphrase_never_auto:'属于观点转述，不会自动采用',reference_field_disambiguated:'已通过参考文献交叉核对出处',multiple_reprints:'存在多个转载或版本，需人工选择',locator_without_text_match:'仅核对到页码，尚未核实文字',proofreading_comment_only:'校注意见只写入 Word 批注，不改正文',auto_insert_hard_evidence:'证据充分，可安全自动插注',cross_reference_readonly:'尾注交叉引用无法唯一解析，仅供人工复核',unsafe_ooxml_anchor:'Word 结构复杂，不能安全自动写入',no_local_evidence:'未找到可核验的站内证据'};
  function reasonLabel(code){return reasonLabels[String(code||'')]||'其他需要人工复核的情况'}
  function badges(candidate){
    const locatorOnly=candidate.match_type==='locator'||candidate.verification_scope==='locator_only';
    const origin=locatorOnly?'<span class="ca-tag">原注页码定位</span>':(candidate.evidence_origin==='agent'?'<span class="ca-tag cat-origin">Agent 补漏</span>':(candidate.evidence_level==='exact'?'<span class="ca-tag">确定性命中</span>':'<span class="ca-tag">确定性检索</span>'));
    const level=locatorOnly?'<span class="ca-tag warn">文字未核验</span>':(candidate.evidence_level==='paraphrase'?'<span class="ca-tag cat-paraphrase">观点依据建议</span>':(candidate.evidence_level==='exact'?'<span class="ca-tag exact">逐字一致</span>':'<span class="ca-tag warn">文字需复核</span>'));
    return origin+level+(candidate.auto_selected?'<span class="ca-auto-badge">已自动采用</span>':'');
  }
  function candidateHtml(candidate,readonly){
    const options=candidate.source_options||[],chosen=Math.max(0,Math.min(Number(candidate.selected_option||0),Math.max(0,options.length-1))),option=options[chosen]||{};
    const viewpoint=candidate.evidence_level==='paraphrase',locatorOnly=candidate.match_type==='locator'||candidate.verification_scope==='locator_only',effectiveReadonly=readonly||locatorOnly;
    const select=options.length?'<select class="ca-source-select" data-field="option">'+options.map((value,index)=>'<option value="'+index+'" '+(index===chosen?'selected':'')+'>'+esc(optionLabel(value,index))+'</option>').join('')+'</select>':'';
    const viewerUrl=sourceUrl(option,candidate.paper_text,locatorOnly);
    const acceptLabel=candidate.decision==='accepted'?(viewpoint?'已作为观点依据':'已采信'):(viewpoint?'作为观点依据':'采信此条');
    const actions=effectiveReadonly?'':('<div class="ca-decision"><button type="button" class="ca-accept-button '+(candidate.decision==='accepted'?'selected':'')+'" data-action="accepted">'+acceptLabel+'</button><button type="button" class="ca-reject-button '+(candidate.decision==='rejected'?'selected':'')+'" data-action="rejected">'+(candidate.decision==='rejected'?'已弃用':'弃用此条')+'</button>'+(candidate.decision!=='pending'?'<button type="button" data-action="pending">重新审核</button>':'')+'</div>');
    const viewpointNotice=viewpoint?'<div class="ca-rule-note"><b>这不是引文命中</b><span>仅表示该站内文献可能支持论文观点；不得用它声称论文语句出自该处。</span></div>':'';
    const scope=candidate.verification_scope||(locatorOnly?'locator_only':'unknown'),scopeNotice='<div class="ca-rule-note"><b>核验范围</b><span>'+esc(verificationLabels[scope]||verificationLabels.unknown)+'</span></div>';
    const resultNotice='<div class="ca-rule-note"><b>文字与来源</b><span>'+esc(textMatchLabels[candidate.text_match_level]||'文字未核验')+'；'+esc(sourceResolutionLabels[candidate.source_resolution]||'无站内出处')+'；'+esc(writebackLabels[candidate.writeback_mode]||'不写回文件')+'</span></div>';
    const reasonNotice=(candidate.reason_codes||[]).length?'<div class="ca-rule-note"><b>审核原因</b><span>'+esc((candidate.reason_codes||[]).map(reasonLabel).join('；'))+'</span></div>':'';
    const editableEvidence='<dt>站内证据</dt><dd class="ca-source">'+select+'<div class="cat-evidence-context">'+evidenceHtml(option.context,false)+'</div><a class="ca-source-link" href="'+esc(viewerUrl)+'" '+(viewerUrl?'':'hidden')+'>打开原文 ↗</a></dd><dt>'+(viewpoint?'建议依据注':'建议注释')+'</dt><dd class="cat-citation-editor"><textarea class="ca-citation-edit cat-citation-edit" data-field="citation" rows="3" aria-label="'+(viewpoint?'建议依据注':'建议注释')+'">'+esc(candidate.proposed_citation||'')+'</textarea><small>可在采信前修改；内容会随卡片决定一并保存。</small></dd>';
    const locatorEvidence=locatorOnly&&options.length?'<dt>原注所指页面</dt><dd class="ca-source"><div>'+esc(optionLabel(option,chosen))+'</div><div class="cat-evidence-context">'+evidenceHtml(option.context,true)+'</div><a class="ca-source-link" href="'+esc(viewerUrl)+'" '+(viewerUrl?'':'hidden')+'>打开原注所指页面 ↗</a></dd>':'';
    const readonlyEvidence=!locatorOnly&&options.length?'<dt>只读站内证据</dt><dd class="ca-source"><div>'+esc(optionLabel(option,chosen))+'</div><div class="cat-evidence-context">'+evidenceHtml(option.context,false)+'</div><a class="ca-source-link" href="'+esc(viewerUrl)+'" '+(viewerUrl?'':'hidden')+'>打开原文 ↗</a></dd>':'';
    return '<article class="ca-candidate '+(effectiveReadonly?'cat-readonly':esc(candidate.decision))+'" data-id="'+candidate.id+'"><div class="ca-cand-head"><strong>'+esc(candidate.issue_label||candidate.issue_code)+'</strong>'+badges(candidate)+actions+'</div>'+viewpointNotice+scopeNotice+resultNotice+reasonNotice+'<dl><dt>实际核对文字</dt><dd class="ca-quote">'+esc(candidate.paper_text||'（无）')+'</dd>'+(candidate.existing_note_text?'<dt>原注</dt><dd>'+esc(candidate.existing_note_text)+'</dd>':'')+(effectiveReadonly?(locatorEvidence||readonlyEvidence):editableEvidence)+'</dl></article>';
  }
  async function loadCandidates(){
    const query=new URLSearchParams({page:String(page),page_size:String(pageSize),bucket:'actionable'});if(decision)query.set('decision',decision);
    try{const data=await api(apiBase+'/jobs/'+job.id+'/candidates?'+query);items=data.items||[];total=Number(data.total||0);summary=Object.assign(summary,data.summary||{});renderCandidates()}
    catch(error){notice(error.message,true)}
  }
  function renderCandidates(){
    const box=$('#catCandidateList');if(!box)return;box.innerHTML=items.length?items.map(item=>candidateHtml(item,false)).join(''):'<div class="ca-empty">当前状态下没有可操作候选。</div>';
    box.querySelectorAll('[data-action]').forEach(button=>button.addEventListener('click',()=>decide(button).catch(error=>notice(error.message,true))));
    box.querySelectorAll('[data-field=option]').forEach(select=>select.addEventListener('change',()=>{
      const item=items.find(value=>String(value.id)===select.closest('.ca-candidate').dataset.id),option=(item.source_options||[])[Number(select.value)]||{},citations=option.citations||{};
      const card=select.closest('.ca-candidate'),link=card.querySelector('.ca-source-link'),url=sourceUrl(option,item.paper_text,false);
      card.querySelector('[data-field=citation]').value=citations[job.resolved_style||job.citation_style]||citations.gb2025||citations.gb2015||option.citation||'';
      card.querySelector('.cat-evidence-context').innerHTML=evidenceHtml(option.context,false);
      link.href=url;link.hidden=!url;
    }));
    box.querySelectorAll('.cat-citation-edit').forEach(textarea=>{
      const resize=()=>{textarea.style.height='auto';textarea.style.height=Math.min(260,Math.max(104,textarea.scrollHeight+2))+'px'};
      textarea.addEventListener('input',resize);resize();
    });
    const pages=Math.max(1,Math.ceil(total/pageSize));$('#catPage').textContent='第 '+page+' / '+pages+' 页';$('#catPrev').disabled=page<=1;$('#catNext').disabled=page>=pages;renderSummary();
  }
  function renderSummary(){
    const values={catAgentVerified:job.agent_verified_count,catViewpointSuggestions:job.viewpoint_suggestion_count,catPending:summary.pending,catUnresolved:job.unresolved_count,catOutScope:job.out_of_scope_count,catNoEvidence:job.skipped_no_evidence_count,catPendingTab:summary.pending,catAcceptedTab:summary.accepted,catRejectedTab:summary.rejected,catAllTab:summary.total,catUnresolvedSummary:job.unresolved_count};
    Object.keys(values).forEach(id=>{const element=document.getElementById(id);if(element)element.textContent=Number(values[id]||0)});
    document.querySelectorAll('[data-cat-decision]').forEach(button=>button.classList.toggle('active',button.dataset.catDecision===decision));
    $('#catPendingActions').hidden=decision!=='pending'||Number(summary.pending||0)===0;
    const warning=$('#catAgentWarning');if(warning){
      if(job.agent_status==='budget_exhausted')warning.innerHTML='<div class="ca-warning"><b>深度补漏未完全覆盖</b><p>已达到时间或工具预算，未处理完的项目没有被伪装成完成。</p></div>';
      else if(job.agent_status==='degraded')warning.innerHTML='<div class="ca-warning"><b>Agent 已安全降级</b><p>模型、队列或结构校验未完成；确定性结果仍可正常审核和导出。</p></div>';
      else if(job.agent_status==='not_run')warning.innerHTML='<div class="ca-rule-note"><b>Agent 当前未运行</b><span>本次任务只包含确定性核验结果。</span></div>';
      else warning.innerHTML='';
    }
    const exportButton=$('#catExport');if(exportButton)exportButton.textContent=exportButtonText();
  }
  function payload(card,action){return {id:Number(card.dataset.id),decision:action,selected_option:Number((card.querySelector('[data-field=option]')||{}).value||0),proposed_citation:(card.querySelector('[data-field=citation]')||{}).value||''}}
  async function decide(button){const card=button.closest('.ca-candidate');card.querySelectorAll('button').forEach(item=>item.disabled=true);await api(apiBase+'/jobs/'+job.id+'/decisions',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({decisions:[payload(card,button.dataset.action)]})});await loadCandidates()}
  async function decidePending(value){const out=await api(apiBase+'/jobs/'+job.id+'/decisions/pending',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({decision:value})});notice('已处理 '+Number(out.updated||0)+' 条可操作候选。');page=1;await loadCandidates()}
  async function loadUnresolved(){
    const box=$('#catUnresolvedList');if(!box||box.dataset.loaded==='1')return;
    try{const data=await api(apiBase+'/jobs/'+job.id+'/candidates?bucket=unresolved&page_size=200');box.dataset.loaded='1';box.innerHTML=(data.items||[]).length?(data.items||[]).map(item=>candidateHtml(item,true)).join(''):'<div class="ca-empty">没有未能核实项。</div>'}
    catch(error){notice(error.message,true)}
  }
  function setupReview(){
    $('#catReviewPanel').hidden=false;if(!reviewBound){reviewBound=true;
      document.querySelectorAll('[data-cat-decision]').forEach(button=>button.addEventListener('click',()=>{decision=button.dataset.catDecision||'';page=1;loadCandidates()}));
      $('#catPrev').addEventListener('click',()=>{if(page>1){page--;loadCandidates()}});$('#catNext').addEventListener('click',()=>{if(page*pageSize<total){page++;loadCandidates()}});
      $('#catAcceptPending').addEventListener('click',()=>decidePending('accepted').catch(error=>notice(error.message,true)));$('#catRejectPending').addEventListener('click',()=>decidePending('rejected').catch(error=>notice(error.message,true)));
      $('#catUnresolvedDetails').addEventListener('toggle',event=>{if(event.currentTarget.open)loadUnresolved()});
      $('#catExport').addEventListener('click',async()=>{const button=$('#catExport');button.disabled=true;try{await api(apiBase+'/jobs/'+job.id+'/export',{method:'POST'});job.status='exporting';renderJob();startPolling()}catch(error){notice(error.message,true);button.disabled=false}});
    }loadCandidates();
  }
  function renderDownloads(){const box=$('#catDownloads');box.hidden=false;box.className='ca-downloads';const wordText=job.mode==='generate'?'新增上标、标号和注文均为浅蓝色':(job.mode==='audit'?'原脚注未改动，意见写入正文批注':'同一副本含浅蓝色插注和正文校注批注');const counts='自动插注 '+Number(job.inserted_count||0)+' 条，未插入 '+Number(job.not_inserted_count||0)+' 条；正文批注 '+Number(job.commented_count||0)+' 条，只读 '+Number(job.readonly_count||0)+' 条。';const pdfFailed=['failed','position_failed'].includes(job.pdf_export_status),pdfConverting=job.pdf_export_status==='converting',heading=pdfFailed?'Word 已完成，PDF 未完成':(pdfConverting?'Word 已完成，PDF 正在生成':'输出文件已生成');let html='<div style="width:100%"><h3>'+heading+'</h3><p>只读清单没有进入这些文件；上传原稿保持不变。'+counts+'</p>'+(job.error?'<p class="ca-warning">'+esc(job.error)+'</p>':'')+'</div>'+(job.docx_url?'<a class="ca-download" href="'+esc(withPreviewIdentity(job.docx_url))+'"><b>下载 Word</b><span>'+wordText+'</span></a>':'')+(job.pdf_url?'<a class="ca-download" href="'+esc(withPreviewIdentity(job.pdf_url))+'"><b>下载批注 PDF</b><span>从最终 Word 转换，批注已精确定位</span></a>':'');if(pdfConverting)html+='<div class="ca-rule-note" style="width:100%">PDF 正在从已生成的最终 Word 单独重建。</div>';if(job.docx_url&&job.mode!=='generate'&&pdfFailed)html+='<button type="button" id="catRetryPdf">单独重试 PDF</button>';box.innerHTML=html;const retry=$('#catRetryPdf');if(retry)retry.addEventListener('click',async()=>{retry.disabled=true;try{const out=await api(apiBase+'/jobs/'+job.id+'/retry-pdf',{method:'POST'});job=out.job||job;job.status='exporting';job.pdf_export_status='converting';renderJob();startPolling()}catch(error){notice(error.message,true);retry.disabled=false}})}
  function renderJob(){
    const status=$('#catStatusPanel'),sections=$('#catSectionsPanel'),review=$('#catReviewPanel'),downloads=$('#catDownloads');if(!status)return;sections.hidden=true;review.hidden=true;downloads.hidden=true;renderTimeline();
    if(['extracting','queued','matching','exporting'].includes(job.status)){status.innerHTML=progressHtml();if(job.status==='exporting'&&job.word_export_status==='ready')renderDownloads()}
    else if(job.status==='awaiting_sections'){status.innerHTML='';renderSections()}
    else if(job.status==='review_ready'){status.innerHTML='';setupReview()}
    else if(job.status==='complete'){status.innerHTML='';renderDownloads()}
    else if(job.status==='failed')status.innerHTML='<div class="ca-errorbox"><b>任务未完成</b><p>'+esc(job.error||'处理时发生错误。')+'</p></div>';
    else status.innerHTML='<div class="ca-status"><h3>'+esc(statusLabels[job.status]||job.status)+'</h3></div>';
  }
  async function refresh(){try{const out=await api(apiBase+'/jobs/'+job.id);job=out.job;renderJob();if(!['extracting','queued','matching','exporting'].includes(job.status)){clearInterval(poll);poll=null}}catch(error){clearInterval(poll);poll=null;notice(error.message,true)}}
  function startPolling(){if(!poll)poll=setInterval(refresh,1800)}
  function setupJob(){renderJob();if(['extracting','queued','matching','exporting'].includes(job.status))startPolling();$('#catDeleteJob').addEventListener('click',async()=>{if(!confirm('确定删除这个任务及其文件吗？'))return;try{await api(apiBase+'/jobs/'+job.id,{method:'DELETE'});location.href=withPreviewIdentity('/citation-agent')}catch(error){notice(error.message,true)}})}
  if(job&&job.id)setupJob();else{if(hasAccess)setupUpload();renderHistory()}
})();
