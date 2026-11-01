/* The submission form (issue #25): vanilla JS, no libraries, no external requests. */
(function () {
  "use strict";

  var L = window.Logic;
  var $ = function (id) { return document.getElementById(id); };
  var I18N = JSON.parse($("i18n").textContent);
  var lang = "en";
  var probeTimer = null;
  var probeSerial = 0;

  /* ---------------------------------------------------------------- language */

  function t(key, vars) {
    var table = I18N[lang] || I18N.en;
    var text = table[key] != null ? table[key] : (I18N.en[key] != null ? I18N.en[key] : key);
    return vars ? L.format(text, vars) : text;
  }

  function chooseLanguage() {
    var saved = null;
    try { saved = window.localStorage.getItem("ytp-lang"); } catch (e) { saved = null; }
    if (saved === "de" || saved === "en") return saved;
    return /^de/i.test(navigator.language || "") ? "de" : "en";
  }

  function applyLanguage() {
    document.documentElement.lang = lang === "de" ? "de" : "en-GB";
    document.querySelectorAll("[data-i18n]").forEach(function (el) { el.textContent = t(el.dataset.i18n); });
    document.querySelectorAll("[data-i18n-placeholder]").forEach(function (el) {
      el.placeholder = t(el.dataset.i18nPlaceholder);
    });
    $("lang").textContent = lang === "de" ? "EN" : "DE";
    $("lang").setAttribute("aria-label", lang === "de" ? "Switch to English" : "Auf Deutsch umstellen");
    refreshVpnText();
    renderJobs();
  }

  /* ---------------------------------------------------------------- state */

  function mode() { return document.querySelector("input[name=mode]:checked").value; }
  function mediaType() { return $("media-type").value; }

  function setMode(value) {
    $("mode-" + value).checked = true;
    $("youtube-fields").hidden = value !== "youtube";
    $("magnet-fields").hidden = value !== "magnet";
    clearErrors();
  }

  function showType() {
    var type = mediaType();
    document.querySelectorAll(".type-fields").forEach(function (box) { box.hidden = box.dataset.type !== type; });
    clearErrors();
  }

  function values() {
    var type = mediaType();
    var box = document.querySelector('.type-fields[data-type="' + type + '"]');
    var result = { pack: $("pack").checked };
    box.querySelectorAll("[data-field]").forEach(function (input) { result[input.dataset.field] = input.value; });
    return result;
  }

  /* ---------------------------------------------------------------- errors */

  function fieldElement(field) {
    if (field === "link") return $("link");
    if (mode() === "youtube") return field === "channel" ? $("f-channel") : field === "title" ? $("f-yt-title") : null;
    var box = document.querySelector('.type-fields[data-type="' + mediaType() + '"]');
    return box.querySelector('[data-field="' + field + '"]');
  }

  function errorElement(input) { return input.id === "link" ? $("link-error") : $(input.id + "-error"); }

  function clearErrors() {
    document.querySelectorAll("input[aria-invalid]").forEach(function (input) { input.removeAttribute("aria-invalid"); });
    document.querySelectorAll(".error").forEach(function (el) { el.textContent = ""; });
    $("result").textContent = "";
    $("result").className = "result";
  }

  function message(item) {
    var key = "err." + (typeof item === "string" ? item.replace(/^err\./, "") : item.type);
    var vars = { max: new Date().getFullYear() + 1 };
    if (I18N[lang][key] != null) return t(key, vars);
    return item.msg ? item.msg : t("err.other");
  }

  function showFieldErrors(errors) {
    var first = null;
    var general = false;
    Object.keys(errors).forEach(function (field) {
      var input = fieldElement(field);
      if (!input) { general = true; return; }
      input.setAttribute("aria-invalid", "true");
      errorElement(input).textContent = message(errors[field]);
      first = first || input;
    });
    if (first) first.focus();
    return { general: general, any: !!first };
  }

  function showResult(text, ok) {
    $("result").textContent = text;
    $("result").className = "result " + (ok ? "ok" : "bad");
  }

  /* ---------------------------------------------------------------- the server */

  function post(path, body) {
    return fetch(path, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify(body),
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        return { status: response.status, data: data };
      });
    });
  }

  function statusText(status) { return t("status." + status); }

  /* ---------------------------------------------------------------- YouTube probe */

  function scheduleProbe() {
    window.clearTimeout(probeTimer);
    var link = $("link").value;
    if (mode() !== "youtube" || !L.looksLikeVideoUrl(link)) { $("probe-status").textContent = ""; return; }
    probeTimer = window.setTimeout(function () { runProbe(link); }, 500);
  }

  function runProbe(link) {
    var serial = ++probeSerial;
    $("probe-status").textContent = t("probe.looking");
    post("/api/youtube/probe", { url: link.trim() }).then(function (reply) {
      if (serial !== probeSerial) return; // a newer paste replaced this one
      if (reply.status === 200) {
        $("f-channel").value = reply.data.channel || "";
        $("f-yt-title").value = reply.data.title || "";
        $("probe-status").textContent = t("probe.ok");
        $("probe-duration").textContent = reply.data.duration_text ? t("probe.duration", { value: reply.data.duration_text }) : "";
        return;
      }
      $("probe-duration").textContent = "";
      if (reply.status === 422) {
        var errors = L.serverErrors(reply.data.detail);
        $("probe-status").textContent = "";
        showFieldErrors(errors);
        return;
      }
      var kind = reply.data && reply.data.kind;
      $("probe-status").textContent = (kind && t("kind." + kind) !== "kind." + kind ? t("kind." + kind) : t("result.failed")) + " " + t("probe.manual");
    }).catch(function () {
      if (serial === probeSerial) $("probe-status").textContent = t("result.network") + " " + t("probe.manual");
    });
  }

  /* ---------------------------------------------------------------- submit */

  function onSubmit(event) {
    event.preventDefault();
    clearErrors();
    var current = mode();
    var link = $("link").value;
    var data = current === "youtube"
      ? { channel: $("f-channel").value, title: $("f-yt-title").value }
      : values();
    var problems = L.clientErrors(current, mediaType(), link, data, new Date().getFullYear());
    if (Object.keys(problems).length) {
      showFieldErrors(problems);
      showResult(t("result.fix"), false);
      return;
    }
    var path = current === "youtube" ? "/api/youtube/download" : "/api/magnet";
    var body = current === "youtube"
      ? L.youtubePayload(link, data.channel, data.title)
      : L.magnetPayload(link, mediaType(), data);
    $("submit").disabled = true;
    post(path, body).then(function (reply) {
      $("submit").disabled = false;
      if (reply.status === 201 || reply.status === 200) {
        var vars = { id: reply.data.id, status: statusText(reply.data.status) };
        showResult(t(reply.data.duplicate ? "result.duplicate" : "result.created", vars), true);
        if (!reply.data.duplicate) resetFields();
        pollJobs();
        return;
      }
      if (reply.status === 422) {
        var shown = showFieldErrors(L.serverErrors(reply.data.detail));
        showResult(t("result.fix"), false);
        return shown;
      }
      var kind = reply.data && reply.data.kind;
      var text = kind && t("kind." + kind) !== "kind." + kind ? t("kind." + kind) : (reply.data && reply.data.detail) || t("result.failed");
      showResult(t("result.failed") + " " + (typeof text === "string" ? text : ""), false);
    }).catch(function () {
      $("submit").disabled = false;
      showResult(t("result.network"), false);
    });
  }

  function resetFields() {
    document.querySelectorAll("#form input[type=text]").forEach(function (input) { input.value = ""; });
    $("pack").checked = false;
    $("f-episode").disabled = false;
    $("probe-status").textContent = "";
    $("probe-duration").textContent = "";
  }

  /* ---------------------------------------------------------------- VPN bar (#28) */

  var VPN_POLL_MS = 5000;
  var vpnData = null;       // last answer of /api/vpn; null = never answered
  var vpnStale = false;     // the last request failed: what is shown may be old
  var baseTitle = document.title;

  function refreshVpnText() {
    var info = L.vpnView(vpnStale ? null : vpnData);
    var bar = $("vpn");
    bar.className = "vpn-bar vpn-" + info.view + (vpnStale ? " vpn-stale" : "");
    bar.setAttribute("role", info.alert ? "alert" : "status");
    $("vpn-text").textContent = vpnStale ? t("vpn.stale") : t("vpn." + info.view);
    var parts = [];
    if (!vpnStale && ["down", "mismatch", "blind", "starting", "unknown"].indexOf(info.view) >= 0) {
      parts.push(t("vpn.d." + info.view));
    }
    if (!vpnStale && info.paused) parts.push(t("vpn.paused"));
    if (!vpnStale && info.since && info.view !== "unknown") {
      var age = L.age(info.since, Date.now());
      if (age) parts.push(t("vpn.since", { when: age.unit === "now" ? t("time.now") : t("time." + age.unit, { n: age.n }) }));
    }
    if (info.country) parts.push(t("vpn.country", { country: info.country }));
    $("vpn-detail").textContent = parts.join(" · ");
    // The tab shows it too, for when the page is in the background.
    document.title = (info.alert || vpnStale ? "\u26a0 " : "") + baseTitle;
    $("favicon").setAttribute("href", info.alert ? "/static/favicon-alert.svg" : "/static/favicon.svg");
  }

  function pollVpn() {
    fetch("/api/vpn", { credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (response) {
        if (!response.ok) throw new Error("status " + response.status);
        return response.json();
      })
      .then(function (data) { vpnData = data; vpnStale = false; })
      .catch(function () { vpnStale = true; })
      .then(refreshVpnText);
  }

  /* ---------------------------------------------------------------- job list (#26) */

  var JOBS_POLL_MS = 5000;
  var jobsData = null;        // the last good answer: {jobs, counts}
  var jobsOnlyFailed = false;
  var jobsLoading = false;
  var jobsLoadFailed = false;
  var retrying = {};          // job id -> true while its retry request runs
  var retryErrors = {};       // job id -> text of the last failed retry

  function jobsUrl() {
    return "/api/jobs?limit=50" + (jobsOnlyFailed ? "&status=failed" : "");
  }

  function pollJobs() {
    if (jobsLoading) return;
    jobsLoading = true;
    fetch(jobsUrl(), { credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (response) {
        if (!response.ok) throw new Error("status " + response.status);
        return response.json();
      })
      .then(function (data) { jobsData = data; jobsLoadFailed = false; })
      .catch(function () { jobsLoadFailed = true; })
      .then(function () { jobsLoading = false; renderJobs(); });
  }

  function timeText(iso) {
    var age = L.age(iso, Date.now());
    if (!age) return "";
    return age.unit === "now" ? t("time.now") : t("time." + age.unit, { n: age.n });
  }

  function jobRow(job) {
    var li = document.createElement("li");
    li.className = "job job-" + job.status;

    var top = document.createElement("div");
    top.className = "job-top";
    var name = document.createElement("span");
    name.className = "job-name";
    name.textContent = job.label;
    var badge = document.createElement("span");
    badge.className = "job-status";
    badge.textContent = statusText(job.status);
    top.appendChild(name);
    top.appendChild(badge);
    li.appendChild(top);

    var meta = document.createElement("div");
    meta.className = "job-meta";
    var key = L.kindKey(job.kind);
    var parts = [t("jobs.id", { id: job.id })];
    if (key) parts.push(t(key));
    var when = timeText(job.updated_at);
    if (when) parts.push(when);
    meta.textContent = parts.join(" · ");
    li.appendChild(meta);

    if (job.progress != null && job.status !== "failed") {
      var bar = document.createElement("progress");
      bar.max = 100;
      bar.value = job.progress;
      bar.setAttribute("aria-label", t("jobs.progress", { n: job.progress }));
      li.appendChild(bar);
      var percent = document.createElement("span");
      percent.className = "job-percent";
      percent.textContent = t("jobs.progress", { n: job.progress });
      li.appendChild(percent);
    }

    if (job.status === "failed") {
      var reason = document.createElement("p");
      reason.className = "job-error";
      reason.textContent = job.error || t("jobs.no_reason");
      li.appendChild(reason);
    }
    if (retryErrors[job.id]) {
      var again = document.createElement("p");
      again.className = "job-error";
      again.textContent = retryErrors[job.id];
      li.appendChild(again);
    }
    if (job.can_retry) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "ghost small";
      button.disabled = !!retrying[job.id];
      button.textContent = t(retrying[job.id] ? "jobs.retrying" : "jobs.retry");
      button.addEventListener("click", function () { retryJob(job.id); });
      li.appendChild(button);
    }
    return li;
  }

  function renderJobs() {
    var list = $("jobs-list");
    list.textContent = "";
    $("jobs-error").textContent = jobsLoadFailed ? t("jobs.load_error") : "";
    if (!jobsData) { $("jobs-empty").hidden = true; return; }
    jobsData.jobs.forEach(function (job) { list.appendChild(jobRow(job)); });
    $("jobs-empty").hidden = jobsData.jobs.length > 0 || jobsOnlyFailed;
    var failed = jobsData.counts.failed || 0;
    var banner = $("jobs-banner");
    banner.hidden = failed === 0;
    banner.textContent = failed ? t("jobs.banner", { n: failed }) : "";
    var filter = $("jobs-filter");
    filter.hidden = failed === 0 && !jobsOnlyFailed;
    filter.textContent = jobsOnlyFailed ? t("jobs.filter_all") : t("jobs.filter_failed", { n: failed });
    filter.setAttribute("aria-pressed", jobsOnlyFailed ? "true" : "false");
  }

  function retryJob(id) {
    retrying[id] = true;
    delete retryErrors[id];
    renderJobs();
    post("/api/jobs/" + id + "/retry", {}).then(function (reply) {
      delete retrying[id];
      if (reply.status !== 200) {
        var detail = reply.data && typeof reply.data.detail === "string" ? reply.data.detail : "";
        retryErrors[id] = t("jobs.retry_failed") + (detail ? " " + detail : "");
      }
    }).catch(function () {
      delete retrying[id];
      retryErrors[id] = t("jobs.retry_failed") + " " + t("result.network");
    }).then(function () { renderJobs(); pollJobs(); });
  }

  function startJobPolling() {
    pollJobs();
    window.setInterval(function () {
      if (document.visibilityState !== "hidden") pollJobs();
    }, JOBS_POLL_MS);
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible") pollJobs();
    });
    $("jobs-filter").addEventListener("click", function () {
      jobsOnlyFailed = !jobsOnlyFailed;
      pollJobs();
    });
  }

  /* ---------------------------------------------------------------- wiring */

  function onLinkChanged() {
    var detected = L.detectMode($("link").value);
    if (detected && detected !== mode()) setMode(detected);
    scheduleProbe();
  }

  function init() {
    lang = chooseLanguage();
    applyLanguage();
    document.querySelectorAll("input[name=mode]").forEach(function (radio) {
      radio.addEventListener("change", function () { setMode(radio.value); scheduleProbe(); });
    });
    $("media-type").addEventListener("change", showType);
    $("pack").addEventListener("change", function () {
      $("f-episode").disabled = $("pack").checked;
      if ($("pack").checked) $("f-episode").value = "";
    });
    $("link").addEventListener("input", onLinkChanged);
    $("link").addEventListener("paste", function () { window.setTimeout(onLinkChanged, 0); });
    $("form").addEventListener("submit", onSubmit);
    $("reset").addEventListener("click", function () { resetFields(); clearErrors(); });
    $("lang").addEventListener("click", function () {
      lang = lang === "de" ? "en" : "de";
      try { window.localStorage.setItem("ytp-lang", lang); } catch (e) { /* private mode: fine */ }
      applyLanguage();
      clearErrors();
    });
    pollVpn();
    window.setInterval(function () {
      if (document.visibilityState !== "hidden") pollVpn();
    }, VPN_POLL_MS);
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible") pollVpn();
    });
    startJobPolling();
  }

  init();
})();
