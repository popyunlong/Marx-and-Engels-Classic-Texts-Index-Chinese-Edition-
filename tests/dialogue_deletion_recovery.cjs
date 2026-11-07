const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
test('remote deletion preserves verified output under a different local identity',()=>{
 const source=fs.readFileSync(path.join(__dirname,'../static/ai-page/ai-page.js'),'utf8');
 const extract=(name,next)=>source.slice(source.indexOf('  function '+name+'('),source.indexOf('  function '+next+'('));
 const backups=[],removed=[],old={id:'old',messages:[]};
 const context={messages:[{role:'user',content:'q'},{role:'assistant',content:'verified',pending:true,_mid:'answer',_revision:2}],
  sessionsData:{current:'old',sessions:[old]},streaming:true,sessionRefreshPending:false,
  sessionDb:null,sessionDbFailed:false,genId:()=> 'recovery',nowTs:()=>123,ensureMessageIdentity:x=>x,
  writeEmergencyTextBackup:x=>backups.push(JSON.parse(JSON.stringify(x))),removeEmergencyBackup:id=>removed.push(id),showStorageWarning:()=>{}};
 vm.createContext(context);
 vm.runInContext(extract('storedMessages','isQuotaError')+extract('preserveDeletedAnswer','markLocalCloudExpired'),context);
 context.removeLocalConversation('old',123);
 assert.equal(context.messages[1].pending,true);
 assert.equal(context.sessionsData.current,'old');
 assert.equal(context.sessionsData.sessions.length,2);
 assert.equal(backups[0].id,'recovery');
 assert.equal(backups[0].messages[1].pending,undefined);
 context.messages[1].content='verified final';context.streaming=false;
 context.removeLocalConversation('old',123);
 assert.deepEqual(removed,['old']);
 assert.equal(context.sessionsData.sessions.length,1);
 assert.equal(context.sessionsData.sessions[0].messages[1].content,'verified final');
 assert.equal(context.sessionsData.sessions[0].id,'recovery');
});
