import {nextSlot,sleep} from './lib/control.js';
import {launch} from './lib/launch.js';
let stopping=false;for(const sig of ['SIGTERM','SIGINT'])process.on(sig,()=>{stopping=true;});
while(!stopping){
  const next=nextSlot(Date.now());console.log(JSON.stringify({next:new Date(next.at).toISOString(),mode:next.mode}));
  while(!stopping&&Date.now()<next.at)await sleep(Math.min(1000,next.at-Date.now()));
  if(stopping)break;
  // Sleep/clock changes never cause backlogged checks to catch up.
  if(Date.now()-next.at>120000)continue;
  try{const r=await launch({mode:next.mode,slot:next.slot});if(r.code)console.error('scheduled runner exit',r);}catch(e){console.error('scheduled runner failed',e.message);}
}
