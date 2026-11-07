import {runOnce} from './runOnce.js';
const [mode,slot,noAlert,only]=process.argv.slice(2);
try{const r=await runOnce({mode,slot:slot==='manual'?null:Number(slot),alert:noAlert!=='1',only:only?only.split(','):null});process.exitCode=r.failedCount?1:0;}catch(e){console.error('Monitor stopped:',e.message);process.exitCode=2;}
