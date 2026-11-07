import {launch} from './lib/launch.js';
const arg=n=>process.argv.find(a=>a.startsWith(n+'='))?.slice(n.length+1);
const r=await launch({mode:arg('--mode')||'light',only:arg('--only')||'',noAlert:process.argv.includes('--no-alert')});process.exitCode=r.code??2;
