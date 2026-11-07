import { url } from '../config.js';
export async function login(page,account){
  if(!account?.email||!account?.password)throw new Error('monitor_account_missing');
  await page.goto(url('/login'),{waitUntil:'domcontentloaded'});
  await page.locator('input[name="email"]').fill(account.email);
  await page.locator('input[type="password"]').first().fill(account.password);
  const response=page.waitForResponse(r=>r.request().method()==='POST'&&new URL(r.url()).pathname==='/login');
  response.catch(()=>{});
  await page.getByRole('button',{name:/登\s*录|登入|sign\s*in|log\s*in/i}).first().click();
  const r=await response;await r.finished();
  if(r.status()>=400)throw new Error(`monitor_account_http_${r.status()}`);
  await page.goto(url('/account'),{waitUntil:'domcontentloaded'});
  if(!(await page.locator('form[action="/logout"]').count()))throw new Error('monitor_account_not_authenticated');
}
