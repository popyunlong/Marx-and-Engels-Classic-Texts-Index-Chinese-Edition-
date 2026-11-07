import {createRequire} from 'node:module';
import {config} from '../config.js';
import {login} from './site.js';
import {NetworkGate,VERSION,LIMITS} from './control.js';
const {chromium}=createRequire(import.meta.url)('playwright');
export function classify(message,responses=[]){
  if(responses.some(r=>r.status>=500))return 'site';
  if(/account|authenticated/.test(message))return 'account';
  if(responses.some(r=>r.status===403))return 'edge';
  if(responses.some(r=>r.status===429))return 'capacity';
  if(/locator\.|selector|waitForFunction/.test(message))return 'script';
  if(/Timeout|timeout|network/.test(message))return 'network';
  return 'script';
}
export async function runChecks(checks,{mode='light',executablePath=process.env.MONITOR_BROWSER_EXECUTABLE,limits=LIMITS[mode]}={}){
  const started=Date.now(),results=[],contexts=new Map(),accounts=new Map(),state={};let browser;
  const gate=new NetworkGate({baseUrl:config.baseUrl,mode,limits,onStop:reason=>{if(['round_deadline','request_budget','network_timeout_or_error','response_too_large'].includes(reason))for(const c of contexts.values())c.close().catch(()=>{});}});state.gate=gate;
  try{
    browser=await chromium.launch({headless:true,...(executablePath?{executablePath}:{})});
    for(const check of checks){
      if(gate.reason){results.push({key:check.key,title:check.title,status:'skipped',ok:false,category:'coverage',message:gate.reason,durationMs:0});continue;}
      const at=Date.now(),responseOffset=gate.metrics.responses.length;let outcome;
      try{
        const identity=check.account||'anonymous';let context=contexts.get(identity);
        if(!context){context=await browser.newContext({viewport:{width:1366,height:900},locale:'zh-CN',userAgent:config.userAgent,serviceWorkers:'block',acceptDownloads:false});contexts.set(identity,context);await context.route('**/*',r=>gate.handle(r));await context.routeWebSocket('**/*',r=>r.close());context.setDefaultTimeout(config.checkTimeout);context.setDefaultNavigationTimeout(config.checkTimeout);}
        const page=context.pages()[0]||await context.newPage();gate.account=identity;
        if(check.account&&!accounts.has(identity)){accounts.set(identity,false);gate.stage='login';await login(page,config.accounts[identity]);accounts.set(identity,true);}
        if(check.account&&!accounts.get(identity))throw new Error('monitor_account_prior_login_failed');
        gate.stage=check.stage||check.key;const message=await check.run(page,state);const serverError=gate.metrics.responses.slice(responseOffset).find(r=>r.status>=500);outcome=serverError?{status:'failed',ok:false,category:'site',immediate:true,message:`HTTP ${serverError.status} ${serverError.path}`}:{status:'passed',ok:true,message};
      }catch(e){
        const responses=gate.metrics.responses.slice(responseOffset),message=String(e.message).slice(0,240),http5xx=responses.find(r=>r.status>=500);
        const budget=['round_deadline','request_budget','origin_slow','response_too_large'].includes(gate.reason);
        outcome={status:budget&&!http5xx?'degraded':'failed',ok:false,message:http5xx?`HTTP ${http5xx.status} ${http5xx.path}`:message,category:classify(message,responses),immediate:!!http5xx};
        if(/Timeout|timeout/.test(message))gate.stop('check_timeout');
      }
      results.push({key:check.key,title:check.title,...outcome,durationMs:Date.now()-at});
      if(gate.reason==='origin_slow'&&outcome.ok)results[results.length-1]={...results.at(-1),status:'degraded',ok:false,category:'coverage',message:outcome.message+'；源站处理超过2秒，本轮退让'};
    }
  }catch(e){results.push({key:'monitor',title:'巡检运行环境',status:'failed',ok:false,category:'script',message:String(e.message).slice(0,240),durationMs:Date.now()-started});}
  finally{await browser?.close();gate.close();}
  return {version:VERSION,timestamp:new Date(started).toISOString(),mode,totalMs:Date.now()-started,results,network:gate.metrics,stopReason:gate.reason==='closed'?null:gate.reason,coverage:{aiGeneration:false,paymentGateway:false,fullPdfRendering:false,dailyFunctional:mode==='daily'}};
}
