import path from 'node:path';
import {checks} from './checks/index.js';
import {runChecks} from './lib/runner.js';
import {hostBusy,claimRun,readJSON,writeJSON,VERSION} from './lib/control.js';
import {planAlerts,acknowledge} from './lib/alerts.js';
import {saveRun} from './lib/report.js';
import {sendAlert} from './lib/alert.js';
import {config} from './config.js';

export async function runOnce({mode='light',slot=null,alert=true,only=null}={}){
  if(process.env.MONITOR_LOCK_HELD!=='1')throw new Error('Use runAll.js: the shared flock is mandatory');
  const dir=path.resolve(process.env.MONITOR_REPORT_DIR||'reports'),stateFile=path.join(dir,'schedule-state.json');
  const schedule=readJSON(stateFile,{}),previousIncomplete=schedule.outcome==='started';
  const reason=claimRun(schedule,{mode,slot});
  if(reason){console.log(JSON.stringify({status:'skipped',reason}));return {failedCount:0,skipped:reason};}
  schedule.outcome='started';writeJSON(stateFile,schedule);
  const selected=checks.filter(c=>(mode==='daily'||!c.daily)&&(!only||only.includes(c.key)));
  let busy;try{busy=hostBusy();}catch{busy='宿主资源读取失败，安全跳过';}
  const run=busy?{version:VERSION,timestamp:new Date().toISOString(),mode,totalMs:0,results:selected.map(c=>({key:c.key,title:c.title,status:'skipped',ok:false,category:'coverage',message:busy})),network:{requests:0,peakConcurrency:0},coverage:{aiGeneration:false,paymentGateway:false}}:await runChecks(selected,{mode});
  if(previousIncomplete)run.results.push({key:'previous-run',title:'上一轮执行完整性',status:'degraded',ok:false,category:'coverage',message:'上一轮未正常结束，本轮未补跑'});
  saveRun(run,dir);schedule.outcome='finished';writeJSON(stateFile,schedule);
  if(alert&&!only){
    const file=path.join(dir,'alert-state.json');const {state,pending}=planAlerts(run.results,readJSON(file,{}),Date.now(),config.alert.repeatHours*3600000);
    writeJSON(file,state); // Failure evidence survives a failed notification.
    if(pending.length){const sent=await sendAlert('【站点巡检】'+pending.map(p=>`${p.type==='recovered'?'恢复':'异常'}:${p.title}`).join('、'),pending.map(p=>`${p.category||''} ${p.title}: ${p.message}`).join('\n'));if(sent)writeJSON(file,acknowledge(state,pending));}
  }
  console.log(JSON.stringify({version:VERSION,mode,results:run.results,network:{requests:run.network.requests,peakConcurrency:run.network.peakConcurrency}}));
  return {failedCount:run.results.filter(r=>r.status==='failed').length};
}
