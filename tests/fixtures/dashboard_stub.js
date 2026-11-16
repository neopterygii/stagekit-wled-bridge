// Loads the dashboard's page script against stand-in browser objects and feeds
// it status snapshots. Driven by tests/test_dashboard_js.py:
//
//   node dashboard_stub.js <script.js> <page.html> <input.json>
//
// input.json: {"snapshots": [{"name": ..., "data": {...}}], "routes": {path: body}}
// Prints {"<name>": {"<element id>": {"text", "cls", "html", "lit", "kids"}}} on stdout.
// Exits non-zero if the script throws at load, on a snapshot, on an animation
// frame, in a control's event handler, or in a promise.
//
// The stand-ins are deliberately strict. An element has only the members
// listed in `El`, so a typo such as `el.clasList` throws here as it would in a
// browser, and getElementById returns null for an id the page does not define.
'use strict';
const fs = require('fs');
const vm = require('vm');

const [scriptPath, htmlPath, inputPath] = process.argv.slice(2);
const script = fs.readFileSync(scriptPath, 'utf8');
const html = fs.readFileSync(htmlPath, 'utf8');
const input = JSON.parse(fs.readFileSync(inputPath, 'utf8'));

const byId = new Map();
const listening = [];

class ClassList {
  constructor() { this.names = new Set(); }
  add(...n) { n.forEach(x => this.names.add(x)); }
  remove(...n) { n.forEach(x => this.names.delete(x)); }
  contains(n) { return this.names.has(n); }
  toggle(n, force) {
    const on = force === undefined ? !this.names.has(n) : !!force;
    if (on) this.names.add(n); else this.names.delete(n);
    return on;
  }
}

class El {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.style = { setProperty() {} };
    this.dataset = {};
    this.classList = new ClassList();
    this.children = [];
    this.options = this.children;
    this.attrs = {};
    this.found = new Map();
    this.textContent = '';
    this.value = '50';
    this.checked = false;
    this.disabled = false;
    this.title = '';
    this.width = 300;
    this.height = 20;
    this.clientWidth = 300;
    this.clientHeight = 20;
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.lit = null;      // pixels with colour in the last putImageData
    this._html = '';
    this._id = '';
  }
  get id() { return this._id; }
  set id(v) { this._id = v; byId.set(v, this); }
  get className() { return [...this.classList.names].join(' '); }
  set className(v) { this.classList.names = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get innerHTML() { return this._html; }
  set innerHTML(v) {
    this._html = String(v);
    this.children.length = 0;
    this.found.clear();
    for (const m of this._html.matchAll(/<(\w+)[^>]*?\bid="([^"]+)"/g)) new El(m[1]).id = m[2];
  }
  get firstChild() { return this.children[0] || null; }
  get lastChild() { return this.children[this.children.length - 1] || null; }
  appendChild(c) { this.children.push(c); return c; }
  prepend(c) { this.children.unshift(c); }
  removeChild(c) {
    const i = this.children.indexOf(c);
    if (i < 0) throw new Error('removeChild: not a child');
    this.children.splice(i, 1);
    return c;
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  addEventListener(type, fn) { listening.push({ el: this, type, fn }); }
  // No selector engine: one stand-in per selector, so the code after a lookup
  // runs. A selector that should match nothing is not modelled.
  querySelector(sel) {
    if (!this.found.has(sel)) this.found.set(sel, new El('div'));
    return this.found.get(sel);
  }
  querySelectorAll(sel) { return [this.querySelector(sel)]; }
  // The 2D context: only the two calls the page makes.
  getContext() {
    const canvas = this;
    return {
      createImageData: (w, h) => ({ width: w, height: h, data: new Uint8ClampedArray(w * h * 4) }),
      putImageData(img) {
        canvas.lit = 0;
        for (let p = 0; p < img.data.length; p += 4) {
          if (img.data[p] || img.data[p + 1] || img.data[p + 2]) canvas.lit++;
        }
      },
    };
  }
  getBoundingClientRect() { return { left: 0, top: 0, width: 300, height: 20 }; }
}

for (const m of html.replace(/<script>[\s\S]*?<\/script>/g, '')
  .matchAll(/<(\w+)[^>]*?\bid="([^"]+)"/g)) new El(m[1]).id = m[2];

const root = new El('html');
const frames = [];
const sources = [];
let clock = 1000;

const sandbox = {
  console,
  Math, JSON, Date, Promise,
  performance: { now: () => clock },
  document: {
    body: root,
    documentElement: root,
    getElementById: id => byId.get(id) || null,
    createElement: tag => new El(tag),
    createElementNS: (ns, tag) => new El(tag),
    querySelector: sel => root.querySelector(sel),
    querySelectorAll: sel => root.querySelectorAll(sel),
    addEventListener: (type, fn) => listening.push({ el: root, type, fn }),
  },
  requestAnimationFrame: fn => frames.push(fn),
  setTimeout: fn => { frames.push(() => fn()); return frames.length; },
  clearTimeout: () => {},
  fetch: url => Promise.resolve({
    ok: true,
    status: 200,
    json: () => Promise.resolve(input.routes[url] || { ok: true }),
    text: () => Promise.resolve(''),
  }),
  EventSource: class { constructor(url) { this.url = url; sources.push(this); } },
};
sandbox.window = sandbox;
const ctx = vm.createContext(sandbox);

async function settle() {
  // Run what the page queued (animation frames, debounce timers), then let
  // fetch promise chains finish. A rejection there fails the process.
  for (let round = 0; round < 3; round++) {
    for (const fn of frames.splice(0)) { clock += 16; fn(clock); }
    for (let i = 0; i < 10; i++) await Promise.resolve();
  }
}

function dump() {
  const out = {};
  for (const [id, el] of byId) {
    out[id] = { text: String(el.textContent), cls: el.className, html: el.innerHTML, lit: el.lit,
                kids: el.children.length };
  }
  return out;
}

(async () => {
  vm.runInContext(script, ctx, { filename: 'dashboard.js' });
  if (sources.length !== 1 || typeof sources[0].onmessage !== 'function') {
    throw new Error('the page did not open one EventSource with an onmessage handler');
  }
  const result = {};
  for (const snap of input.snapshots) {
    clock += 100;
    sources[0].onmessage({ data: JSON.stringify(snap.data) });
    await settle();
    result[snap.name] = dump();
  }
  // Fire every handler the page registered, once, as an operator might.
  for (const { el, type, fn } of listening.slice()) {
    fn.call(el, { type, target: el, currentTarget: el, preventDefault() {} });
    await settle();
  }
  // And the handlers written inline in the markup (onclick="togglePower()").
  const inline = [...html.matchAll(/\son(\w+)="([^"]+)"/g)];
  if (!inline.length) throw new Error('no inline handlers found; has the markup changed?');
  for (const m of inline) {
    vm.runInContext(m[2], ctx, { filename: 'inline-on' + m[1] });
    await settle();
  }
  sources[0].onerror();
  result.after_error = dump();
  process.stdout.write(JSON.stringify(result));
})().catch(err => {
  console.error(err && err.stack ? err.stack : err);
  process.exit(1);
});
