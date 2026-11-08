// First compatibility run using the SDKs shipped in the installed clients.
// This does not automate either desktop UI or claim to test their agent loop.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const profile = process.argv[2];
const output = process.env.AGENT_API_TEST_OUTPUT;
const fixture = JSON.parse(fs.readFileSync(path.join(output, 'fixture.json'), 'utf8'));
let Client, StreamableHTTPClientTransport;
if (profile === 'workbuddy') {
  const base = 'D:/workbuddy/resources/app.asar/node_modules/@modelcontextprotocol/sdk/dist/cjs/client/';
  ({ Client } = require(base + 'index.js'));
  ({ StreamableHTTPClientTransport } = require(base + 'streamableHttp.js'));
} else if (profile === 'harness') {
  ({ Client, StreamableHTTPClientTransport } = require('C:/Users/10108/AppData/Local/Programs/DeepSeek Harness/resources/app.asar/dsh/node_modules/@modelcontextprotocol/client/dist/index.cjs'));
} else {
  throw new Error('Unknown installed client SDK');
}
const trace = [];
async function tracedFetch(input, init) {
  let method;
  try { method = JSON.parse(init?.body).method; } catch {}
  const response = await fetch(input, init);
  const event = { method, status: response.status };
  if (response.headers.get('content-type')?.includes('application/json')) {
    try {
      const value = await response.clone().json();
      event.protocol_version = value.result?.protocolVersion;
      event.supported_versions = value.result?.supportedVersions;
    } catch {}
  }
  trace.push(event);
  return response;
}
async function main() {
  const client = new Client({ name: profile + '-bundled-sdk-acceptance', version: '1.0.0' },
    profile === 'harness' ? { capabilities: {}, versionNegotiation: { mode: 'auto' } } : { capabilities: {} });
  const transport = new StreamableHTTPClientTransport(new URL(fixture.mcp_url), {
    requestInit: { headers: { Authorization: 'Bearer ' + fixture.key } }, fetch: tracedFetch });
  let locker;
  try {
    await client.connect(transport);
    const tools = await client.listTools();
    assert.deepEqual(tools.tools.map(t => t.name).sort(), ['get_api_limits', 'get_passage', 'list_sources', 'search_corpus']);
    assert.ok(tools.tools.every(t => t.annotations.readOnlyHint));
    const sources = await client.callTool({ name: 'list_sources', arguments: {} });
    assert.notEqual(sources.isError, true);
    assert.equal(sources.structuredContent.sources[0].book, '文集');
    const search = await client.callTool({ name: 'search_corpus', arguments: {
      q: '生产关系', mode: 'exact', scope: ['book:文集'], limit: 1 } });
    assert.notEqual(search.isError, true);
    const evidence = search.structuredContent.results[0];
    const passage = await client.callTool({ name: 'get_passage', arguments: { evidence_id: evidence.evidence_id } });
    assert.notEqual(passage.isError, true);
    assert.ok(passage.structuredContent.result.text.includes('生产关系'));
    const limits = await client.callTool({ name: 'get_api_limits', arguments: {} });
    assert.equal(limits.structuredContent.global_concurrency, 1);
    const code = 'import sys; from agent_api_store import Store; slot=Store(sys.argv[1]).admission(999); slot.__enter__(); print("HELD",flush=True); sys.stdin.read(); slot.__exit__(None,None,None)';
    locker = spawn(process.env.AGENT_API_MCP_PYTHON, ['-u', '-c', code, path.join(output, 'state')],
      { cwd: process.env.AGENT_API_ROOT, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
    await new Promise((resolve, reject) => {
      const deadline = setTimeout(() => reject(new Error('Slot holder startup timeout')), 5000);
      locker.once('error', error => { clearTimeout(deadline); reject(error); });
      locker.stdout.once('data', data => {
        clearTimeout(deadline);
        data.toString().includes('HELD') ? resolve() : reject(new Error('Slot holder startup failed'));
      });
    });
    const busy = await client.callTool({ name: 'list_sources', arguments: {} });
    assert.equal(busy.isError, true);
    assert.equal(busy.structuredContent.error.code, 'concurrency_limited');
    assert.equal(busy.structuredContent.error.retry_after_seconds, 2);
    const result = { status: 'passed', coverage: 'Installed SDK only; desktop UI and agent loop not covered',
      tools: tools.tools.map(t => t.name), bibliography: evidence.bibliography,
      printed_pages: evidence.printed_pages, pdf_pages: evidence.pdf_pages,
      verification: evidence.verification, busy: busy.structuredContent.error, trace };
    fs.writeFileSync(path.join(output, profile + '-sdk-summary.json'), JSON.stringify(result, null, 2));
    console.log('PASS: ' + profile + ' installed SDK discovery, structured output, passage and busy error');
  } finally {
    if (locker) {
      locker.stdin.end();
      if (locker.exitCode === null) await once(locker, 'exit');
    }
    await client.close();
  }
}
main().catch(error => {
  // No request headers, Key or complete query is written to diagnostic output.
  fs.writeFileSync(path.join(output, profile + '-sdk-summary.json'), JSON.stringify({ status: 'failed',
    error_type: error.name, message: String(error.message).replace(/marx_[A-Za-z0-9_-]+/g, '[REDACTED]'), trace }, null, 2));
  console.error(error.name + ': ' + String(error.message).replace(/marx_[A-Za-z0-9_-]+/g, '[REDACTED]'));
  process.exitCode = 1;
});
