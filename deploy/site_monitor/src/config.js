import { createRequire } from 'node:module';
createRequire(import.meta.url)('dotenv').config({quiet:true});

const bool = (v, def = false) =>
  v === undefined ? def : ['1', 'true', 'yes', 'on'].includes(String(v).toLowerCase());

// 监控浏览器 UA：携带一个密钥子串（默认 MazhuMonitor/1.0），让被监控站点据此把监控流量
// 从「当前在线/当日访问/阅读器访问」统计与反爬审计中豁免掉。站点侧按子串匹配（大小写不敏感）。
// 可用 MONITOR_UA_TOKEN 改密钥，或用 MONITOR_USER_AGENT 完全自定义整条 UA。
const monitorUaToken = process.env.MONITOR_UA_TOKEN || 'MazhuMonitor/1.0';
const monitorUserAgent =
  process.env.MONITOR_USER_AGENT ||
  `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 ${monitorUaToken}`;

export const config = {
  baseUrl: (process.env.BASE_URL || 'https://mazhuzuojiansuo.com').replace(/\/$/, ''),
  monitorUaToken,
  userAgent: monitorUserAgent,
  accounts: {
    nonmember: {
      email: process.env.NONMEMBER_EMAIL || '',
      password: process.env.NONMEMBER_PASSWORD || '',
    },
    member: {
      email: process.env.MEMBER_EMAIL || '',
      password: process.env.MEMBER_PASSWORD || '',
    },
  },
  checkTimeout: Math.min(20000, Number(process.env.CHECK_TIMEOUT || 20000)),
  // 每项检查之间的间隔(毫秒)，对站点友好、避免短时间集中请求
  checkDelay: Number(process.env.CHECK_DELAY || 2000),
  headless: bool(process.env.HEADLESS, true),
  alert: {
    channel: (process.env.ALERT_CHANNEL || 'none').toLowerCase(),
    // 持续未恢复的故障，最多每多少小时重复提醒一次（避免刷屏/超额度）
    repeatHours: Number(process.env.ALERT_REPEAT_HOURS || 6),
    serverchanKey: process.env.SERVERCHAN_KEY || '',
    webhookUrl: process.env.WEBHOOK_URL || '',
    smtp: {
      host: process.env.SMTP_HOST || '',
      port: Number(process.env.SMTP_PORT || 465),
      user: process.env.SMTP_USER || '',
      pass: process.env.SMTP_PASS || '',
      to: process.env.ALERT_EMAIL_TO || '',
    },
  },
};

export function url(path = '/') {
  if (!path.startsWith('/')) path = '/' + path;
  return config.baseUrl + path;
}
