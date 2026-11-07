'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../templates/index.html'), 'utf8');
const statusSource = source.slice(source.indexOf('  function searchExportStatusText(job)'),
  source.indexOf('  function downloadSearchExport(job)'));
const pollSource = source.slice(source.indexOf('  async function pollSearchExport(jobId, immediate)'),
  source.indexOf('  async function startSearchExport(format)'));

function harness(job) {
  const events = [];
  const context = {
    searchExportPollTimer: null, searchExportControls: {hidden: true},
    apiFetch: async () => ({ok: true, json: async () => ({ok: true, job})}),
    showSearchExportStatus: (text, error) => events.push({text, error}),
    setSearchExportBusy: busy => events.push({busy}),
    downloadSearchExport: () => events.push({download: true}),
    localStorage: {removeItem: () => events.push({removed: true})},
    SEARCH_EXPORT_JOB_KEY: 'test',
    clearTimeout: () => {}, setTimeout: () => {events.push({scheduled: true}); return 1;},
  };
  vm.createContext(context);
  vm.runInContext(statusSource + pollSource, context);
  return {context, events};
}

test('terminal mismatch stops polling and never promises another retry', async () => {
  const {context, events} = harness({status: 'failed', error_code: 'count_mismatch', attempts: 2,
    max_attempts: 2, retryable: false, error: 'old retry message'});
  await context.pollSearchExport('id', false);
  assert.match(events[0].text, /自动重试已结束/);
  assert.equal(events[0].error, true);
  assert.ok(events.some(e => e.busy === false));
  assert.ok(events.some(e => e.removed));
  assert.ok(!events.some(e => e.scheduled || e.download));
});

test('legacy failed jobs receive truthful terminal text', () => {
  const {context} = harness({});
  assert.match(context.searchExportStatusText({status: 'failed',
    error: '检索数据在导出期间发生变化，任务将重新核对。'}), /自动重试已结束/);
});

test('queued mismatch polls without showing a terminal error', async () => {
  const {context, events} = harness({status: 'queued', retryable: true, error_code: 'count_mismatch'});
  await context.pollSearchExport('id', false);
  assert.match(events[0].text, /已安排自动重新核对/);
  assert.equal(events[0].error, false);
  assert.ok(events.some(e => e.scheduled));
  assert.ok(!events.some(e => e.download));
});

test('source version failure shows rebuild instruction and never downloads', async () => {
  const {context, events} = harness({status: 'failed', retryable: false,
    error_code: 'source_changed', error: '检索来源版本已变更，请重新创建任务。'});
  await context.pollSearchExport('id', false);
  assert.match(events[0].text, /重新创建/);
  assert.ok(!events.some(e => e.download || e.scheduled));
});

test('completed task downloads and legacy queued task remains compatible', async () => {
  const {context, events} = harness({status: 'complete'});
  await context.pollSearchExport('id', false);
  assert.ok(events.some(e => e.download));
  assert.ok(!events.some(e => e.scheduled));
  assert.match(context.searchExportStatusText({status: 'queued'}), /等待独立工作节点/);
});
