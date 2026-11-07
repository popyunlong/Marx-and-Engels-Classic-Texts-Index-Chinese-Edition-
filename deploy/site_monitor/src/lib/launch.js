import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';
export function launch({mode='light',slot=null,noAlert=false,only=''}={}){
  if(!['light','daily'].includes(mode))throw new Error('mode must be light or daily');
  const script=fileURLToPath(new URL('../../scripts/locked-run.sh',import.meta.url));
  return new Promise((resolve,reject)=>{const child=spawn('bash',[script,mode,slot===null?'manual':String(slot),noAlert?'1':'0',only],{stdio:'inherit'});child.once('error',reject);child.once('exit',(code,signal)=>resolve({code,signal}));});
}
