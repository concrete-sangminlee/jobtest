const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const code = fs.readFileSync(path.join(__dirname, '../google-apps-script/Code.gs'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, 'fixtures/job_list.html'), 'utf8');

function runtime(responses) {
  const calls = [], logs = [];
  const context = vm.createContext({
    console: {log: (...args) => logs.push(args.join(' '))},
    UrlFetchApp: {fetch(url, options) {
      calls.push({url, options});
      const next = responses.shift();
      if (next instanceof Error) throw next;
      assert.ok(next, 'Unexpected extra network call');
      return {getResponseCode: () => next.code, getContentText: () => next.html};
    }},
    ContentService: {MimeType: {JSON: 'application/json'}, createTextOutput(text) {
      return {text, setMimeType(type) {this.type = type; return this;}};
    }}
  });
  vm.runInContext(code, context);
  return {context, calls, logs};
}

test('web app returns validated envelope and preserves TLS validation', () => {
  const {context, calls} = runtime([{code: 200, html}]);
  const output = context.doGet();
  const result = JSON.parse(output.text);
  assert.equal(result.ok, true);
  assert.equal(result.version, 1);
  assert.equal(result.html, html);
  assert.equal(output.type, 'application/json');
  assert.ok(Number.isFinite(Date.parse(result.fetched_at)));
  assert.equal(calls[0].options.validateHttpsCertificates, true);
});

test('primary block tries only the other official hostname', () => {
  const {context, calls} = runtime([{code: 403, html: ''}, {code: 200, html}]);
  assert.equal(context.collectListing_().ok, true);
  assert.equal(new URL(calls[1].url).hostname, 'www.hibrain.net');
});

test('blocked or empty source returns failure, not a fabricated empty success', () => {
  const {context} = runtime([{code: 403, html: ''}, {code: 200, html: '<html>Login</html>'}]);
  const result = JSON.parse(context.doGet().text);
  assert.equal(result.ok, false);
  assert.equal(result.html, undefined);
  assert.match(result.error, /HTTP 403/);
});

test('testConnection prints only counts and timestamps, never complete HTML', () => {
  const {context, logs} = runtime([{code: 200, html}]);
  const result = context.testConnection();
  assert.equal(result.ok, true);
  assert.equal(result.link_count, 6);
  assert.equal(result.html, undefined);
  assert.ok(!logs.join().includes('<ul'));
});

test('upstream errors are sanitized and cause read-only connection test to fail', () => {
  const {context} = runtime([new Error('private stack'), new Error('private stack')]);
  assert.throws(() => context.testConnection(), /Fetch exception/);
});

test('caller parameters cannot turn the web app into an arbitrary proxy', () => {
  const {context, calls} = runtime([{code: 200, html}]);
  context.doGet({parameter: {url: 'https://attacker.example'}});
  assert.equal(new URL(calls[0].url).hostname, 'hibrain.net');
});
