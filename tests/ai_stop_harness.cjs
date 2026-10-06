const fs=require('node:fs');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const source=fs.readFileSync('static/ai-page/ai-page.js','utf8');
const start=source.indexOf('  function submitQuestion(text) {');
const end=source.indexOf('  // ===================== 事件绑定',start);
assert.ok(start>=0&&end>start);
const requests=[];
const context={AbortController,console,messages:[],sessionsReady:true,streaming:false,currentAbort:null,currentPendingAssistant:null,
 depth:'quick',promptEl:{value:''},sessionRefreshPending:false,
 runtimeEnabled:()=>true,depthAllowed:()=>true,buildHistory:()=>[],saveMessages:()=>{},renderMessages:()=>{},scrollToLatestQuestion:()=>{},updateSendEnabled:()=>{},
 currentScope:()=>['all'],currentProvider:()=>'',currentApiModel:()=>'',currentReasoningEffort:()=>'',currentGrounding:()=>false,
 parseJsonResponse:r=>Promise.resolve(r.payload),
 apiFetch:()=>new Promise((resolve,reject)=>requests.push({resolve,reject})),
};
vm.createContext(context);
vm.runInContext(source.slice(start,end),context);
const response=content=>({headers:{get:()=> 'application/json'},payload:{ok:true,answer_markdown:content}});
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 context.submitQuestion('first');
 const first=context.currentAbort;
 context.stopStreaming();
 assert.equal(first.signal.aborted,true);
 assert.equal(context.streaming,false);
 assert.equal(context.messages.length,1);
 context.submitQuestion('second');
 requests[0].resolve(response('stale first answer'));
 await flush();
 assert.equal(context.streaming,true);
 assert.ok(!context.messages.some(m=>m.content==='stale first answer'));
 requests[1].resolve(response('current second answer'));
 await flush();
 assert.equal(context.streaming,false);
 assert.equal(context.messages.at(-1).content,'current second answer');
 context.submitQuestion('third');context.stopStreaming();context.submitQuestion('fourth');
 const aborted=new Error('stopped');aborted.name='AbortError';requests[2].reject(aborted);
 await flush();assert.equal(context.streaming,true);
 requests[3].resolve(response('fourth answer'));await flush();
 assert.equal(context.messages.at(-1).content,'fourth answer');
 assert.equal(context.streaming,false);
 console.log('AI cancellation and stale-response isolation passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
