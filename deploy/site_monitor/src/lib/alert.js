// 告警发送：支持 Server酱 / 通用Webhook / 邮件。渠道由 .env 的 ALERT_CHANNEL 决定。
import { config } from '../config.js';
import { createRequire } from 'node:module';

async function sendServerchan(title, text) {
  const key = config.alert.serverchanKey;
  if (!key) throw new Error('SERVERCHAN_KEY 未配置');
  const res = await fetch(`https://sctapi.ftqq.com/${key}.send`, {
    method: 'POST',
    signal: AbortSignal.timeout(10000),
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ title, desp: text }),
  });
  if (!res.ok) throw new Error(`Server酱 返回 ${res.status}`);
  const result=await res.json();
  if(Number(result.code??result.errno??-1)!==0)throw new Error('Server酱未确认投递成功');
}

async function sendWebhook(title, text) {
  const u = config.alert.webhookUrl;
  if (!u) throw new Error('WEBHOOK_URL 未配置');
  // 通用文本格式，兼容钉钉/飞书/企业微信自定义机器人
  const res = await fetch(u, {
    method: 'POST',
    signal: AbortSignal.timeout(10000),
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ msgtype: 'text', text: { content: `${title}\n${text}` } }),
  });
  if (!res.ok) throw new Error(`Webhook 返回 ${res.status}`);
  const result=await res.json().catch(()=>null);
  if(result&&((result.errcode!==undefined&&Number(result.errcode)!==0)||(result.code!==undefined&&Number(result.code)!==0)))throw new Error('Webhook未确认投递成功');
}

async function sendEmail(title, text) {
  const { host, port, user, pass, to } = config.alert.smtp;
  if (!host || !user || !to) throw new Error('SMTP 配置不完整');
  let nodemailer;
  try {
    nodemailer = createRequire(import.meta.url)('nodemailer');
  } catch {
    throw new Error('未安装 nodemailer，请运行: npm i nodemailer');
  }
  const transporter = nodemailer.createTransport({ host, port, secure: port === 465, auth: { user, pass }, connectionTimeout:10000, greetingTimeout:10000, socketTimeout:10000 });
  const result=await transporter.sendMail({ from: user, to, subject: title, text });
  if(!result.accepted?.length)throw new Error('邮件服务器未接受收件人');
}

export async function sendAlert(title, text) {
  const ch = config.alert.channel;
  if (ch === 'none' || !ch) {
    console.log('[alert] ALERT_CHANNEL=none，跳过告警发送');
    return false;
  }
  try {
    if (ch === 'serverchan') await sendServerchan(title, text);
    else if (ch === 'webhook') await sendWebhook(title, text);
    else if (ch === 'email') await sendEmail(title, text);
    else throw new Error(`未知告警渠道: ${ch}`);
    console.log(`[alert] 已通过 ${ch} 发送告警`);
    return true;
  } catch (e) {
    console.error(`[alert] 告警发送失败: ${e.message}`);
    return false;
  }
}
