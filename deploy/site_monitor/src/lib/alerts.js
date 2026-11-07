export function statusOf(r){return r.status||(r.ok?'passed':'failed');}
export function planAlerts(results,old={},now=Date.now(),repeatMs=6*3600000){
  const state=old.version===2?structuredClone(old):{version:2,items:Object.fromEntries(Object.entries(old).map(([k,v])=>[k,{...v,streak:1,notified:!!v.lastAlert}]))};
  state.items||={};const pending=[];const work=[...results];
  const incomplete=results.some(r=>['skipped','degraded'].includes(statusOf(r)));
  work.push({key:'coverage',title:'监控覆盖',status:incomplete?'degraded':'passed',category:'coverage',message:incomplete?'本轮检查未完整执行':'本轮检查完整执行'});
  for(const r of work){
    const status=statusOf(r),st=state.items[r.key];
    if(status==='passed'){
      if(st?.notified){pending.push({key:r.key,type:'recovered',message:r.message,title:r.title});}
      else delete state.items[r.key];
      continue;
    }
    if(status==='skipped'||(status==='degraded'&&r.key!=='coverage'))continue;
    const item=st||{since:new Date(now).toISOString(),streak:0,notified:false};
    item.streak++;item.category=r.category;item.message=r.message;state.items[r.key]=item;
    if((r.immediate||item.streak>=2)&&(!item.lastAlert||now-Date.parse(item.lastAlert)>=repeatMs))pending.push({key:r.key,type:'failure',title:r.title,category:r.category,message:r.message});
  }
  return {state,pending};
}
export function acknowledge(state,pending,now=Date.now()){
  const next=structuredClone(state);
  for(const p of pending){if(p.type==='recovered')delete next.items[p.key];else if(next.items[p.key]){next.items[p.key].notified=true;next.items[p.key].lastAlert=new Date(now).toISOString();}}
  return next;
}
