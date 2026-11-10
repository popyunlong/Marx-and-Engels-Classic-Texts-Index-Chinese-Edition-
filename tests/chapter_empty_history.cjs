const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
test('empty chapter history restores scope and version into the next reload snapshot', () => {
  const source = fs.readFileSync(path.join(__dirname, '../templates/index.html'), 'utf8');
  const fn = source.slice(source.indexOf('  function restoreSearchHistory(id)'), source.indexOf('  function updateBookTabs(payload)'));
  const snapshot = {requested_scope:['chapter:fixture.pdf#toc-1-3'],scope_version:'old-version',scope_label:'指定篇章'};
  let saved, restored;
  const context = {
    q:{}, coocCheckbox:{}, loadSearchHistory:() => [{id:'empty',kind:'standard',query:'无此原文',empty:true,payload:snapshot}],
    invalidateSearchRequest:()=>{}, applySearchMode:()=>{}, restoreSearchScope:(tokens, version)=>{restored={tokens,version};},
    renderEmpty:()=>{},updateBookTabs:()=>{},saveSearchState:value=>{saved=value;},scrollToResults:()=>{}
  };
  vm.createContext(context); vm.runInContext(fn,context); context.restoreSearchHistory('empty');
  assert.deepEqual(restored,{tokens:snapshot.requested_scope,version:'old-version'});
  assert.deepEqual(saved.requested_scope,snapshot.requested_scope);
  assert.equal(saved.scope_version,'old-version'); assert.equal(saved.mode,'empty');
});
