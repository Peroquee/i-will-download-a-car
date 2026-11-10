/* The alert strip and the dashboard (issue #38): vanilla JS, no libraries, no external requests.
   Everything that comes from the server is set with textContent, never as markup. */
(function (root) {
  "use strict";

  var L = root.Logic;
  var STRIP_MAX = 3;
  var ALERTS_LIMIT = 50;
  var DASH_POLL_MS = 3000;
  /* The storage bar's categories, in the order the server uses (#82). The second value is the
     key `Logic.kindKey` knows for the category name in the i18n table. */
  var STORAGE_KINDS = [
    ["movies", "movies"], ["series", "series"], ["audiobooks", "audiobooks"],
    ["music", "music"], ["podcasts", "podcast"], ["youtube", "youtube"]
  ];

  function create(ctx) {
    var t = ctx.t;
    var $ = ctx.$;
    var view = L.pageView(root.location.pathname, root.location.hash);
    var alertsData = null;       // last answer of /api/alerts; null = never answered
    var alertsFailed = false;
    var dashData = null;         // last answer of /api/dashboard
    var dashFailed = false;
    var vpnStatus = null;        // last answer of /api/vpn, for the overview cards
    var vpnFailed = false;
    var jobsData = null;         // last answer of /api/jobs?limit=50, for finished downloads
    var jobsFailed = false;
    var extraAlert = null;       // an older alert named by the address (#alert-12)
    var showDismissed = false;   // list the failed jobs this account dismissed as well
    var finishedLimit = 10;      // finished downloads shown before "Show 50"
    var busy = {};               // "ack:12", "ack-all", "retry:7", "dismiss:7" while a request runs

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
        if (view === "dashboard") {
          renderAlertSummary();
          renderAlertLog();
        }
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
      var name = el("span", "job-name", job.label);
      if (job.high_priority) name.appendChild(el("span", "priority-badge", t("jobs.priority")));
      top.appendChild(name);
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

    /* "Ausblenden" / "Wieder einblenden": hides a failed job for this account (it stays, and can
       still be retried); the input page's red "failed: N" no longer counts it. */
    function dismissButton(job) {
      var key = "dismiss:" + job.id;
      var label = job.dismissed ? "jobs.restore" : "jobs.dismiss";
      var button = el("button", "ghost small dismiss", t(label));
      button.type = "button";
      button.disabled = !!busy[key];
      button.setAttribute("aria-label", t(label) + ": " + job.label);
      button.addEventListener("click", function () {
        busy[key] = true;
        renderDashboard();
        var done = job.dismissed ? ctx.restoreJob(job.id) : ctx.dismissJob(job.id);
        done.then(function () { delete busy[key]; refreshDashboard(); });
      });
      return button;
    }

    function finishedJobs() {
      var jobs = jobsData ? jobsData.jobs.filter(function (job) {
        return job.status === "done" || job.status === "failed";
      }) : [];
      if (!showDismissed) {
        jobs = jobs.filter(function (job) { return !job.dismissed; });
      }
      return jobs;
    }

    function renderFinished() {
      var list = $("dash-recent-list");
      list.textContent = "";
      var jobs = finishedJobs();
      jobs.slice(0, finishedLimit).forEach(function (job) {
        list.appendChild(recentRow(job));
      });
      $("dash-recent-empty").hidden = jobs.length > 0 || !jobsData;
      renderRecentTools();
    }

    /* The two buttons above the list: dismiss every failed job, show or hide the dismissed ones. */
    function renderRecentTools() {
      var jobs = finishedJobs();
      var failed = jobs.some(function (job) { return job.status === "failed" && !job.dismissed; });
      var all = $("dash-dismiss-all");
      all.hidden = !failed;
      all.disabled = !!busy["dismiss-all"];
      var count = jobsData ? jobsData.counts.dismissed || 0 : 0;
      var toggle = $("dash-show-dismissed");
      toggle.hidden = count === 0 && !showDismissed;
      toggle.textContent = t(showDismissed ? "dash.hide_dismissed" : "dash.show_dismissed", { n: count });
      toggle.setAttribute("aria-pressed", showDismissed ? "true" : "false");
      var limit = $("dash-limit");
      limit.hidden = jobs.length <= 10;
      limit.textContent = t(finishedLimit === 10 ? "dash.show_50" : "dash.show_10");
    }

    function dismissAll() {
      busy["dismiss-all"] = true;
      renderDashboard();
      ctx.dismissAllFailed().then(function () { delete busy["dismiss-all"]; refreshDashboard(); });
    }

    function toggleDismissed() {
      showDismissed = !showDismissed;
      renderFinished();
    }

    function toggleLimit() {
      finishedLimit = finishedLimit === 10 ? 50 : 10;
      renderFinished();
    }

    /* ------------------------------------------------------------ the overview */

    function setStatusCard(cardId, stateId, detailId, tone, state, detail) {
      $(cardId).className = "status-card state-" + tone;
      $(stateId).textContent = state;
      $(detailId).textContent = detail || "";
    }

    function renderStatus() {
      if (vpnFailed || !vpnStatus) {
        setStatusCard("status-internet", "status-internet-state", "status-internet-detail",
          "warn", t("dash.internet_unknown"), "");
        setStatusCard("status-vpn", "status-vpn-state", "status-vpn-detail",
          "warn", t("vpn.unknown"), "");
        setStatusCard("status-watchdog", "status-watchdog-state", "status-watchdog-detail",
          "bad", t("dash.watchdog_unreachable"), "");
        return;
      }
      // The internet signal is independent of the VPN's egress check (#63).
      var internet = vpnStatus.internet || {};
      var internetState = ["ok", "offline", "unknown"].indexOf(internet.state) >= 0
        ? internet.state : "unknown";
      var internetDetail = internet.checked_at
        ? t("dash.checked", { when: ctx.timeText(internet.checked_at) || internet.checked_at })
        : "";
      if (internetState === "unknown" && internet.reason === "disabled") {
        internetDetail = t("dash.internet_disabled");
      }
      setStatusCard("status-internet", "status-internet-state", "status-internet-detail",
        internetState === "ok" ? "ok" : internetState === "offline" ? "bad" : "warn",
        t("dash.internet_" + internetState), internetDetail);

      var info = L.vpnView(vpnStatus);
      var vpnTone = info.view === "ok" ? "ok"
        : (info.view === "down" || info.view === "mismatch" || info.view === "blind") ? "bad" : "warn";
      var vpnParts = [];
      var detailKey = L.vpnDetailKey(info);
      if (detailKey && has(detailKey)) vpnParts.push(t(detailKey));
      if (info.paused) vpnParts.push(t("vpn.paused"));
      if (info.since && info.view !== "unknown") {
        var since = ctx.timeText(info.since);
        if (since) vpnParts.push(t("dash.since", { when: since }));
      }
      if (info.country) vpnParts.push(t("vpn.country", { country: info.country }));
      setStatusCard("status-vpn", "status-vpn-state", "status-vpn-detail",
        vpnTone, t("vpn." + info.view), vpnParts.join(" · "));

      // The watchdog card says only whether it answers and polls freshly (#63); its VPN state
      // is the VPN card above.
      var dog = vpnStatus.watchdog || {};
      var dogState = ["ok", "blind", "stale", "unreachable", "starting"].indexOf(dog.state) >= 0
        ? dog.state : "unreachable";
      var dogTone = dogState === "ok" ? "ok" : dogState === "starting" ? "warn" : "bad";
      var dogDetail = dog.last_poll
        ? t("dash.checked", { when: ctx.timeText(dog.last_poll) || dog.last_poll })
        : "";
      setStatusCard("status-watchdog", "status-watchdog-state", "status-watchdog-detail",
        dogTone, t("dash.watchdog_" + dogState), dogDetail);
    }

    function renderJobsSummary() {
      var active = dashData ? dashData.active : [];
      var downloading = active.filter(function (job) { return job.status === "downloading"; });
      var timed = downloading.filter(function (job) { return L.formatEta(job.eta) !== null; });
      var next = timed.slice().sort(function (a, b) { return a.eta - b.eta; })[0];
      $("dash-next-job").textContent = next
        ? next.label + " · " + (L.formatEta(next.eta) || "")
        : t("dash.none");
      $("dash-job-count").textContent = t("dash.job_count", { n: active.length });
      var maxEta = timed.reduce(function (max, job) { return job.eta > max ? job.eta : max; }, 0);
      $("dash-total-eta").textContent = maxEta ? (L.formatEta(maxEta) || "—") : "—";
      var speed = downloading.reduce(function (sum, job) {
        return typeof job.speed === "number" ? sum + job.speed : sum;
      }, 0);
      $("dash-total-speed").textContent = speed ? (L.formatSpeed(speed, lang()) || "—") : "—";
    }

    function renderAlertSummary() {
      var box = $("dash-alert-counts");
      if (!box) return;
      box.textContent = "";
      if (!alertsData) return;
      var counts = { error: 0, warning: 0, info: 0 };
      alertsData.alerts.forEach(function (alert) {
        if (!alert.acknowledged && counts[alert.severity] != null) counts[alert.severity] += 1;
      });
      [["error", "dash.errors"], ["warning", "dash.warnings"], ["info", "dash.notices"]].forEach(function (pair) {
        var chip = el("span", "alert-count " + pair[0]);
        chip.appendChild(el("span", "dot"));
        chip.appendChild(el("strong", null, String(counts[pair[0]])));
        chip.appendChild(el("span", null, t(pair[1])));
        chip.setAttribute("aria-label", t(pair[1]) + ": " + counts[pair[0]]);
        box.appendChild(chip);
      });
      var total = alertsData.unread || 0;
      box.appendChild(el("span", "alert-count", total ? t("dash.unread", { n: total }) : t("dash.no_alerts")));
    }


    function recentRow(job) {
      var li = el("li", "job" + (job.status === "failed" ? " job-failed" : job.status === "done" ? " job-done" : ""));
      var top = el("div", "job-top");
      var name = el("span", "job-name", job.label);
      if (job.high_priority) name.appendChild(el("span", "priority-badge", t("jobs.priority")));
      top.appendChild(name);
      top.appendChild(el("span", "job-status", ctx.statusText(job.status)));
      li.appendChild(top);
      var meta = [t("jobs.id", { id: job.id })];
      var kind = L.kindKey(job.kind);
      if (kind) meta.push(t(kind));
      var when = ctx.timeText(job.updated_at);
      if (when) meta.push(when);
      li.appendChild(el("div", "job-meta", meta.join(" · ")));
      if (job.dismissed) li.classList.add("job-dismissed");
      if (job.status === "failed") li.appendChild(el("p", "job-error", job.error || t("jobs.no_reason")));
      else if (job.note) li.appendChild(el("p", "job-note-warn", job.note));
      if (job.status === "failed") li.appendChild(dismissButton(job));
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

    /* "All episodes" of a podcast (#45): how far each run is. */
    function backlogRow(run) {
      var li = el("li", "job" + (run.failed ? " job-failed" : run.state === "done" ? " job-done" : ""));
      var top = el("div", "job-top");
      top.appendChild(el("span", "job-name", run.folder));
      top.appendChild(el("span", "job-status", t("dash.backlog_" + run.state)));
      li.appendChild(top);
      li.appendChild(el("div", "job-meta", t("dash.backlog_line", { done: run.done, total: run.total })));
      var bar = el("progress");
      bar.max = run.total || 1;
      bar.value = run.done;
      bar.setAttribute("aria-label", t("dash.backlog_line", { done: run.done, total: run.total }));
      li.appendChild(bar);
      var notes = [];
      if (run.waiting && run.state !== "done") notes.push(t("dash.backlog_waiting", { n: run.waiting }));
      if (run.skipped) notes.push(t("dash.backlog_skipped", { n: run.skipped }));
      if (run.failed) notes.push(t("dash.backlog_failed", { n: run.failed }));
      if (notes.length) li.appendChild(el("span", "job-percent", notes.join(" \u00b7 ")));
      if (run.state === "running" || run.state === "paused") {
        var key = "backlog:" + run.id;
        var button = el("button", "ghost small", t(busy[key] ? "dash.backlog_stopping" : "dash.backlog_stop"));
        button.type = "button";
        button.disabled = !!busy[key];
        button.addEventListener("click", function () {
          busy[key] = true;
          renderDashboard();
          ctx.request("DELETE", "/api/podcasts/backlog/" + run.id, null)
            .catch(function () { return null; })
            .then(function () { delete busy[key]; refreshDashboard(); });
        });
        li.appendChild(button);
      }
      return li;
    }

    function renderBacklogs(runs) {
      var list = $("dash-backlog-list");
      list.textContent = "";
      (runs || []).forEach(function (run) { list.appendChild(backlogRow(run)); });
      $("dash-backlog-box").hidden = !(runs && runs.length);
    }

    function renderVpn(vpn) {
      var info = L.vpnView(vpn.current);
      var now = $("dash-vpn-now");
      now.className = "dash-vpn-now vpn-" + info.view;
      var text = t("vpn." + info.view);
      if (info.since) text += " · " + t("dash.since", { when: ctx.timeText(info.since) || info.since });
      now.textContent = text;
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

    /* The storage bar on the overview (#82): the cached category sizes as segments, the free
       space as the empty rest, and a legend with every category, "other" and the free line. */
    function storageWidth(size, total) {
      if (!(size > 0) || !(total > 0)) return "0%";
      return (size * 100 / total).toFixed(3) + "%";
    }

    function storageItem(cls, label, size) {
      var li = el("li", "storage-item");
      li.appendChild(el("span", "storage-swatch " + cls));
      li.appendChild(el("span", "storage-name", label));
      li.appendChild(el("span", "storage-size", L.formatBytes(size, lang()) || "—"));
      return li;
    }

    function renderStorage(disk) {
      var bar = $("dash-storage-bar");
      var legend = $("dash-storage-legend");
      var pending = $("dash-storage-pending");
      var unknown = $("dash-storage-unknown");
      var at = $("dash-storage-at");
      bar.textContent = "";
      legend.textContent = "";
      at.textContent = "";
      if (!disk) {
        bar.hidden = true;
        pending.hidden = true;
        unknown.hidden = false;
        return;
      }
      bar.hidden = false;
      unknown.hidden = true;
      var total = typeof disk.total_bytes === "number" ? disk.total_bytes : (disk.total_gb || 0) * 1e9;
      var free = typeof disk.free_bytes === "number" ? disk.free_bytes : (disk.free_gb || 0) * 1e9;
      var categories = disk.categories && typeof disk.categories === "object" ? disk.categories : null;
      pending.hidden = !!categories;
      at.textContent = disk.at
        ? t("dash.storage_asof", { time: ctx.timeText(disk.at) || disk.at })
        : "";
      var parts = [];
      var used = 0;
      if (categories) {
        STORAGE_KINDS.forEach(function (pair) {
          var size = categories[pair[0]];
          if (typeof size !== "number" || !isFinite(size) || size < 0) size = 0;
          parts.push({
            cls: "storage-" + pair[0],
            label: t(L.kindKey(pair[1]) || "dash.storage_other"),
            size: size
          });
          used += size;
        });
        var downloads = typeof disk.downloads === "number" && disk.downloads > 0 ? disk.downloads : 0;
        var other = typeof disk.other === "number" && disk.other > 0 ? disk.other : 0;
        parts.push({ cls: "storage-downloads", label: t("dash.storage_downloads"), size: downloads });
        parts.push({ cls: "storage-other", label: t("dash.storage_other"), size: other });
        used += downloads + other;
      }
      var denom = Math.max(total, used, 1);
      parts.forEach(function (part) {
        var seg = el("span", "storage-seg " + part.cls);
        /* CSSOM, not a style attribute: the CSP (`style-src 'self'`) blocks inline styles. */
        seg.style.width = storageWidth(part.size, denom);
        bar.appendChild(seg);
        legend.appendChild(storageItem(part.cls, part.label, part.size));
      });
      var percent = total > 0 ? Math.round(free * 100 / total) : 0;
      var freeLine = el("li", "storage-item storage-free");
      freeLine.appendChild(el("span", "storage-swatch storage-free"));
      freeLine.appendChild(el("span", "storage-name", t("dash.storage_free", {
        free: L.formatBytes(free, lang()) || "—",
        total: L.formatBytes(total, lang()) || "—",
        percent: percent
      })));
      legend.appendChild(freeLine);
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
      $("dash-error").textContent = dashFailed || jobsFailed ? t("dash.load_error") : "";
      renderStatus();
      renderJobsSummary();
      renderAlertSummary();
      var active = $("dash-active-list");
      active.textContent = "";
      if (dashData) {
        dashData.active.forEach(function (job) { active.appendChild(activeRow(job)); });
        renderBacklogs(dashData.backlogs);
        renderVpn(dashData.vpn);
        renderDeploy(dashData.deploy);
        renderStorage(dashData.disk);
      }
      $("dash-active-empty").hidden = !dashData || dashData.active.length > 0;
      if (jobsData) {
        renderFinished();
      } else {
        $("dash-recent-list").textContent = "";
        $("dash-recent-empty").hidden = true;
        renderRecentTools();
      }
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
      var dashboard = ctx.getJson("/api/dashboard").then(function (data) {
        dashData = data;
        dashFailed = false;
      }).catch(function () { dashFailed = true; });
      var vpn = ctx.getJson("/api/vpn").then(function (data) {
        vpnStatus = data;
        vpnFailed = false;
      }).catch(function () { vpnFailed = true; });
      var jobs = ctx.getJson("/api/jobs?limit=50").then(function (data) {
        jobsData = data;
        jobsFailed = false;
      }).catch(function () { jobsFailed = true; });
      return Promise.all([dashboard, vpn, jobs]).then(renderDashboard);
    }

    /* ------------------------------------------------------------ the page */

    function setCurrent(node, on) {
      if (on) node.setAttribute("aria-current", "page");
      else node.removeAttribute("aria-current");
    }

    /* The dashboard's own tabs: overview, jobs, VPN and alerts (#62). */
    function currentDashTab() {
      var hash = root.location.hash || "";
      if (hash.indexOf("#alert-") === 0 || hash === "#alerts") {
        return ctx.isAdmin && !ctx.isAdmin() ? "overview" : "alerts";
      }
      if (hash === "#jobs") return "jobs";
      if (hash === "#vpn") return "vpn";
      return "overview";
    }

    function applyDashTab() {
      var tab = currentDashTab();
      document.querySelectorAll("[data-dash-view]").forEach(function (node) {
        node.hidden = node.dataset.dashView !== tab;
      });
      document.querySelectorAll("[data-dash-tab]").forEach(function (node) {
        setCurrent(node, node.dataset.dashTab === tab);
      });
      return tab;
    }

    /* The tab of the page you are on. */
    function renderNav() {
      setCurrent($("nav-input"), view === "input");
      setCurrent($("nav-dashboard"), view === "dashboard");
      setCurrent($("nav-subs"), view === "subscriptions");
      setCurrent($("nav-accounts"), view === "accounts");
    }

    function applyView() {
      document.querySelectorAll("[data-view]").forEach(function (node) {
        node.hidden = node.dataset.view !== view;
      });
      renderNav();
    }

    function render() {
      applyView();
      if (view === "dashboard") applyDashTab();
      renderStrip();
      if (view === "dashboard") {
        renderDashboard();
        renderAlertLog();
      }
    }

    function start() {
      $("alert-strip-ack").addEventListener("click", acknowledgeAll);
      $("dash-alerts-ack").addEventListener("click", acknowledgeAll);
      $("dash-dismiss-all").addEventListener("click", dismissAll);
      $("dash-show-dismissed").addEventListener("click", toggleDismissed);
      $("dash-limit").addEventListener("click", toggleLimit);
      root.addEventListener("hashchange", function () {
        focused = null;
        /* Input and subscriptions are one address with two hashes: switch without a reload. */
        var next = L.pageView(root.location.pathname, root.location.hash);
        if (next !== view) {
          view = next;
          applyView();
          if (root.scrollTo) root.scrollTo(0, 0);
        }
        renderNav();
        applyDashTab();
        renderAlertLog();
      });
      applyView();
      applyDashTab();
      refreshAlerts();
      if (view !== "dashboard") return;
      refreshDashboard();
      root.setInterval(function () {
        if (document.visibilityState !== "hidden") refreshDashboard();
      }, DASH_POLL_MS);
      document.addEventListener("visibilitychange", function () {
        if (document.visibilityState === "visible") refreshDashboard();
      });
    }

    return { start: start, render: render, refreshAlerts: refreshAlerts, view: function () { return view; } };
  }

  root.Dash = { create: create };
})(window);
