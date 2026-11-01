const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');

function page() {
  const nodes = {};
  for (const [, id] of fs.readFileSync(path.join(root, 'index.html'), 'utf8').matchAll(/id="([^"]+)"/g)) {
    const classes = new Set();
    nodes[id] = { style: {}, attributes: {}, listeners: {}, hidden: false,
      classList: { toggle(name, enabled) { enabled ? classes.add(name) : classes.delete(name); }, contains(name) { return classes.has(name); } },
      setAttribute(name, value) { this.attributes[name] = value; },
      addEventListener(name, callback) { this.listeners[name] = callback; }, focus() {} };
  }
  const storage = new Map();
  const context = vm.createContext({ document: { getElementById: id => nodes[id], querySelectorAll: () => [] },
    Date, AbortSignal, setTimeout() {}, setInterval() {}, Event: class {},
    localStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) } });
  const source = fs.readFileSync(path.join(root, 'duo.js'), 'utf8').replace('void tick();',
    'globalThis.testMap = { model: FSRPressureMap, renderPressure, render, showView };');
  vm.runInContext(source, context);
  return { ...context.testMap, nodes };
}

function device(values = [.1, .2, .3, .4], extra = {}) {
  return { active: true, source: 'usb', input_rate_hz: 100, latest: { pc_epoch_ms: Date.now(),
    F0_V: values[0], F1_V: values[1], F2_V: 3.3, F3_V: values[2], F4_V: values[3], F5_V: 3.3 }, ...extra };
}

test('physical layout uses F0, F1, F3, F4 and excludes unused channels', () => {
  const { model } = page();
  const result = model.reading(device(), Date.now());
  assert.deepEqual(Array.from(result.values), [.1, .2, .3, .4]);
  assert.deepEqual(Array.from(result.peaks), [4]);
});

test('zero voltage remains valid; missing and out-of-range voltages remain unavailable', () => {
  const { model } = page();
  const result = model.reading(device([0, null, -1, 3.4]), Date.now());
  assert.equal(result.valid, 1);
  assert.equal(result.values[0], 0);
  assert.equal(result.values[1], null);
  assert.equal(result.values[2], null);
  assert.equal(result.values[3], null);
  assert.ok(model.color(0, 3.3));
  assert.equal(model.color(null, 3.3), null);
});

test('stale and disconnected sensors cannot leave a live-looking heat map', () => {
  const { model, renderPressure, nodes } = page();
  const d = device();
  assert.equal(model.reading(d, d.latest.pc_epoch_ms + 1501).state, 'stale');
  renderPressure({ devices: { A: d, B: { ...d, active: false } } }, d.latest.pc_epoch_ms + 1600);
  assert.equal(nodes.voltageA0.textContent, '—');
  assert.equal(nodes.voltageB0.textContent, '—');
  assert.equal(nodes.pressureA0.style.backgroundColor, '');
  assert.match(nodes.pressureStateA.textContent, /过期/);
  assert.match(nodes.pressureStateB.textContent, /未连接/);
});

test('left and right use independent readings with the same fixed colour scale', () => {
  const { renderPressure, nodes } = page();
  renderPressure({ devices: { A: device([.1, .5, .2, .3]), B: device([.5, .1, .2, 1.2]) } });
  assert.equal(nodes.voltageA1.textContent, '0.500');
  assert.equal(nodes.voltageB0.textContent, '0.500');
  assert.equal(nodes.pressureA1.style.backgroundColor, nodes.pressureB0.style.backgroundColor);
  assert.match(nodes.pressurePeakA.textContent, /^F1 · 0.500 V$/);
  assert.match(nodes.pressurePeakB.textContent, /^F4 · 1.200 V$/);
});

test('lowering the colour limit clips only the colour, never the actual voltage', () => {
  const { render, nodes, model } = page();
  render({ devices: { A: device([2.5, .2, .3, .4]), B: device() }, elapsed_ms: 0, path: 'data', recording: false });
  nodes.pressureRange.value = '0.5';
  nodes.pressureRange.listeners.change();
  assert.equal(nodes.voltageA0.textContent, '2.500');
  assert.equal(nodes.pressureA0.style.backgroundColor, model.color(.5, .5).background);
  assert.ok(nodes.pressureA0.classList.contains('over-range'));
  assert.match(nodes.pressureNotice.textContent, /超过颜色上限/);
});

test('service failure clears both sides and demo mode is explicitly labelled', () => {
  const { renderPressure, nodes } = page();
  renderPressure({ devices: { A: device(), B: device(undefined, { source: 'demo' }) } });
  assert.match(nodes.pressureStateB.textContent, /模拟数据/);
  renderPressure(null);
  assert.equal(nodes.voltageA0.textContent, '—');
  assert.equal(nodes.voltageB4.textContent, '—');
  assert.match(nodes.pressureNotice.textContent, /本地服务未连接/);
});

test('view switching preserves access to the original monitor and recording controls', () => {
  const { showView, nodes } = page();
  assert.equal(nodes.pressureView.hidden, false);
  assert.equal(nodes.monitorView.hidden, true);
  showView('monitor');
  assert.equal(nodes.pressureView.hidden, true);
  assert.equal(nodes.monitorView.hidden, false);
  assert.equal(nodes.monitorTab.attributes['aria-selected'], 'true');
  assert.equal(nodes.pressureTab.tabIndex, -1);
  assert.ok(nodes.record.listeners.click);
});
