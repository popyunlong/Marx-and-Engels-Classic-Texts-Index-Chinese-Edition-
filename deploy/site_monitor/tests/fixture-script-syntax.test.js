import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

// Validate generated inline code, which parsing the outer test file cannot catch.
test('all generated fixture inline scripts compile before browser acceptance',()=>{
  const source=fs.readFileSync(new URL('./offline.test.js',import.meta.url),'utf8');
  let scripts=0;
  for(const line of source.split('\n')){
    if(!line.trim().startsWith('if(u.pathname===')||!line.includes('return html('))continue;
    const expression=line.slice(line.indexOf('return html(')+12,line.lastIndexOf(');'));
    const html=vm.runInNewContext('('+expression+')',{req:{headers:{cookie:'session=fixture'}}});
    for(const match of html.matchAll(/<script>([\s\S]*?)<\/script>/g)){new vm.Script(match[1]);scripts++;}
  }
  assert.equal(scripts,3,'AI, search and chapter fixture scripts must all be covered');
});
