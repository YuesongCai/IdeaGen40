// Run with: node --test tests/test_dash_tour.cjs
// Execute the shipped tour lifecycle with a fake clock, DOM and browser storage.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../web/dash.html'), 'utf8');
function fn(name) {
  const start = html.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `Missing ${name}`);
  return html.slice(start, html.indexOf('\n}', start) + 2);
}
function harness({hash = '', storage = {}, readFails = false, writeFails = false} = {}) {
  let next = 0;
  const timers = new Map();
  const root = {innerHTML: ''};
  const s = {
    location: {hash}, current: 'overview', S: {},
    TOURS: {overview: [{}], method: [{}], perf: [{}]}, TOUR: [],
    drawerStack: [], $: () => root,
    localStorage: {
      getItem(k) { if (readFails) throw Error('storage unavailable'); return storage[k] || null; },
      setItem(k,v) { if (writeFails) throw Error('storage unavailable'); storage[k] = v; }
    },
    setTimeout(f) { timers.set(++next, f); return next; },
    clearTimeout(id) { timers.delete(id); },
    setInterval() { return 999; }, clearInterval() {},
    closeDrawers() { s.drawerStack = []; }, tourPlace() {},
    starts: [], tourGo() { s.starts.push(s.TOUR_PAGE); }
  };
  vm.createContext(s);
  const declarations = html.slice(html.indexOf('var TOUR_KEY_PREFIX='), html.indexOf('function tourBubble(){'));
  vm.runInContext(declarations + '\n' + ['startTour','tourEnd','maybeStartTour'].map(fn).join('\n'), s);
  return {s, storage, timers, flush() {
    const jobs = [...timers.values()]; timers.clear(); jobs.forEach(f => f());
  }};
}
test('deep link remains quiet through repeated polling renders', () => {
  const h = harness({hash: '#method'}); h.s.current = 'method';
  for (let i=0;i<5;i++) { h.s.maybeStartTour(); h.flush(); }
  assert.equal(h.s.starts.length, 0);
});
test('first visit schedules once; closing suppresses all pages and reloads', () => {
  const h = harness();
  for (let i=0;i<5;i++) h.s.maybeStartTour();
  assert.equal(h.timers.size, 1); h.flush();
  assert.deepEqual(h.s.starts, ['overview']); h.s.tourEnd();
  h.s.current = 'method'; h.s.maybeStartTour(); h.flush();
  const reload = harness({storage: h.storage}); reload.s.maybeStartTour(); reload.flush();
  assert.equal(reload.s.starts.length, 0);
  assert.equal(h.s.starts.length, 1);
});
test('manual help remains available and cancels a pending automatic launch', () => {
  const h = harness(); h.s.maybeStartTour();
  h.s.startTour('method'); assert.equal(h.timers.size, 0);
  h.s.tourEnd(); h.flush(); h.s.startTour('perf');
  assert.deepEqual(h.s.starts, ['method', 'perf']);
});
test('queued callback rechecks a dismissal written by another tab', () => {
  const h = harness(); h.s.maybeStartTour();
  h.storage['ig40.tour.auto.seen'] = '1'; h.flush();
  assert.equal(h.s.starts.length, 0);
});
test('legacy per-page dismissal suppresses automatic tours globally', () => {
  const h = harness({storage: {'ig40.tour.v2.method': '1'}});
  h.s.maybeStartTour(); h.flush(); assert.equal(h.s.starts.length, 0);
});
test('storage failure fails quiet; failed writes still suppress within session', () => {
  const h = harness({readFails: true}); h.s.maybeStartTour(); h.flush();
  assert.equal(h.s.starts.length, 0);
  const w = harness({writeFails: true}); w.s.maybeStartTour(); w.flush(); w.s.tourEnd();
  assert.equal(w.s.autoTourSeen(), true);
  w.s.maybeStartTour(); w.flush(); assert.equal(w.s.starts.length, 1);
});
test('no data waits for first success; initial drawer skips permanently for this load', () => {
  const h = harness(); h.s.S = null; h.s.maybeStartTour();
  assert.equal(h.s.tourBooted, false);
  h.s.S = {}; h.s.drawerStack = [{}]; h.s.maybeStartTour();
  h.s.drawerStack = []; h.s.maybeStartTour(); h.flush();
  assert.equal(h.s.starts.length, 0);
});
test('switching away cancels launch and returning cannot queue another', () => {
  const h = harness(); h.s.maybeStartTour();
  h.s.cancelAutoTour(); h.s.current = 'method'; h.flush();
  h.s.current = 'overview'; h.s.maybeStartTour(); h.flush();
  assert.equal(h.s.starts.length, 0);
  assert.match(fn('switchView'), /cancelAutoTour\(\)/);
  assert.doesNotMatch(fn('switchView'), /maybeStartTour\(/);
});
test('complete inline dashboard script parses', () => {
  for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
});
