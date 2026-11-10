const {test} = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const {chromium} = require('playwright');
test('clear refreshes already-open stale directory before selecting new version', async t => {
  const browser = await chromium.launch({headless:true,...(process.env.BROWSER_CHANNEL ? {channel:process.env.BROWSER_CHANNEL} : {})});
  t.after(() => browser.close());
  const page = await browser.newPage(), errors = [];
  page.on('pageerror', e => errors.push(e.message));
  let version = 'v1', requests = 0;
  await page.route('http://scope.test/**', async route => {
    if (!route.request().url().includes('/chapters')) return route.fulfill({body:'<div id="scope"></div>',contentType:'text/html'});
    requests++;
    return route.fulfill({json:{ok:true,scope_version:version,results:[{
      title:version === 'v1' ? '旧篇名' : '新篇名', level:1, selectable:true,
      token:'chapter:fixture.pdf#toc-1-' + (version === 'v1' ? '0' : '2')
    }]}});
  });
  await page.goto('http://scope.test/');
  for (const file of ['book-scope.js','chapter-scope.js']) await page.addScriptTag({path:path.join(__dirname,'../../static/book-scope',file)});
  await page.evaluate(() => {
    window.control = BookScope.mount(document.querySelector('#scope'), [{id:'test',label:'测试',books:[{
      key:'book',label:'测试书',volumes:[],chapter_volumes:[1],chapter_sources:[{file:'fixture.pdf',volume:1}]
    }]}], {chapters:true});
  });
  await page.locator('.bscope-trigger').click();
  await page.locator('.bscope-chapter-box > summary').click();
  await page.locator('.bscope-chapter-box details > summary').click();
  await page.locator('.bscope-chapter-check').check();
  version = 'v2';
  await page.locator('.bscope-clear').click();
  await page.getByText('新篇名', {exact:true}).waitFor();
  await page.locator('.bscope-chapter-check').check();
  const state = await page.evaluate(() => ({tokens:control.getTokens(),version:control.getScopeVersion()}));
  assert.deepEqual(state,{tokens:['chapter:fixture.pdf#toc-1-2'],version:'v2'});
  assert.equal(requests,2); assert.deepEqual(errors,[]);
});
