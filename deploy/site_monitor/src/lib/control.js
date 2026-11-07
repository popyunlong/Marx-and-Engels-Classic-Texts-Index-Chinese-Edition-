import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

export const VERSION = '2.0.0-low-impact';
export const LIMITS = Object.freeze({light:{requests:80,ms:180000},daily:{requests:120,ms:240000}});
export const sleep = ms => new Promise(r=>setTimeout(r,ms));
export function hostBusy(load=os.loadavg()[0], memText=fs.readFileSync('/proc/meminfo','utf8')) {
  const kb=Number(memText.match(/^MemAvailable:\s+(\d+)/m)?.[1]);
  return load>=4 ? '宿主负载达到4' : !kb || kb<2*1024*1024 ? '宿主可用内存不足2GiB或不可读取' : '';
}
export function localDate(ms) { return new Date(ms+8*3600000).toISOString().slice(0,10); }
// :17 / :47 slots; 03:17 replaces a light round, never catches up after restart.
export function nextSlot(now, jitter=Math.random()) {
  const base=Math.floor((now-17*60000)/1800000)*1800000+17*60000+1800000;
  const china=new Date(base+8*3600000);
  return {slot:base,at:base+Math.floor(Math.max(0,Math.min(1,jitter))*120000),mode:china.getUTCHours()===3&&china.getUTCMinutes()===17?'daily':'light'};
}
export function claimRun(state,{now=Date.now(),mode='light',slot=null}) {
  if (!['light','daily'].includes(mode)) return 'invalid_mode';
  if(state.lastAttempt && now-state.lastAttempt<28*60000) return 'frequency_limit';
  if(slot!==null && state.lastSlot>=slot) return 'duplicate_slot';
  if(mode==='daily' && state.dailyDate===localDate(now)) return 'daily_already_attempted';
  state.lastAttempt=now;if(slot!==null)state.lastSlot=slot;
  if(mode==='daily')state.dailyDate=localDate(now);
  return '';
}
export function readJSON(file,fallback={}) {try{return JSON.parse(fs.readFileSync(file,'utf8'));}catch(e){if(e.code==='ENOENT')return fallback;throw e;}}
export function writeJSON(file,value) {fs.mkdirSync(path.dirname(file),{recursive:true});fs.writeFileSync(file+'.tmp',JSON.stringify(value,null,2));fs.renameSync(file+'.tmp',file);}

export function permitted({stage,path,method,type,target}) {
  if(method==='GET' && path.startsWith('/static/') && /\.(?:css|js|mjs)$/.test(path)) return true;
  if(method==='GET' && ({site:['/'],register:['/register'],login:['/login','/account'],payment:['/pricing'],ai:['/v2/ai','/api/ai/assistant-config','/api/ai/runtime'],citation:['/'],chapter:['/v2/read','/api/library/toc-suggest'],reader:['/viewer','/page-image']}[stage]||[]).includes(path)) return path!=='/page-image'||!!target;
  return method==='POST' && ((stage==='login'&&path==='/login')||(stage==='citation'&&path==='/api/search'));
}

export class NetworkGate {
  constructor({baseUrl,mode='light',limits=LIMITS[mode],gap=300,clock=Date.now,onStop=()=>{}}) {
    this.origin=new URL(baseUrl).origin;this.limits=limits;this.gap=gap;this.clock=clock;this.onStop=onStop;
    this.start=clock();this.stage='site';this.active=0;this.next=0;this.waiters=[];this.reason='';this.cache=new Map();this.cacheBytes=0;this.once=new Set();this.target=null;
    this.metrics={requests:0,peakConcurrency:0,blocked:0,cacheHits:0,starts:[],responses:[],unavailableTiming:0};
    this.timer=setTimeout(()=>this.stop('round_deadline'),limits.ms);this.timer.unref?.();
  }
  stop(reason){if(this.reason)return;this.reason=reason;this.onStop(reason);for(const w of this.waiters.splice(0))w();}
  async acquire(){
    while(true){
      if(this.reason)throw new Error(this.reason);
      if(this.clock()-this.start>=this.limits.ms){this.stop('round_deadline');throw new Error(this.reason);}
      if(this.active>=2){await new Promise(r=>this.waiters.push(r));continue;}
      const delay=this.next-this.clock();if(delay>0){await sleep(Math.min(delay,100));continue;}
      if(this.metrics.requests>=this.limits.requests){this.stop('request_budget');throw new Error(this.reason);}
      const now=this.clock();this.next=now+this.gap;this.active++;this.metrics.requests++;this.metrics.starts.push(now);this.metrics.peakConcurrency=Math.max(this.metrics.peakConcurrency,this.active);return;
    }
  }
  release(){this.active--;for(const w of this.waiters.splice(0))w();}
  allow(url,method,type){
    const u=new URL(url);if(u.origin!==this.origin||!permitted({stage:this.stage,path:u.pathname,method,type,target:this.target}))return false;
    if(u.pathname==='/viewer'||u.pathname==='/page-image')return !!this.target&&u.searchParams.get('file')===this.target.file&&u.searchParams.get('page')===this.target.page;
    return true;
  }
  async handle(route){
    const req=route.request();let url=req.url(),method=req.method(),body=req.postData(),headers={...req.headers()};
    if(this.reason||!this.allow(url,method,req.resourceType())){this.metrics.blocked++;return route.abort('blockedbyclient').catch(()=>{});}
    const staticKey=method==='GET'&&new URL(url).pathname.startsWith('/static/')?url:null;
    if(staticKey&&this.cache.has(staticKey)){this.metrics.cacheHits++;return route.fulfill(this.cache.get(staticKey)).catch(()=>{});}
    let final;
    try{
      // Fetch redirects explicitly: browser interception alone does not cover every redirect hop.
      for(let hop=0;hop<5;hop++){
        if(!this.allow(url,method,req.resourceType()))throw new Error('redirect_not_allowed');
        const path=new URL(url).pathname;
        const onceKey=path==='/page-image'?'page-image':path==='/api/search'?'search':path==='/api/library/toc-suggest'?'chapter':method==='POST'&&path==='/login'?`login:${this.account}`:null;
        if(onceKey){if(this.once.has(onceKey)){this.metrics.blocked++;return route.abort('blockedbyclient').catch(()=>{});}this.once.add(onceKey);}
        await this.acquire();let resp;
        try{resp=await route.fetch({url,method,postData:body,headers,maxRedirects:0,maxRetries:0,timeout:Math.min(20000,this.limits.ms-(this.clock()-this.start))});}
        catch(e){this.stop('network_timeout_or_error');throw e;}
        finally{this.release();}
        const status=resp.status(),rh=resp.headers();
        const originMs=Number(rh['server-timing']?.match(/cfOrigin;dur=([\d.]+)/i)?.[1]);
        const event={path,status,originMs:Number.isFinite(originMs)?originMs:null,stage:this.stage};this.metrics.responses.push(event);
        if(!Number.isFinite(originMs))this.metrics.unavailableTiming++;
        if(status===429||status===503)this.stop(`http_${status}`);
        if(originMs>2000)this.stop('origin_slow');
        if([301,302,303,307,308].includes(status)&&rh.location){
          await resp.dispose();
          if(this.reason)throw new Error(this.reason);
          url=new URL(rh.location,url).href;
          if(status===303||([301,302].includes(status)&&method==='POST')){method='GET';body=undefined;delete headers['content-type'];delete headers['content-length'];}
          continue;
        }
        const data=await resp.body();await resp.dispose();if(data.length>8*1024*1024){this.stop('response_too_large');throw new Error(this.reason);}
        // API fetch decodes the body; do not forward encoded transfer headers.
        for(const k of ['content-encoding','content-length','transfer-encoding'])delete rh[k];
        final={status,headers:rh,body:data};break;
      }
      if(!final)throw new Error('redirect_limit');
      if(staticKey&&final.status===200&&this.cacheBytes+final.body.length<=4*1024*1024){const cached={...final,headers:{...final.headers}};delete cached.headers['set-cookie'];this.cache.set(staticKey,cached);this.cacheBytes+=final.body.length;}
      await route.fulfill(final);
    }catch(e){await route.abort('failed').catch(()=>{});if(!this.reason)this.stop(e.message);}
  }
  close(){clearTimeout(this.timer);this.stop('closed');this.cache.clear();}
}
