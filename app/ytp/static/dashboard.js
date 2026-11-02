/* The alert strip and the dashboard (issue #38): vanilla JS, no libraries, no external requests.
   Everything that comes from the server is set with textContent, never as markup. */
(function (root) {
  "use strict";

  var L = root.Logic;
  var STRIP_MAX = 3;
  var ALERTS_LIMIT = 50;
  var DASH_POLL_MS = 3000;

  function create(ctx) {
    var t = ctx.t;
    var $ = ctx.$;
    var view = L.pageView(root.location.pathname);
    var alertsData = null;       // last answer of /api/alerts; null = never answered
    var alertsFailed = false;
    var dashData = null;         // last answer of /api/dashboard
    var dashFailed = false;
    var extraAlert = null;       // an older alert named by the address (#alert-12)
    var busy = {};               // "ack:12", "ack-all", "retry:7" while a request runs
    var timer = null;

    function el(tag, className, text) {
      var node = document.createElement(tag);
      if (className) node.className = className;
      if (text != null) node.textContent = text;
      return node;
    }

    function has(key) { return t(key) !== key; }

    function lang() { return ctx.lang(); }

    /* ------------------------------------------------------------ alert texts */

    function alertTitle(alert) {
      var texts = L.alertTexts(alert, has);
      return texts.title ? t(texts.title, texts.params) : alert.title;
    }

    function alertParts(alert) {
      var texts = L.alertTexts(alert, has);
      if (texts.fallback) return [{ label: null, text: alert.detail }].filter(function (p) { return p.text; });
      var parts = [];
      [["what", "alert.label_what"], ["did", "alert.label_did"], ["todo", "alert.label_todo"]].forEach(function (pair) {
        if (texts[pair[0]]) parts.push({ label: t(pair[1]), text: t(texts[pair[0]], texts.params) });
      });
      return parts;
    }

    /* ------------------------------------------------------------ the strip */

    function renderStrip() {
      var strip = $("alert-strip");
      var list = $("alert-strip-list");
      list.textContent = "";
      var unread = alertsData ? alertsData.unread : 0;
      var shown = alertsData ? L.stripAlerts(alertsData.alerts, STRIP_MAX) : [];
      strip.hidden = shown.length === 0;
      if (!shown.length) return;
      shown.forEach(function (alert) {
        var item = el("li", "alert-chip alert-" + alert.severity);
        var link = el("a", "alert-link", alertTitle(alert));
        link.href = "/dashboard#" + L.alertAnchor(alert.id);
        item.appendChild(link);
        var when = ctx.timeText(alert.time);
        if (when) item.appendChild(el("span", "alert-when", when));
        var dismiss = el("button", "ghost small alert-dismiss", "×");
        dismiss.type = "button";
        dismiss.disabled = !!busy["ack:" + alert.id];
        dismiss.setAttribute("aria-label", t("alert.dismiss"));
        dismiss.addEventListener("click", function () { acknowledge(alert.id); });
        item.appendChild(dismiss);
        list.appendChild(item);
      });
      var more = $("alert-strip-more");
      more.textContent = unread > shown.length ? t("alert.more", { n: unread - shown.length }) : t("alert.all");
      var all = $("alert-strip-ack");
      all.hidden = unread < 2;
      all.disabled = !!busy["ack-all"];
    }

    function refreshAlerts() {
      if (ctx.isAdmin && !ctx.isAdmin()) return Promise.resolve();   // the alerts are the admin's (#44)
      return ctx.getJson("/api/alerts?limit=" + ALERTS_LIMIT).then(function (data) {
        alertsData = data;
        alertsFailed = false;
      }).catch(function () { alertsFailed = true; }).then(function () {
        renderStrip();
        if (view === "dashboard") renderAlertLog();
      });
    }

    function acknowledge(id) {
      busy["ack:" + id] = true;
      renderStrip();
      ctx.post("/api/alerts/" + id + "/ack", {}).catch(function () { return null; }).then(function () {
        delete busy["ack:" + id];
        return refreshAlerts();
      });
    }

    function acknowledgeAll() {
      busy["ack-all"] = true;
      renderStrip();
      if (view === "dashboard") renderAlertLog();
      ctx.post("/api/alerts/ack-all", {}).catch(function () { return null; }).then(function () {
        delete busy["ack-all"];
        return refreshAlerts();
      });
    }

    /* ------------------------------------------------------------ the dashboard */

    function numbers(job) {
      var parts = [];
      if (job.progress != null) parts.push(t("jobs.progress", { n: job.progress }));
      var done = L.formatBytes(job.done_bytes, lang());
      var total = L.formatBytes(job.total_bytes, lang());
      if (done && total) parts.push(t("dash.size", { done: done, total: total }));
      else if (total) parts.push(total);
      var speed = L.formatSpeed(job.speed, lang());
      if (speed) parts.push(speed);
      var eta = L.formatEta(job.eta);
      if (eta) parts.push(t("dash.eta", { eta: eta }));
      return parts;
    }

    function activeRow(job) {
      var li = el("li", "job");
      var top = el("div", "job-top");
      top.appendChild(el("span", "job-name", job.label));
      top.appendChild(el("span", "job-status", ctx.statusText(job.status)));
      li.appendChild(top);
      var meta = [t("jobs.id", { id: job.id })];
      var kind = L.kindKey(job.kind);
      if (kind) meta.push(t(kind));
      li.appendChild(el("div", "job-meta", meta.join(" · ")));
      if (job.progress != null) {
        var bar = el("progress");
        bar.max = 100;
        bar.value = job.progress;
        bar.setAttribute("aria-label", t("jobs.progress", { n: job.progress }));
        li.appendChild(bar);
      }
      var parts = numbers(job);
      var line = parts.length ? parts.join(" · ")
        : t(job.status === "queued" ? "dash.waiting" : "dash.starting");
      li.appendChild(el("span", "job-percent", line));
      return li;
    }

    function recentRow(job) {
      var li = el("li", "job" + (job.status === "failed" ? " job-failed" : job.status === "done" ? " job-done" : ""));
      var top = el("div", "job-top");
      top.appendChild(el("span", "job-name", job.label));
      top.appendChild(el("span", "job-status", ctx.statusText(job.status)));
      li.appendChild(top);
      var meta = [t("jobs.id", { id: job.id })];
      var kind = L.kindKey(job.kind);
      if (kind) meta.push(t(kind));
      var when = ctx.timeText(job.updated_at);
      if (when) meta.push(when);
      li.appendChild(el("div", "job-meta", meta.join(" · ")));
      if (job.status === "failed") li.appendChild(el("p", "job-error", job.error || t("jobs.no_reason")));
      if (job.can_retry) {
        var button = el("button", "ghost small", t(busy["retry:" + job.id] ? "jobs.retrying" : "jobs.retry"));
        button.type = "button";
        button.disabled = !!busy["retry:" + job.id];
        button.addEventListener("click", function () {
          busy["retry:" + job.id] = true;
          renderDashboard();
          ctx.retryJob(job.id).then(function () {
            delete busy["retry:" + job.id];
            refreshDashboard();
          });
        });
        li.appendChild(button);
      }
      return li;
    }

    function renderVpn(vpn) {
      var info = L.vpnView(vpn.current);
      var now = $("dash-vpn-now");
      now.className = "dash-vpn-now vpn-" + info.view;
      var text = t("vpn." + info.view);
      if (info.since) text += " · " + t("dash.since", { when: ctx.timeText(info.since) || info.since });
      now.textContent = text;
      var explain = "vpn.d." + info.view;
      $("dash-vpn-explain").textContent = info.view === "ok" ? t("dash.vpn_explain_ok") : has(explain) ? t(explain) : "";
      var list = $("dash-vpn-list");
      list.textContent = "";
      var history = vpn.history || [];   // a user's answer carries none
      history.forEach(function (event) {
        var key = "vpn." + event.condition;
        var line = (ctx.timeText(event.time) || event.time) + " · " + (has(key) ? t(key) : event.condition);
        if (event.paused) line += " · " + t("dash.paused");
        if (event.reason) line += " · " + event.reason;
        list.appendChild(el("li", "dash-event", line));
      });
      $("dash-vpn-empty").hidden = history.length > 0;
    }

    function renderDeploy(deploy) {
      var line = $("dash-deploy");
      line.hidden = !deploy;
      if (!deploy) return;
      var parts = [t("dash.deploy_version", { version: deploy.deployed || "?" })];
      if (deploy.halted) parts.push(t("dash.deploy_halted"));
      else if (deploy.paused) parts.push(t("dash.deploy_paused"));
      else if (deploy.result) {
        var key = "deploy.result." + deploy.result;
        parts.push(t("dash.deploy_last", { result: has(key) ? t(key) : deploy.result }));
      }
      line.textContent = parts.join(" \u00b7 ");
    }

    function renderDashboard() {
      $("dash-error").textContent = dashFailed ? t("dash.load_error") : "";
      if (!dashData) return;
      var active = $("dash-active-list");
      active.textContent = "";
      dashData.active.forEach(function (job) { active.appendChild(activeRow(job)); });
      $("dash-active-empty").hidden = dashData.active.length > 0;
      var recent = $("dash-recent-list");
      recent.textContent = "";
      dashData.recent.forEach(function (job) { recent.appendChild(recentRow(job)); });
      $("dash-recent-empty").hidden = dashData.recent.length > 0;
      renderVpn(dashData.vpn);
      focusVpn();
      renderDeploy(dashData.deploy);
      var disk = dashData.disk;
      $("dash-disk").textContent = disk
        ? t("dash.disk", { percent: disk.free_percent, free: L.formatBytes(disk.free_gb * 1e9, lang()), total: L.formatBytes(disk.total_gb * 1e9, lang()) })
        : t("dash.disk_unknown");
    }

    function alertItem(alert, target) {
      var li = el("li", "alert-item alert-" + alert.severity + (alert.acknowledged ? "" : " alert-unread") + (target ? " alert-target" : ""));
      li.id = L.alertAnchor(alert.id);
      var head = el("div", "job-top");
      head.appendChild(el("h3", "alert-title", alertTitle(alert)));
      head.appendChild(el("span", "job-status", t("alert.sev." + alert.severity)));
      li.appendChild(head);
      li.appendChild(el("div", "job-meta", (ctx.timeText(alert.time) || alert.time) + (alert.acknowledged ? "" : " · " + t("alert.unread"))));
      alertParts(alert).forEach(function (part) {
        var p = el("p", "alert-part");
        if (part.label) p.appendChild(el("strong", null, part.label + " "));
        p.appendChild(document.createTextNode(part.text));
        li.appendChild(p);
      });
      if (!alert.acknowledged) {
        var button = el("button", "ghost small", t("alert.ack"));
        button.type = "button";
        button.disabled = !!busy["ack:" + alert.id];
        button.addEventListener("click", function () { acknowledge(alert.id); });
        li.appendChild(button);
      }
      return li;
    }

    function renderAlertLog() {
      var list = $("dash-alerts-list");
      list.textContent = "";
      $("dash-alerts-error").textContent = alertsFailed ? t("alert.load_error") : "";
      var alerts = alertsData ? alertsData.alerts.slice() : [];
      var wanted = L.alertIdFromHash(root.location.hash);
      if (extraAlert && !alerts.some(function (a) { return a.id === extraAlert.id; })) alerts.push(extraAlert);
      alerts.forEach(function (alert) { list.appendChild(alertItem(alert, alert.id === wanted)); });
      $("dash-alerts-empty").hidden = alerts.length > 0 || !alertsData;
      var ack = $("dash-alerts-ack");
      ack.hidden = !alertsData || alertsData.unread === 0;
      ack.disabled = !!busy["ack-all"];
      focusWanted(wanted);
    }

    var focused = null;
    function focusWanted(wanted) {
      if (wanted == null || focused === wanted) return;
      var node = document.getElementById(L.alertAnchor(wanted));
      if (node) {
        focused = wanted;
        node.scrollIntoView({ block: "center" });
      } else if (alertsData && !extraAlert) {
        /* An older alert than the list holds: fetch just that one. */
        ctx.getJson("/api/alerts/" + wanted).then(function (alert) {
          extraAlert = alert;
          renderAlertLog();
        }).catch(function () { focused = wanted; });
      }
    }

    function refreshDashboard() {
      if (view !== "dashboard") return Promise.resolve();
      return ctx.getJson("/api/dashboard").then(function (data) {
        dashData = data;
        dashFailed = false;
      }).catch(function () { dashFailed = true; }).then(renderDashboard);
    }

    /* ------------------------------------------------------------ the page */

    function setCurrent(node, on) {
      if (on) node.setAttribute("aria-current", "page");
      else node.removeAttribute("aria-current");
    }

    /* The tab of the page you are on (the subscriptions are a part of the input page). */
    function renderNav() {
      var onDashboard = view === "dashboard";
      var onAccounts = view === "accounts";
      var subs = view === "input" && root.location.hash === "#subscriptions";
      setCurrent($("nav-input"), view === "input" && !subs);
      setCurrent($("nav-dashboard"), onDashboard);
      setCurrent($("nav-subs"), subs);
      setCurrent($("nav-accounts"), onAccounts);
    }

    /* The VPN bar links to /dashboard#vpn: bring that part into view and frame it. */
    function focusVpn() {
      if (view !== "dashboard" || root.location.hash !== "#vpn") return;
      var section = $("dash-vpn");
      section.classList.add("alert-target");
      section.scrollIntoView({ block: "start" });
    }

    function applyView() {
      document.querySelectorAll("[data-view]").forEach(function (node) {
        node.hidden = node.dataset.view !== view;
      });
      renderNav();
    }

    function render() {
      applyView();
      renderStrip();
      if (view === "dashboard") {
        renderDashboard();
        renderAlertLog();
      }
    }

    function start() {
      $("alert-strip-ack").addEventListener("click", acknowledgeAll);
      $("dash-alerts-ack").addEventListener("click", acknowledgeAll);
      root.addEventListener("hashchange", function () {
        focused = null;
        renderNav();
        $("dash-vpn").classList.remove("alert-target");
        focusVpn();
        renderAlertLog();
      });
      applyView();
      refreshAlerts();
      if (view !== "dashboard") return;
      focusVpn();
      refreshDashboard();
      timer = root.setInterval(function () {
        if (document.visibilityState !== "hidden") refreshDashboard();
      }, DASH_POLL_MS);
      document.addEventListener("visibilitychange", function () {
        if (document.visibilityState === "visible") refreshDashboard();
      });
    }

    return { start: start, render: render, refreshAlerts: refreshAlerts, view: function () { return view; }, stop: function () { if (timer) root.clearInterval(timer); } };
  }

  root.Dash = { create: create };
})(window);
