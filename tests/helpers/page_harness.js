// Runs the page's real scripts (logic.js, dashboard.js, auth.js, app.js) against the real index.html in a
// small DOM written for this purpose, and reports what happened. Not a browser: no layout and no
// CSS; but every script error that does not depend on one shows up, and that is what a page
// whose scripts "do not run to the end" (issue #43) needs.
// Usage: node page_harness.js <static dir> <pathname> <hash> <language> <answers json> [steps json]
// Steps (run after the scripts): {set: sel, value}, {check: sel, value}, {fire: sel, type},
// {submit: sel}, {click: sel}, {tick}, {wait}, {read: sel}. Answers: "METHOD /path" or "/path".
const fs = require("fs");
const path = require("path");
const [staticDir, pathname, hash, language, answersJson, stepsJson] = process.argv.slice(2);
const steps = JSON.parse(stepsJson || "[]");
const answers = JSON.parse(answersJson || "{}");

// ------------------------------------------------------------------ the DOM
const VOID = new Set(["meta", "link", "input", "br", "img", "hr", "source", "path", "ellipse", "circle", "text", "rect", "line"]);
class TextNode { constructor(t) { this.nodeType = 3; this._text = t; this.parentNode = null; } get textContent() { return this._text; } }
class El {
  constructor(tag, attrs = {}) {
    this.nodeType = 1; this.tagName = tag.toUpperCase(); this.tag = tag.toLowerCase(); this.attrs = { ...attrs };
    this.children = []; this.parentNode = null; this.listeners = {}; this.style = {};
    this._hidden = "hidden" in attrs; this._disabled = "disabled" in attrs; this._checked = "checked" in attrs;
    this._value = attrs.value !== undefined ? attrs.value : null; this.open = false;
  }
  get id() { return this.attrs.id || ""; } set id(v) { this.attrs.id = v; }
  get className() { return this.attrs.class || ""; } set className(v) { this.attrs.class = v; }
  get hidden() { return this._hidden; } set hidden(v) { this._hidden = !!v; }
  get disabled() { return this._disabled; } set disabled(v) { this._disabled = !!v; }
  get checked() { return this._checked; } set checked(v) { this._checked = !!v; }
  get type() { return this.attrs.type || (this.tag === "input" ? "text" : ""); } set type(v) { this.attrs.type = v; }
  get name() { return this.attrs.name || ""; }
  get title() { return this.attrs.title || ""; } set title(v) { this.attrs.title = v; }
  get placeholder() { return this.attrs.placeholder || ""; } set placeholder(v) { this.attrs.placeholder = v; }
  get href() { return this.attrs.href || ""; } set href(v) { this.attrs.href = v; }
  get maxLength() { return Number(this.attrs.maxlength || -1); }
  get max() { return this.attrs.max; } set max(v) { this.attrs.max = String(v); }
  get min() { return this.attrs.min; } set min(v) { this.attrs.min = String(v); }
  get value() {
    if (this._value !== null) return this._value;
    if (this.tag === "select") { const o = this.options[0]; return o ? o.value : ""; }
    return "";
  }
  set value(v) { this._value = String(v); }
  get options() { return this.querySelectorAll("option"); }
  get dataset() {
    const el = this;
    return new Proxy({}, {
      get(_, key) { return el.attrs["data-" + String(key).replace(/[A-Z]/g, (c) => "-" + c.toLowerCase())]; },
      set(_, key, v) { el.attrs["data-" + String(key).replace(/[A-Z]/g, (c) => "-" + c.toLowerCase())] = String(v); return true; },
    });
  }
  get classList() {
    const el = this;
    return {
      add(...n) { const s = new Set(el.className.split(/\s+/).filter(Boolean)); n.forEach((x) => s.add(x)); el.className = [...s].join(" "); },
      remove(...n) { el.className = el.className.split(/\s+/).filter((c) => c && !n.includes(c)).join(" "); },
      toggle(n, force) { const has = el.className.split(/\s+/).includes(n); const on = force === undefined ? !has : force; if (on) this.add(n); else this.remove(n); return on; },
      contains(n) { return el.className.split(/\s+/).includes(n); },
    };
  }
  get textContent() { return this.children.map((c) => c.textContent).join(""); }
  set textContent(v) { this.children = []; if (String(v) !== "") this.appendChild(new TextNode(String(v))); }
  get firstChild() { return this.children[0] || null; }
  get lastChild() { return this.children[this.children.length - 1] || null; }
  appendChild(c) { if (c.parentNode) c.parentNode.children = c.parentNode.children.filter((x) => x !== c); c.parentNode = this; this.children.push(c); return c; }
  removeChild(c) { this.children = this.children.filter((x) => x !== c); c.parentNode = null; return c; }
  insertBefore(c, ref) { c.parentNode = this; const i = ref ? this.children.indexOf(ref) : -1; if (i < 0) this.children.push(c); else this.children.splice(i, 0, c); return c; }
  replaceChildren(...n) { this.children = []; n.forEach((c) => this.appendChild(c)); }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === "hidden") this._hidden = true; }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; if (k === "hidden") this._hidden = false; }
  hasAttribute(k) { return k in this.attrs; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  removeEventListener() {}
  dispatchEvent(e) { (this.listeners[e.type] || []).forEach((f) => f(e)); return true; }
  focus() {} blur() {} select() {} scrollIntoView() {} click() { this.dispatchEvent({ type: "click", target: this, preventDefault() {} }); }
  showModal() { this.open = true; } close() { this.open = false; }
  closest(sel) { let n = this; while (n && n.nodeType === 1) { if (matches(n, parseCompound(sel.trim()))) return n; n = n.parentNode; } return null; }
  querySelectorAll(sel) { return queryAll(this, sel); }
  querySelector(sel) { return queryAll(this, sel)[0] || null; }
  getElementsByTagName(t) { return queryAll(this, t); }
}
function descendants(root) { const out = []; (function walk(n) { for (const c of n.children) { if (c.nodeType === 1) { out.push(c); walk(c); } } })(root); return out; }
function parseCompound(s) {
  const c = { tag: null, id: null, classes: [], attrs: [], checked: false };
  const re = /^([a-zA-Z][\w-]*)|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:=(?:"([^"]*)"|'([^']*)'|([^\]]*)))?\]|:(checked)/y;
  let m; re.lastIndex = 0;
  while (re.lastIndex < s.length && (m = re.exec(s))) {
    if (m[1]) c.tag = m[1].toLowerCase(); else if (m[2]) c.id = m[2]; else if (m[3]) c.classes.push(m[3]);
    else if (m[4]) c.attrs.push([m[4], m[5] ?? m[6] ?? m[7] ?? null]); else if (m[8]) c.checked = true;
  }
  if (re.lastIndex < s.length) throw new Error("selector not understood by the test DOM: " + s);
  return c;
}
function matches(el, c) {
  if (c.tag && el.tag !== c.tag) return false;
  if (c.id && el.id !== c.id) return false;
  const cl = el.className.split(/\s+/);
  if (c.classes.some((k) => !cl.includes(k))) return false;
  for (const [k, v] of c.attrs) { if (!(k in el.attrs)) return false; if (v !== null && el.attrs[k] !== v) return false; }
  if (c.checked && !el.checked) return false;
  return true;
}
function queryAll(root, selector) {
  const out = new Set();
  for (const part of selector.split(",")) {
    const chain = part.trim().split(/\s+/).map(parseCompound);
    for (const el of descendants(root)) {
      let i = chain.length - 1; if (!matches(el, chain[i])) continue;
      let n = el.parentNode; i -= 1;
      while (i >= 0 && n && n.nodeType === 1) { if (matches(n, chain[i])) i -= 1; n = n.parentNode; }
      if (i < 0) out.add(el);
    }
  }
  return [...out];
}
function parseHtml(html) {
  const root = new El("#root"); const stack = [root];
  const re = /<!--[\s\S]*?-->|<!doctype[^>]*>|<\/([a-zA-Z][\w-]*)\s*>|<([a-zA-Z][\w-]*)((?:\s+[^\s=>\/]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?)*)\s*(\/?)>|([^<]+)/gi;
  let m;
  while ((m = re.exec(html))) {
    if (m[1]) { const t = m[1].toLowerCase(); for (let i = stack.length - 1; i > 0; i--) { if (stack[i].tag === t) { stack.length = i; break; } } }
    else if (m[2]) {
      const tag = m[2].toLowerCase(); const attrs = {};
      for (const a of m[3].matchAll(/([^\s=>\/]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)) attrs[a[1].toLowerCase()] = a[2] ?? a[3] ?? a[4] ?? "";
      const el = new El(tag, attrs); stack[stack.length - 1].appendChild(el);
      if (tag === "script" || tag === "style") { const end = html.indexOf("</" + tag, re.lastIndex); const body = html.slice(re.lastIndex, end); if (body) el.appendChild(new TextNode(body)); re.lastIndex = end; continue; }
      if (!VOID.has(tag) && !m[4]) stack.push(el);
    } else if (m[5] && stack.length) { if (m[5].trim() !== "" || stack.length > 1) stack[stack.length - 1].appendChild(new TextNode(m[5])); }
  }
  return root;
}

// ------------------------------------------------------------------ the page
const html = fs.readFileSync(path.join(staticDir, "index.html"), "utf8");
const tree = parseHtml(html);
const errors = [];
const timers = [];
let timerIds = 0;
const fetched = [];
const store = {};
const document = {
  visibilityState: "visible", documentElement: new El("html"), body: tree,
  getElementById: (id) => queryAll(tree, "#" + id)[0] || null,
  querySelector: (s) => queryAll(tree, s)[0] || null, querySelectorAll: (s) => queryAll(tree, s),
  createElement: (t) => new El(t), createTextNode: (t) => new TextNode(t),
  addEventListener(t, f) { (this.l = this.l || {}); (this.l[t] = this.l[t] || []).push(f); },
};
function fireEvent(el, type, extra = {}) {
  const event = { type, target: el, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, stopPropagation() {}, ...extra };
  const guard = (document.l && document.l[type]) || [];
  try { guard.forEach((f) => f(event)); (el.listeners[type] || []).forEach((f) => f(event)); }
  catch (e) { errors.push({ file: "event " + type + " on " + (el.id || el.tag), message: String(e && e.message), stack: String(e && e.stack).split("\n").slice(0, 5).join("\n") }); }
  return event;
}
let reloads = 0;
const location = { pathname, hash, href: "http://page.test" + pathname + hash, reload() { reloads += 1; } };
const window = {
  location, document, Logic: require(path.join(staticDir, "logic.js")),
  localStorage: { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } },
  setTimeout: (f, ms) => { const id = ++timerIds; timers.push({ id, f, ms, once: true }); return id; },
  clearTimeout: (id) => { const i = timers.findIndex((t) => t.id === id); if (i >= 0) timers.splice(i, 1); },
  setInterval: (f, ms) => { const id = ++timerIds; timers.push({ id, f, ms }); return id; },
  clearInterval: (id) => { const i = timers.findIndex((t) => t.id === id); if (i >= 0) timers.splice(i, 1); },
  addEventListener() {}, scrollTo() {},
};
const navigator = { language };
const fetchStub = (url, options) => {
  fetched.push([(options && options.method) || "GET", String(url)]);
  const path = String(url).split("?")[0];
  const method = (options && options.method) || "GET";
  const key = method + " " + path in answers ? method + " " + path : path;
  let body = key in answers ? answers[key] : null;
  let status = body !== null ? 200 : 404;
  if (body && typeof body === "object" && "__status" in body) { status = body.__status; body = body.body; }
  return Promise.resolve({ ok: status >= 200 && status < 300, status, json: () => Promise.resolve(body), text: () => Promise.resolve(JSON.stringify(body)) });
};
function run(file) {
  const code = fs.readFileSync(path.join(staticDir, file), "utf8");
  try { new Function("window", "document", "navigator", "fetch", code)(window, document, navigator, fetchStub); }
  catch (e) { errors.push({ file, message: String(e && e.message), stack: String(e && e.stack).split("\n").slice(0, 6).join("\n") }); }
}
for (const f of ["dashboard.js", "auth.js", "app.js"]) run(f);   // the order the page loads them in (logic.js is window.Logic)
function select(sel) { const el = document.querySelector(sel); if (!el) throw new Error("no element for " + sel); return el; }
const pause = (ms = 15) => new Promise((resolve) => setTimeout(resolve, ms));
(async () => {
  await pause(30);
  const reads = {}; const prevented = {};
  for (const step of steps) {
    try {
      if ("set" in step) { select(step.set).value = step.value; }
      else if ("check" in step) { select(step.check).checked = step.value; }
      else if ("fire" in step) { fireEvent(select(step.fire), step.type); }
      else if ("submit" in step) { prevented[step.submit] = fireEvent(select(step.submit), "submit").defaultPrevented; }
      else if ("click" in step) { fireEvent(select(step.click), "click"); }
      else if ("tick" in step) { const due = timers.filter((t) => t.once).splice(0); timers.splice(0, timers.length, ...timers.filter((t) => !t.once)); due.forEach((t) => { try { t.f(); } catch (e) { errors.push({ file: "timer", message: String(e && e.message), stack: String(e && e.stack).split("\n").slice(0, 5).join("\n") }); } }); }
      else if ("read" in step) { const el = select(step.read); reads[step.read] = { text: el.textContent, hidden: el.hidden, value: el.value, children: el.children.length, disabled: el.disabled, cls: el.className }; }
    } catch (e) { errors.push({ file: "step " + JSON.stringify(step), message: String(e && e.message), stack: "" }); }
    await pause();
  }
  const warning = document.getElementById("js-warning");
  console.log(JSON.stringify({
    errors, warningHidden: warning ? warning.hidden : null, fetched, reads, prevented, reloads,
    auth: tree.attrs["data-auth"] || null, role: tree.attrs["data-role"] || null, href: location.href,
    version: (document.querySelector('meta[name="ytp-static-version"]') || { attrs: {} }).attrs.content || null,
    banner: document.getElementById("update-banner").hidden,
  }));
})();
