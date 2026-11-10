// Runs static/dashboard.js against a tiny fake DOM, so a typo or a wrong id fails a test and not
// the owner's browser. Usage: node dashboard_smoke.js <static dir> <path> <hash> <scenario json>
const fs = require("fs");
const path = require("path");
const [staticDir, pathname, hash, scenarioJson] = process.argv.slice(2);
const scenario = JSON.parse(scenarioJson);

class Node {
  constructor(tag) {
    this.tagName = tag; this.children = []; this._text = ""; this.className = ""; this.dataset = {};
    this.attrs = {}; this.style = {}; this.hidden = false; this.disabled = false; this.listeners = {}; this.id = "";
  }
  appendChild(child) { this.children.push(child); return child; }
  setAttribute(k, v) { this.attrs[k] = v; }
  removeAttribute(k) { delete this.attrs[k]; }
  get classList() {
    const node = this;
    return {
      add(name) { if (!node.className.split(" ").includes(name)) node.className = (node.className + " " + name).trim(); },
      remove(name) { node.className = node.className.split(" ").filter((c) => c && c !== name).join(" "); },
    };
  }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  click() { (this.listeners.click || []).forEach((fn) => fn()); }
  scrollIntoView() { this.scrolled = true; scrollLog.push(this.id); }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
}
const scrollLog = [];
const byId = {};
for (const id of scenario.ids) { const n = new Node("div"); n.id = id; byId[id] = n; }
const viewNodes = (scenario.viewNodes || []).map((v) => { const n = new Node("div"); n.dataset.view = v; return n; });
const dashViewIds = { "dash-overview": "overview", "dash-jobs-view": "jobs", "dash-vpn-view": "vpn", "dash-alerts-view": "alerts" };
const dashViews = [];
for (const [id, view] of Object.entries(dashViewIds)) {
  if (byId[id]) { byId[id].dataset.dashView = view; dashViews.push(byId[id]); }
}
const dashTabs = [];
for (const id of scenario.ids) {
  if (id.startsWith("dash-tab-")) { byId[id].dataset.dashTab = id.slice("dash-tab-".length); dashTabs.push(byId[id]); }
}
const document = {
  visibilityState: "visible",
  createElement: (tag) => new Node(tag),
  createTextNode: (text) => { const n = new Node("#text"); n._text = text; return n; },
  getElementById: (id) => { if (byId[id]) return byId[id]; for (const n of Object.values(byId)) { const f = find(n, id); if (f) return f; } return null; },
  querySelectorAll: (selector) => selector === "[data-dash-view]" ? dashViews
    : selector === "[data-dash-tab]" ? dashTabs : viewNodes,
  addEventListener() {},
};
function find(node, id) {
  if (node.id === id) return node;
  for (const c of node.children) { const f = find(c, id); if (f) return f; }
  return null;
}
const calls = [];
const intervals = [];
const win = {
  location: { pathname, hash }, Logic: require(path.join(staticDir, "logic.js")),
  setInterval: (fn) => { intervals.push(fn); return 1; }, clearInterval() {}, addEventListener() {},
};
global.document = document;
new Function("window", fs.readFileSync(path.join(staticDir, "dashboard.js"), "utf8"))(win);

const dictionary = scenario.dictionary;
const t = (key, vars) => (key in dictionary ? win.Logic.format(dictionary[key], vars) : key);
const answers = scenario.answers;
const ctx = {
  t, $: (id) => { const n = document.getElementById(id); if (!n) throw new Error("no element " + id); return n; },
  lang: () => "en", timeText: () => "5 min ago", statusText: (s) => "status " + s,
  getJson: (url) => { calls.push(["GET", url]); const a = answers[url.split("?")[0]]; return a ? Promise.resolve(a) : Promise.reject(new Error("no answer for " + url)); },
  post: (url) => { calls.push(["POST", url]); return Promise.resolve({ status: 200, data: {} }); },
  retryJob: (id) => { calls.push(["RETRY", id]); return Promise.resolve({}); },
};
const dash = win.Dash.create(ctx);
dash.start();
setTimeout(() => {
  if (scenario.click === "dismiss") {
    const chip = byId["alert-strip-list"].children[0];
    chip.children[chip.children.length - 1].click();
  }
  if (scenario.click === "retry") {
    const first = byId["dash-recent-list"].children[0];
    first.children[first.children.length - 1].click();
  }
  if (scenario.click === "ack-all") byId["alert-strip-ack"].click();
  /* The periodic refresh, as often as the scenario wants it. */
  for (let i = 0; i < (scenario.polls || 0); i++) intervals.forEach((fn) => fn());
  setTimeout(finish, 50);
}, 50);

function finish() {
  dash.render();
  const out = { calls, scrolls: scrollLog, nodes: {} };
  for (const id of Object.keys(byId)) out.nodes[id] = { text: byId[id].textContent, hidden: byId[id].hidden, attrs: byId[id].attrs, style: byId[id].style, cls: byId[id].className, scrolled: !!byId[id].scrolled, children: byId[id].children.map((c) => ({ cls: c.className, text: c.textContent, id: c.id, style: c.style })) };
  out.views = viewNodes.map((n) => [n.dataset.view, n.hidden]);
  out.strip = document.getElementById("alert-strip-list").children.map((c) => ({ cls: c.className, text: c.textContent, href: (c.children[0] || {}).href }));
  console.log(JSON.stringify(out));
}
