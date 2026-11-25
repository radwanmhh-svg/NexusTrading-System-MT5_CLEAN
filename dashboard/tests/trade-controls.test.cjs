// Real mounted controls and API client against a scripted gateway; no network.
const { test, beforeEach, afterEach, after } = require('node:test');
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<body></body>', { url: 'http://fixture.invalid/' });
global.window = dom.window;
global.document = dom.window.document;
global.localStorage = dom.window.localStorage;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true });
global.IS_REACT_ACT_ENVIRONMENT = true;
const React = require('react');
const { act, createElement: h } = React;
const { createRoot } = require('react-dom/client');
const { AutoTradeToggle } = require('../src/components/trade-controls/AutoTradeToggle.tsx');
const { KillSwitch } = require('../src/components/trade-controls/KillSwitch.tsx');
const { TradeControlNotice } = require('../src/components/trade-controls/TradeControlNotice.tsx');
const { refreshControls, requestMode, requestHalt, useTradeControls: store } = require('../src/stores/tradeControls.ts');
const off = { mode: 'paper', auto_trade: false, auto_requested: false, manual_halt: false, risk_halt: false, reconcile_paused: null, entry_block: 'auto_off' };
const on = { ...off, auto_trade: true, auto_requested: true, entry_block: null };
const held = { ...off, manual_halt: true, entry_block: 'operator_halt' };
const json = (body, status = 200) => new Response(JSON.stringify(body), { status });
let root, host, handler, calls;
beforeEach(async () => {
  localStorage.clear();
  calls = [];
  handler = () => json(off);
  global.fetch = async (url, opts = {}) => {
    const call = { method: opts.method ?? 'GET', path: new URL(url).pathname, body: opts.body && JSON.parse(opts.body) };
    calls.push(call);
    return handler(call);
  };
  store.setState({ snapshot: null, busy: false, actionError: '', readError: '' });
  host = document.createElement('main'); document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root.render(h(React.Fragment, null, h(TradeControlNotice), h(AutoTradeToggle, { compact: true }), h(KillSwitch, { compact: true }))));
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); });
after(() => dom.window.close());
const button = text => [...host.querySelectorAll('button')].find(el => el.textContent.trim() === text);
const click = async text => { const b = button(text); assert.ok(b, `expected '${text}' in ${host.textContent}`); await act(async () => b.click()); };
const read = async () => act(async () => { await refreshControls(); });
async function sendPin(text) {
  const input = host.querySelector('input[type=password]');
  await act(async () => {
    Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(input, 'fixture-only');
    input.dispatchEvent(new window.Event('input', { bubbles: true }));
  });
  assert.equal(button(text).disabled, false);
  await click(text);
}

test('unverified startup does not display old persisted AUTO or LIVE', async () => {
  localStorage.setItem('nexus-dashboard', JSON.stringify({ version: 3, state: { mode: 'LIVE', autoTradeEnabled: true } }));
  handler = () => json({}, 503);
  await read();
  assert.ok(button('Unknown').disabled); assert.match(host.textContent, /UNKNOWN/);
  assert.ok(!button('Auto')); assert.ok(!button('Stop entries').disabled);
});

test('failed disable does not show Off; later reads expose actual state and retain the error', async () => {
  handler = call => call.method === 'GET' ? json(on) : json({ detail: 'offline' }, 503);
  await read(); await click('Auto');
  assert.deepEqual(calls.at(-1).body, { enabled: false });
  assert.ok(button('Unknown')); assert.equal(button('Off'), undefined);
  assert.match(host.querySelector('[role=alert]').textContent, /not confirmed.*offline/i);
  await read(); assert.ok(button('Auto')); assert.match(host.textContent, /not confirmed/i);
});

test('200 acknowledgement with conflicting readback is not reported as success', async () => {
  handler = call => call.method === 'GET' ? json(on) : json({ auto_trade: false });
  await read(); await click('Auto');
  assert.ok(button('Auto')); assert.match(host.textContent, /requested state not confirmed/i);
});

test('operator explicitly enables only the confirmed mode', async () => {
  let state = off;
  handler = call => {
    if (call.method === 'POST') { assert.deepEqual(call.body, { enabled: true, expected_mode: 'paper' }); state = on; }
    return json(state);
  };
  await read(); await click('Off'); assert.equal(calls.length, 1);
  await click('Enable'); assert.ok(button('Auto')); assert.equal(calls.length, 3);
});

test('a different mode arriving while enable confirmation is open invalidates that approval', async () => {
  await read(); await click('Off');
  handler = () => json({ ...off, mode: 'live' });
  await read(); assert.ok(button('Enable').disabled); assert.ok(!calls.some(c => c.method === 'POST'));
});

test('server denies the PIN: no local stopped state and no leaked PIN in storage', async () => {
  handler = call => call.method === 'GET' ? json(on) : json({ detail: 'PIN denied' }, 401);
  await read(); await click('Stop entries'); await sendPin('Confirm stop');
  assert.equal(button('HALTED · Release'), undefined); assert.match(host.textContent, /not confirmed.*PIN denied/i);
  assert.equal(localStorage.length, 0); assert.equal(host.querySelector('input[type=password]'), null);
});

test('confirmed halt/release uses two distinct PIN-protected endpoints; release does not enable AUTO', async () => {
  let state = on;
  handler = call => {
    if (call.method === 'POST') {
      assert.equal(call.body.pin, 'fixture-only');
      state = call.path.endsWith('/release') ? off : held;
    }
    return json(state);
  };
  await read(); await click('Stop entries'); await sendPin('Confirm stop');
  assert.ok(button('HALTED · Release')); assert.ok(button('Off').disabled);
  await click('HALTED · Release'); await sendPin('Release halt');
  assert.ok(button('Stop entries')); assert.ok(button('Off'));
  assert.deepEqual(calls.filter(c => c.method === 'POST').map(c => c.path), ['/api/system/kill-switch', '/api/system/kill-switch/release']);
});

test('a slow poll predating a confirmed stop cannot overwrite it', async () => {
  let done; let first = true;
  handler = () => { if (first) { first = false; return new Promise(r => { done = r; }); } return json(held); };
  const old = refreshControls();
  await act(async () => { assert.ok(await requestHalt(true, 'fixture-only')); });
  await act(async () => { done(json(on)); await old; });
  assert.ok(button('HALTED · Release')); assert.ok(!button('Auto'));
});

test('mode POST success followed by an invalid snapshot is unknown, not a local mode change', async () => {
  handler = c => c.method === 'POST' ? json({ mode: 'paper' }) : json({ mode: 'paper', auto_trade: true });
  await act(async () => { assert.equal(await requestMode('paper'), false); });
  assert.equal(store.getState().snapshot, null); assert.match(host.textContent, /Invalid gateway control snapshot/);
});
