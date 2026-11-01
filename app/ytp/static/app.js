/* The submission form (issue #25): vanilla JS, no libraries, no external requests. */
(function () {
  "use strict";

  var L = window.Logic;
  var $ = function (id) { return document.getElementById(id); };
  var I18N = JSON.parse($("i18n").textContent);
  var lang = "en";
  var probeTimer = null;
  var probeSerial = 0;
  var feedSerial = 0;

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
    renderSubs();
  }

  /* ---------------------------------------------------------------- state */

  function mode() { return document.querySelector("input[name=mode]:checked").value; }
  function mediaType() { return $("media-type").value; }

  function setMode(value) {
    $("mode-" + value).checked = true;
    $("youtube-fields").hidden = value !== "youtube";
    $("magnet-fields").hidden = value !== "magnet";
    $("podcast-fields").hidden = value !== "podcast";
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
    if (mode() === "podcast") {
      return { name: $("f-podcast"), episode: $("pc-episode"), key: $("pc-episode"), backfill: $("pc-backfill") }[field] || null;
    }
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
    var key = typeof item === "string" && /^pc\./.test(item) ? item
      : "err." + (typeof item === "string" ? item.replace(/^err\./, "") : item.type);
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
    if (mode() === "podcast" && L.looksLikeFeedUrl(link)) {
      probeTimer = window.setTimeout(function () { runFeedProbe(link); }, 600);
      return;
    }
    if (mode() !== "youtube" || !L.looksLikeVideoUrl(link)) { $("probe-status").textContent = ""; return; }
    probeTimer = window.setTimeout(function () { runProbe(link); }, 500);
  }

  /* ---------------------------------------------------------------- podcast feed (#34) */

  function fillEpisodes(episodes) {
    var select = $("pc-episode");
    while (select.options.length > 1) select.remove(1);
    episodes.forEach(function (episode) {
      var option = document.createElement("option");
      option.value = episode.key;
      option.textContent = episode.published + " \u2013 " + episode.title;
      select.appendChild(option);
    });
    select.value = "";
  }

  function runFeedProbe(link) {
    var serial = ++feedSerial;
    $("pc-status").textContent = t("pc.looking");
    post("/api/podcasts/probe", { feed: link.trim() }).then(function (reply) {
      if (serial !== feedSerial) return; // a newer paste replaced this one
      if (reply.status === 200) {
        $("f-podcast").value = reply.data.folder || reply.data.title || "";
        fillEpisodes(reply.data.episodes || []);
        $("pc-subscribe").checked = !reply.data.subscribed && !(reply.data.episodes || []).length;
        $("pc-status").textContent = reply.data.episodes.length
          ? t("pc.found", { title: reply.data.title, n: reply.data.episodes.length })
          : t("pc.found_none", { title: reply.data.title });
        return;
      }
      fillEpisodes([]);
      if (reply.status === 422) {
        $("pc-status").textContent = "";
        showFieldErrors(L.serverErrors(reply.data.detail));
        return;
      }
      $("pc-status").textContent = failureText(reply, "result.failed") + " " + t("pc.manual");
    }).catch(function () {
      if (serial === feedSerial) $("pc-status").textContent = t("result.network") + " " + t("pc.manual");
    });
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

  /* ---------------------------------------------------------------- subscribe (#19) */

  /* Ticked "subscribe" next to a YouTube download: the video's address names the channel. */
  function subscribeToChannel(link, name, downloadText) {
    var body = { channel: link.trim() };
    if (name && name.trim()) body.name = name.trim();
    post("/api/subscriptions", body).then(function (reply) {
      if (reply.status === 201 || reply.status === 200) {
        var key = reply.data.duplicate ? "sub.duplicate" : "sub.created";
        showResult(downloadText + " " + t(key, { name: reply.data.channel_name }), true);
        loadSubs();
        return;
      }
      var detail = "";
      if (reply.status === 422) {
        var errors = L.serverErrors(reply.data.detail);
        detail = Object.keys(errors).map(function (field) { return message(errors[field]); }).join(" ");
      } else if (reply.data && typeof reply.data.detail === "string") {
        detail = reply.data.detail;
      }
      showResult(downloadText + " " + t("sub.failed") + (detail ? " " + detail : ""), false);
    }).catch(function () {
      showResult(downloadText + " " + t("sub.failed") + " " + t("result.network"), false);
    });
  }

  /* ---------------------------------------------------------------- submit */

  function onSubmit(event) {
    event.preventDefault();
    clearErrors();
    var current = mode();
    var link = $("link").value;
    if (current === "podcast") { submitPodcast(link); return; }
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
        var text = t(reply.data.duplicate ? "result.duplicate" : "result.created", vars);
        var wantSubscribe = current === "youtube" && $("subscribe").checked;
        var channelName = data.channel;
        showResult(text, true);
        if (!reply.data.duplicate) resetFields();
        pollJobs();
        if (wantSubscribe) subscribeToChannel(link, channelName, text);
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

  /* Podcast: a single episode and/or a subscription, one request each (#34). */
  function submitPodcast(link) {
    var data = {
      name: $("f-podcast").value,
      episode: $("pc-episode").value,
      subscribe: $("pc-subscribe").checked,
      backfill: $("pc-backfill").value,
    };
    var problems = L.clientErrors("podcast", null, link, data, new Date().getFullYear());
    if (Object.keys(problems).length) {
      showFieldErrors(problems);
      showResult(t("result.fix"), false);
      return;
    }
    var payloads = L.podcastPayloads(link, data);
    var lines = [];
    var good = true;
    var fieldErrors = null;

    function failed(reply, fallbackKey) {
      good = false;
      if (reply.status === 422) { fieldErrors = L.serverErrors(reply.data.detail); return; }
      lines.push(failureText(reply, fallbackKey));
    }

    $("submit").disabled = true;
    var steps = Promise.resolve();
    if (payloads.episode) {
      steps = steps.then(function () {
        return post("/api/podcasts/episode", payloads.episode).then(function (reply) {
          if (reply.status === 201 || reply.status === 200) {
            var vars = { id: reply.data.id, status: statusText(reply.data.status) };
            lines.push(t(reply.data.duplicate ? "pc.queued_again" : "pc.queued", vars));
          } else {
            failed(reply, "result.failed");
          }
        });
      });
    }
    if (payloads.subscribe) {
      steps = steps.then(function () {
        return post("/api/podcasts", payloads.subscribe).then(function (reply) {
          if (reply.status === 201 || reply.status === 200) {
            lines.push(t(reply.data.duplicate ? "pc.already" : "pc.subscribed", { name: reply.data.title }));
          } else {
            failed(reply, "pc.sub_failed");
          }
        });
      });
    }
    steps.then(function () {
      $("submit").disabled = false;
      if (fieldErrors) { showFieldErrors(fieldErrors); lines.push(t("result.fix")); }
      showResult(lines.join(" "), good);
      if (good) resetFields();
      pollJobs();
      loadSubs();
    }).catch(function () {
      $("submit").disabled = false;
      showResult(lines.concat([t("result.network")]).join(" "), false);
    });
  }

  function resetFields() {
    document.querySelectorAll("#form input[type=text]").forEach(function (input) { input.value = ""; });
    $("pack").checked = false;
    $("subscribe").checked = false;
    $("pc-subscribe").checked = false;
    $("pc-backfill").value = "0";
    fillEpisodes([]);
    $("pc-status").textContent = "";
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

  /* ---------------------------------------------------------------- subscriptions (#27) */

  var SUBS_POLL_MS = 30000;
  var subsData = null;
  var subsLoading = false;
  var subsLoadFailed = false;
  var subsBusy = {};          // id -> "poll" | "remove" while a request runs
  var subsConfirm = {};       // id -> true after the first click on Remove
  var subsMessages = {};      // id -> {text, ok} shown under the row

  /* Channels and podcasts share one list; the kind says which API a row belongs to. */
  function subKey(sub) { return sub.kind + ":" + sub.id; }
  function subPath(sub) { return (sub.kind === "podcast" ? "/api/podcasts/" : "/api/subscriptions/") + sub.id; }
  function subName(sub) { return sub.kind === "podcast" ? sub.title : sub.channel_name; }

  function getJson(path) {
    return fetch(path, { credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (response) {
        if (!response.ok) throw new Error("status " + response.status);
        return response.json();
      });
  }

  function loadSubs() {
    if (subsLoading) return;
    subsLoading = true;
    Promise.all([getJson("/api/subscriptions"), getJson("/api/podcasts")])
      .then(function (answers) {
        var channels = answers[0].subscriptions.map(function (sub) { sub.kind = "channel"; return sub; });
        var podcasts = answers[1].podcasts;   // these carry kind: "podcast" already
        subsData = channels.concat(podcasts).sort(function (a, b) {
          return subName(a).toLowerCase().localeCompare(subName(b).toLowerCase());
        });
        subsLoadFailed = false;
      })
      .catch(function () { subsLoadFailed = true; })
      .then(function () { subsLoading = false; renderSubs(); });
  }

  function subsRequest(method, path) {
    return fetch(path, {
      method: method,
      credentials: "same-origin",
      headers: { "Accept": "application/json" },
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        return { status: response.status, data: data };
      });
    });
  }

  function failureText(reply, fallbackKey) {
    var kind = reply.data && reply.data.kind;
    var known = kind && t("kind." + kind) !== "kind." + kind;
    var detail = reply.data && typeof reply.data.detail === "string" ? reply.data.detail : "";
    return t(fallbackKey) + (known ? " " + t("kind." + kind) : detail ? " " + detail : "");
  }

  function subRow(sub) {
    var li = document.createElement("li");
    li.className = "job" + (sub.last_error ? " job-failed" : "");

    var top = document.createElement("div");
    top.className = "job-top";
    var name = document.createElement("span");
    name.className = "job-name";
    name.textContent = subName(sub);
    top.appendChild(name);
    li.appendChild(top);

    var meta = document.createElement("div");
    meta.className = "job-meta";
    meta.textContent = t(sub.kind === "podcast" ? "subs.podcast" : "subs.channel_tag") + " \u00b7 " + (sub.last_checked
      ? t("subs.checked", { when: timeText(sub.last_checked) })
      : t("subs.never"));
    li.appendChild(meta);

    if (sub.last_error) {
      var problem = document.createElement("p");
      problem.className = "job-error";
      problem.textContent = sub.last_error;
      li.appendChild(problem);
    }
    var rowKey = subKey(sub);
    if (subsMessages[rowKey]) {
      var note = document.createElement("p");
      note.className = subsMessages[rowKey].ok ? "job-note" : "job-error";
      note.textContent = subsMessages[rowKey].text();
      li.appendChild(note);
    }

    var buttons = document.createElement("div");
    buttons.className = "row-actions";
    var busy = subsBusy[rowKey];
    var poll = document.createElement("button");
    poll.type = "button";
    poll.className = "ghost small";
    poll.disabled = !!busy;
    poll.textContent = t(busy === "poll" ? "subs.polling" : "subs.poll");
    poll.addEventListener("click", function () { pollSub(sub); });
    var remove = document.createElement("button");
    remove.type = "button";
    remove.className = "ghost small" + (subsConfirm[rowKey] ? " danger" : "");
    remove.disabled = !!busy;
    remove.textContent = t(subsConfirm[rowKey] ? "subs.confirm" : "subs.remove");
    remove.addEventListener("click", function () { removeSub(sub); });
    buttons.appendChild(poll);
    buttons.appendChild(remove);
    li.appendChild(buttons);
    return li;
  }

  function renderSubs() {
    var list = $("subs-list");
    list.textContent = "";
    $("subs-load-error").textContent = subsLoadFailed ? t("subs.load_error") : "";
    if (!subsData) { $("subs-empty").hidden = true; return; }
    subsData.forEach(function (sub) { list.appendChild(subRow(sub)); });
    $("subs-empty").hidden = subsData.length > 0;
  }

  function pollSub(sub) {
    var key = subKey(sub);
    subsBusy[key] = "poll";
    delete subsConfirm[key];
    delete subsMessages[key];
    renderSubs();
    subsRequest("POST", subPath(sub) + "/poll").then(function (reply) {
      if (reply.status === 200) {
        var n = reply.data.queued || 0;
        subsMessages[key] = { ok: true, text: function () { return n ? t("subs.polled", { n: n }) : t("subs.polled_none"); } };
        if (n) pollJobs();
      } else {
        subsMessages[key] = { ok: false, text: function () { return failureText(reply, "subs.poll_failed"); } };
      }
    }).catch(function () {
      subsMessages[key] = { ok: false, text: function () { return t("subs.poll_failed") + " " + t("result.network"); } };
    }).then(function () { delete subsBusy[key]; loadSubs(); renderSubs(); });
  }

  /* Two clicks: the first asks "Really remove?", the second does it; the question lapses. */
  function removeSub(sub) {
    var key = subKey(sub);
    if (!subsConfirm[key]) {
      subsConfirm[key] = true;
      renderSubs();
      window.setTimeout(function () {
        if (subsConfirm[key]) { delete subsConfirm[key]; renderSubs(); }
      }, 5000);
      return;
    }
    delete subsConfirm[key];
    subsBusy[key] = "remove";
    renderSubs();
    subsRequest("DELETE", subPath(sub)).then(function (reply) {
      delete subsBusy[key];
      if (reply.status === 204 || reply.status === 404) {
        delete subsMessages[key];
        $("subs-result").textContent = t("subs.removed_note");
        $("subs-result").className = "result ok";
      } else {
        subsMessages[key] = { ok: false, text: function () { return failureText(reply, "subs.remove_failed"); } };
      }
    }).catch(function () {
      delete subsBusy[key];
      subsMessages[key] = { ok: false, text: function () { return t("subs.remove_failed") + " " + t("result.network"); } };
    }).then(function () { loadSubs(); renderSubs(); });
  }

  function subsShowResult(text, ok) {
    $("subs-result").textContent = text;
    $("subs-result").className = "result " + (ok ? "ok" : "bad");
  }

  function onSubsSubmit(event) {
    event.preventDefault();
    $("subs-error").textContent = "";
    $("subs-result").textContent = "";
    $("subs-result").className = "result";
    $("subs-channel").removeAttribute("aria-invalid");
    var channel = $("subs-channel").value.trim();
    var backfillText = $("subs-backfill").value.trim() || "0";
    if (!channel) {
      $("subs-channel").setAttribute("aria-invalid", "true");
      $("subs-error").textContent = t("err.required");
      $("subs-channel").focus();
      return;
    }
    if (!/^\d+$/.test(backfillText) || +backfillText > 15) {
      $("subs-error").textContent = t("err.int_out_of_range");
      $("subs-backfill").focus();
      return;
    }
    $("subs-add").disabled = true;
    post("/api/subscriptions", { channel: channel, backfill: +backfillText }).then(function (reply) {
      $("subs-add").disabled = false;
      if (reply.status === 201 || reply.status === 200) {
        var key = reply.data.duplicate ? "subs.already" : "subs.added";
        subsShowResult(t(key, { name: reply.data.channel_name }), true);
        if (!reply.data.duplicate) { $("subs-channel").value = ""; $("subs-backfill").value = "0"; }
        loadSubs();
        return;
      }
      if (reply.status === 422) {
        var errors = L.serverErrors(reply.data.detail);
        $("subs-error").textContent = Object.keys(errors).map(function (field) { return message(errors[field]); }).join(" ");
        $("subs-channel").setAttribute("aria-invalid", "true");
        return;
      }
      subsShowResult(failureText(reply, "subs.add_failed"), false);
    }).catch(function () {
      $("subs-add").disabled = false;
      subsShowResult(t("subs.add_failed") + " " + t("result.network"), false);
    });
  }

  function startSubs() {
    loadSubs();
    $("subs-form").addEventListener("submit", onSubsSubmit);
    window.setInterval(function () {
      if (document.visibilityState !== "hidden") loadSubs();
    }, SUBS_POLL_MS);
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible") loadSubs();
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
    startSubs();
  }

  init();
})();
