import fs from 'node:fs';
import path from 'node:path';
import {writeJSON} from './control.js';
import {statusOf} from './alerts.js';
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels={passed:'正常',failed:'失败',degraded:'降级',skipped:'跳过'};
export function renderHtml(run){return `<!doctype html><meta charset="utf-8"><title>站点巡检</title><style>body{font:16px sans-serif;max-width:1100px;margin:30px auto}td,th{padding:10px;border:1px solid #ccc}.passed{color:green}.failed{color:#b00}.degraded,.skipped{color:#855900}table{border-collapse:collapse}</style><h1>站点巡检 ${esc(run.version||'旧版')}</h1><p>${esc(run.timestamp)} · ${esc(run.mode||'legacy')} · ${run.results.every(r=>statusOf(r)==='passed')?'本次覆盖项正常':'存在失败、降级或未覆盖项'}</p><p>请求 ${run.network?.requests??'未记录'}；并发峰值 ${run.network?.peakConcurrency??'未记录'}；耗时 ${run.totalMs}ms</p><p>轻巡检不证明搜索、正文、模型生成及支付网关正常；每日功能检查也不生成AI回答、不创建订单。</p><table><tr><th>项目</th><th>状态</th><th>说明</th></tr>${run.results.map(r=>`<tr><td>${esc(r.title)}</td><td class="${statusOf(r)}">${labels[statusOf(r)]||'未知'}</td><td>${esc(r.message)}</td></tr>`).join('')}</table>`;}
export function saveRun(run,dir){fs.mkdirSync(dir,{recursive:true});fs.appendFileSync(path.join(dir,'history.jsonl'),JSON.stringify(run)+'\n');writeJSON(path.join(dir,'latest.json'),run);fs.writeFileSync(path.join(dir,'latest.html'),renderHtml(run));}
