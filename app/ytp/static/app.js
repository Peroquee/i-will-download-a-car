/* The submission form (issue #25): vanilla JS, no libraries, no external requests. */
(function () {
  "use strict";

  var L = window.Logic;
  var $ = function (id) { return document.getElementById(id); };
  var I18N = JSON.parse($("i18n").textContent);
  var lang = "en";
  var probeTimer = null;
  var probeSerial = 0;
  var probed = { link: "", state: "" };   // the last address looked up: "pending", "ok" or "failed"
  var feedSerial = 0;
  var dash = null;   // the alert strip and the dashboard (dashboard.js, #38)
  var auth = null;   // sign-in, account menu, accounts page (auth.js, #44)
  var catalogue = null;       // what "all episodes" would do for the feed in the form (#45)
  var allConfirmed = false;   // the question "download N episodes?" has been shown

  /* A form of this page is NEVER submitted natively (it would send the pasted link in the address
     and create no job): whatever else fails, this guard is the first thing that runs. */
  document.addEventListener("submit", function (event) { event.preventDefault(); }, true);

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
    document.querySelectorAll("[data-i18n-label]").forEach(function (el) {
      el.setAttribute("aria-label", t(el.dataset.i18nLabel));
    });
    document.querySelectorAll("[data-i18n-placeholder]").forEach(function (el) {
      el.placeholder = t(el.dataset.i18nPlaceholder);
    });
    $("lang").textContent = lang === "de" ? "EN" : "DE";
    applyTarget();
    $("lang").setAttribute("aria-label", lang === "de" ? "Switch to English" : "Auf Deutsch umstellen");
    refreshVpnText();
    showCatalogueNote();
    hideAllConfirm();
    renderJobsMini();
    renderSubs();
    if (dash) dash.render();
    if (auth) auth.render();
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
    if (mode() === "youtube") {
      if (field === "backfill") return $("yt-backfill");
      return { channel: $("f-channel"), title: $("f-yt-title"), album: $("f-yt-album") }[field] || null;
    }
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

  /* Every request to the app goes through here (#44): nothing is sent while nobody is signed in,
     and an answer of 401 means the session ended, so the page shows the sign-in form again. */
  function api(path, options) {
    if (auth && !auth.isIn() && !/^\/api\/auth\//.test(path)) return Promise.reject(new Error("not signed in"));
    return fetch(path, options).then(function (response) {
      if (response.status === 401 && auth && !/^\/api\/auth\/login/.test(path)) auth.expired();
      return response;
    });
  }

  function request(method, path, body) {
    var headers = { "Accept": "application/json" };
    var options = { method: method, credentials: "same-origin", headers: headers };
    if (body !== null && body !== undefined) {
      headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    return api(path, options).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        return { status: response.status, data: data };
      });
    });
  }

  function post(path, body) { return request("POST", path, body); }

  function statusText(status) { return t("status." + status); }

  /* ---------------------------------------------------------------- YouTube probe */

  function scheduleProbe() {
    window.clearTimeout(probeTimer);
    var link = $("link").value;
    if (mode() === "podcast" && L.looksLikeFeedUrl(link)) {
      probeTimer = window.setTimeout(function () { runFeedProbe(link); }, 600);
      return;
    }
    if (mode() !== "youtube" || !L.looksLikeVideoUrl(link)) { showProbe("", false); probed = { link: "", state: "" }; return; }
    /* The same address is not asked twice while the first answer is pending or good (paste,
       input, change and blur all arrive for one paste); after a failure a later change or blur
       asks again. */
    if (probed.link === link.trim() && probed.state !== "failed") return;
    probeTimer = window.setTimeout(function () { runProbe(link); }, 500);
  }

  /* The answer of the lookup, under the address field; a failure in red. */
  function showProbe(text, failed) {
    var node = $("probe-status");
    node.textContent = text;
    node.className = "note probe-status" + (failed ? " probe-failed" : "");
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

  /* "All episodes" (#45): what it would take, as text. */
  function catalogueVars() {
    var size = catalogue && catalogue.size
      ? t("pc.all_size", { size: L.formatBytes(catalogue.size, lang) })
      : t("pc.all_size_unknown");
    return { n: catalogue ? catalogue.to_download : 0, size: size, skipped: catalogue && catalogue.skipped ? t("pc.all_skipped", { n: catalogue.skipped }) : "" };
  }

  function showCatalogueNote() {
    var note = $("pc-all-note");
    if (!catalogue) { note.textContent = ""; return; }
    note.textContent = catalogue.to_download
      ? t("pc.all_note", catalogueVars())
      : t("pc.all_none");
  }

  function hideAllConfirm() {
    allConfirmed = false;
    $("pc-all-confirm").hidden = true;
    $("pc-all-confirm").textContent = "";
  }

  function runFeedProbe(link) {
    var serial = ++feedSerial;
    catalogue = null;
    hideAllConfirm();
    $("pc-status").textContent = t("pc.looking");
    post("/api/podcasts/probe", { feed: link.trim() }).then(function (reply) {
      if (serial !== feedSerial) return; // a newer paste replaced this one
      if (reply.status === 200) {
        $("f-podcast").value = reply.data.folder || reply.data.title || "";
        fillEpisodes(reply.data.episodes || []);
        catalogue = reply.data.catalogue || null;
        showCatalogueNote();
        $("pc-subscribe").checked = !reply.data.subscribed && !(reply.data.episodes || []).length;
        $("pc-status").textContent = reply.data.episodes.length
          ? t("pc.found", { title: reply.data.title, n: reply.data.episodes.length })
          : t("pc.found_none", { title: reply.data.title });
        return;
      }
      fillEpisodes([]);
      showCatalogueNote();
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
    probed = { link: link.trim(), state: "pending" };
    showProbe(t("probe.looking"), false);
    post("/api/youtube/probe", { url: link.trim() }).then(function (reply) {
      if (serial !== probeSerial) return; // a newer paste replaced this one
      if (reply.status === 200) {
        probed.state = "ok";
        $("f-channel").value = reply.data.channel || "";
        $("f-yt-title").value = reply.data.title || "";
        showProbe(t("probe.ok"), false);
        $("probe-duration").textContent = reply.data.duration_text ? t("probe.duration", { value: reply.data.duration_text }) : "";
        return;
      }
      probed.state = "failed";
      $("probe-duration").textContent = "";
      if (reply.status === 422) {
        showProbe("", false);
        showFieldErrors(L.serverErrors(reply.data.detail));
        return;
      }
      /* Why it failed, in words, and that channel and title can be typed in by hand. */
      var kind = reply.data && reply.data.kind;
      var reason = kind && t("kind." + kind) !== "kind." + kind ? t("kind." + kind) : t("probe.failed");
      showProbe(reason + " " + t("probe.manual"), true);
    }).catch(function () {
      if (serial !== probeSerial) return;
      probed.state = "failed";
      showProbe(t("result.network") + " " + t("probe.manual"), true);
    });
  }

  /* ---------------------------------------------------------------- YouTube target (#35) */

  function ytTarget() { return $("yt-target").value; }
  function ytAudioOnly() { return ytTarget() !== "video" && $("yt-audio").checked; }

  /* The same two fields mean channel/title for a video and podcast/episode for an episode. */
  var shownTarget = "video";
  var TARGET_TEXT = {
    video: ["yt.channel", "yt.title", "yt.subscribe_note"],
    podcast: ["yt.podcast", "yt.episode", "yt.subscribe_note_podcast"],
    music: ["yt.artist", "yt.track", "yt.subscribe_note"],
  };

  function applyTarget() {
    var target = ytTarget();
    $("yt-audio-row").hidden = target === "video";
    $("yt-album-row").hidden = target !== "music";
    $("yt-subscribe-row").hidden = target === "music";   // channels are not subscribed as music
    if (target === "video") $("yt-audio").checked = false;
    if (target === "music") $("subscribe").checked = false;
    showBackfill();
    // A music track is audio by default (a video file does not belong in a music library).
    if (target === "music" && shownTarget !== "music") $("yt-audio").checked = true;
    if (target === "podcast" && shownTarget === "music") $("yt-audio").checked = false;
    shownTarget = target;
    var text = TARGET_TEXT[target];
    var labels = [["yt-channel-label", text[0]], ["yt-title-label", text[1]], ["yt-subscribe-note", text[2]]];
    labels.forEach(function (pair) {
      $(pair[0]).dataset.i18n = pair[1];
      $(pair[0]).textContent = t(pair[1]);
    });
  }

  /* ---------------------------------------------------------------- subscribe (#19) */

  /* Subscribing is offered for a video or a podcast episode, not for music (applyTarget). */
  function wantsSubscription() { return ytTarget() !== "music" && $("subscribe").checked; }

  /* "Older videos" only means something while "subscribe" is ticked. */
  function showBackfill() { $("yt-backfill-row").hidden = !$("subscribe").checked; }

  /* Ticked "subscribe" next to a YouTube download: the video's address names the channel, and
     "older videos" says how many of the channel's latest videos to take as well (0 = none). */
  function subscribeToChannel(link, name, backfill, downloadText) {
    var body = L.subscribePayload(link, name, backfill, ytTarget(), ytAudioOnly());
    post("/api/subscriptions", body).then(function (reply) {
      if (reply.status === 201 || reply.status === 200) {
        var key = reply.data.duplicate ? (reply.data.other_followers > 0 ? "sub.joined" : "sub.duplicate") : "sub.created";
        showResult(downloadText + " " + t(key, { name: reply.data.channel_name, n: reply.data.other_followers }), true);
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
      ? {
        channel: $("f-channel").value, title: $("f-yt-title").value, album: $("f-yt-album").value,
        subscribe: wantsSubscription(), backfill: $("yt-backfill").value,
      }
      : values();
    var problems = L.clientErrors(current, mediaType(), link, data, new Date().getFullYear());
    if (Object.keys(problems).length) {
      showFieldErrors(problems);
      showResult(t("result.fix"), false);
      return;
    }
    var path = current === "youtube" ? "/api/youtube/download" : "/api/magnet";
    var body = current === "youtube"
      ? L.youtubePayload(link, data.channel, data.title, ytTarget(), ytAudioOnly(), data.album)
      : L.magnetPayload(link, mediaType(), data);
    $("submit").disabled = true;
    post(path, body).then(function (reply) {
      $("submit").disabled = false;
      if (reply.status === 201 || reply.status === 200) {
        var vars = { id: reply.data.id, status: statusText(reply.data.status) };
        var text = t(reply.data.duplicate ? "result.duplicate" : "result.created", vars);
        var wantSubscribe = current === "youtube" && wantsSubscription();
        var channelName = data.channel;
        var backfill = +(String(data.backfill || "").trim() || "0");
        showResult(text, true);
        if (!reply.data.duplicate) resetFields();
        pollJobs();
        if (wantSubscribe) subscribeToChannel(link, channelName, backfill, text);
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
      all: $("pc-all").checked,
    };
    var problems = L.clientErrors("podcast", null, link, data, new Date().getFullYear());
    if (Object.keys(problems).length) {
      showFieldErrors(problems);
      showResult(t("result.fix"), false);
      return;
    }
    // "All episodes" asks first: how many, how much, and only the second press starts it.
    if (data.all && catalogue && !allConfirmed) {
      var confirm = $("pc-all-confirm");
      if (!catalogue.space_ok) {
        confirm.textContent = t("pc.all_nospace", catalogueVars());
        confirm.hidden = false;
        return;
      }
      if (catalogue.to_download) {
        confirm.textContent = t("pc.all_confirm", catalogueVars());
        confirm.hidden = false;
        allConfirmed = true;
        return;
      }
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
            var podcastKey = reply.data.duplicate ? (reply.data.other_followers > 0 ? "pc.joined" : "pc.already") : "pc.subscribed";
            lines.push(t(podcastKey, { name: reply.data.title, n: reply.data.other_followers }));
          } else {
            failed(reply, "pc.sub_failed");
          }
        });
      });
    }
    if (payloads.backlog) {
      steps = steps.then(function () {
        return post("/api/podcasts/backlog", payloads.backlog).then(function (reply) {
          if (reply.status === 201 || reply.status === 200) {
            if (reply.data.joined) lines.push(t("pc.all_joined"));
            else if (!reply.data.started) lines.push(t("pc.all_nothing"));
            else lines.push(t("pc.all_started", { n: reply.data.backlog.total }));
          } else {
            failed(reply, "pc.all_failed");
          }
        });
      });
    }
    steps.then(function () {
      $("submit").disabled = false;
      hideAllConfirm();
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
    $("yt-backfill").value = "0";
    showBackfill();
    $("yt-target").value = "video";
    shownTarget = "video";
    applyTarget();
    $("pc-subscribe").checked = false;
    $("pc-backfill").value = "0";
    $("pc-all").checked = false;
    catalogue = null;
    showCatalogueNote();
    hideAllConfirm();
    fillEpisodes([]);
    $("pc-status").textContent = "";
    $("f-episode").disabled = false;
    showProbe("", false);
    probed = { link: "", state: "" };
    $("probe-duration").textContent = "";
  }

  /* ---------------------------------------------------------------- VPN bar (#28) */

  var VPN_POLL_MS = 5000;
  var vpnData = null;       // last answer of /api/vpn; null = never answered
  var vpnStale = false;     // the last request failed: what is shown may be old
  var baseTitle = document.title;

  function refreshVpnText() {
    var info = L.vpnView(vpnStale ? null : vpnData);
    var label = vpnStale ? t("vpn.stale") : t("vpn." + info.view);
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
    var detail = parts.join(" · ");
    // The dot never changes size, so loading the state only changes its colour (#61).
    var indicator = $("vpn-indicator");
    indicator.className = "vpn-indicator vpn-" + info.view + (vpnStale ? " vpn-stale" : "");
    indicator.setAttribute("aria-label", label + (detail ? " · " + detail : ""));
    indicator.title = indicator.getAttribute("aria-label");
    // Only a real problem gets the unmissable banner at the top.
    var banner = $("vpn");
    banner.hidden = !info.alert;
    banner.className = "vpn-banner vpn-" + info.view + (vpnStale ? " vpn-stale" : "");
    banner.setAttribute("role", info.alert ? "alert" : "status");
    $("vpn-text").textContent = label;
    $("vpn-detail").textContent = detail;
    // The tab shows it too, for when the page is in the background.
    document.title = (info.alert || vpnStale ? "\u26a0 " : "") + baseTitle;
    $("favicon").setAttribute("href", info.alert ? "/static/favicon-alert.svg" : "/static/favicon.svg");
  }
  function pollVpn() {
    api("/api/vpn", { credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (response) {
        if (!response.ok) throw new Error("status " + response.status);
        return response.json();
      })
      .then(function (data) { vpnData = data; vpnStale = false; })
      .catch(function () { vpnStale = true; })
      .then(refreshVpnText);
  }

  /* ---------------------------------------------------------------- downloads indicator */

  /* The input page shows only a one-line summary of the downloads (how many run, wait and failed,
     with the progress of the running ones); the jobs themselves are on the dashboard (#38). */
  var JOBS_POLL_MS = 5000;
  var jobsData = null;        // the last good answer: {jobs, counts}
  var jobsLoading = false;
  var jobsLoadFailed = false;

  function pollJobs() {
    if (jobsLoading) return;
    jobsLoading = true;
    api("/api/jobs?limit=50", { credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (response) {
        if (!response.ok) throw new Error("status " + response.status);
        return response.json();
      })
      .then(function (data) { jobsData = data; jobsLoadFailed = false; })
      .catch(function () { jobsLoadFailed = true; })
      .then(function () { jobsLoading = false; renderJobsMini(); });
  }

  function timeText(iso) {
    var age = L.age(iso, Date.now());
    if (!age) return "";
    return age.unit === "now" ? t("time.now") : t("time." + age.unit, { n: age.n });
  }

  function renderJobsMini() {
    var box = $("jobs-mini");
    var text = $("jobs-mini-text");
    var bar = $("jobs-mini-bar");
    if (jobsLoadFailed && !jobsData) {
      box.className = "jobs-mini jobs-mini-error";
      text.textContent = t("mini.error");
      bar.hidden = true;
      return;
    }
    if (!jobsData) { text.textContent = ""; bar.hidden = true; return; }
    var summary = L.jobsSummary(jobsData);
    var parts = [];
    if (summary.running) parts.push(t("mini.running", { n: summary.running }));
    if (summary.waiting) parts.push(t("mini.waiting", { n: summary.waiting }));
    if (summary.failed) parts.push(t("mini.failed", { n: summary.failed }));
    text.textContent = parts.length ? parts.join(" \u00b7 ") : t("mini.none");
    box.className = "jobs-mini" + (summary.running || summary.waiting ? " jobs-mini-busy" : "")
      + (summary.failed ? " jobs-mini-has-failed" : "");
    bar.hidden = summary.progress === null;
    if (summary.progress !== null) {
      bar.value = summary.progress;
      bar.setAttribute("aria-label", t("jobs.progress", { n: summary.progress }));
    }
  }

  function startJobPolling() {
    pollJobs();
    window.setInterval(function () {
      if (document.visibilityState !== "hidden") pollJobs();
    }, JOBS_POLL_MS);
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible") pollJobs();
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
    return api(path, { credentials: "same-origin", headers: { "Accept": "application/json" } })
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

  function subsRequest(method, path) { return request(method, path, null); }

  function failureText(reply, fallbackKey) {
    var kind = reply.data && reply.data.kind;
    var known = kind && t("kind." + kind) !== "kind." + kind;
    var detail = reply.data && typeof reply.data.detail === "string" ? reply.data.detail : "";
    return t(fallbackKey) + (known ? " " + t("kind." + kind) : detail ? " " + detail : "");
  }

  // "skipped: 3" with the newest few listed: live streams, premieres and Shorts the poller left
  // out on purpose (not jobs, not failures).
  function skippedDetails(skipped) {
    var details = document.createElement("details");
    details.className = "skipped";
    var summary = document.createElement("summary");
    summary.textContent = t("subs.skipped", { n: skipped.count });
    details.appendChild(summary);
    var list = document.createElement("ul");
    skipped.recent.forEach(function (item) {
      var entry = document.createElement("li");
      var reason = t("subs.skip." + item.reason);
      entry.textContent = (item.title || item.video_id) + " \u00b7 " + reason;
      list.appendChild(entry);
    });
    if (skipped.count > skipped.recent.length) {
      var more = document.createElement("li");
      more.textContent = t("subs.skipped_more", { n: skipped.count - skipped.recent.length });
      list.appendChild(more);
    }
    details.appendChild(list);
    var why = document.createElement("p");
    why.className = "job-meta";
    why.textContent = t("subs.skipped_why");
    details.appendChild(why);
    return details;
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
    var metaText = sub.last_checked
      ? t("subs.checked", { when: timeText(sub.last_checked) })
      : t("subs.never");
    if (sub.target === "podcast") {
      metaText = t(sub.audio_only ? "subs.target_audio" : "subs.target_podcast") + " \u00b7 " + metaText;
    }
    meta.textContent = metaText;
    li.appendChild(meta);
    var others = (sub.follower_count || 0) - (sub.following ? 1 : 0);
    if (sub.followers && sub.followers.length) {   // an admin sees who follows
      var who = document.createElement("div");
      who.className = "job-meta";
      who.textContent = t("subs.followers", { names: sub.followers.join(", ") });
      li.appendChild(who);
    } else if (others > 0) {
      var shared = document.createElement("div");
      shared.className = "job-meta";
      shared.textContent = t("subs.shared_note", { n: others });
      li.appendChild(shared);
    }

    if (sub.last_error) {
      var problem = document.createElement("p");
      problem.className = "job-error";
      problem.textContent = sub.last_error;
      li.appendChild(problem);
    }
    if (sub.skipped && sub.skipped.count > 0) li.appendChild(skippedDetails(sub.skipped));
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
    remove.className = "ghost small danger";
    remove.disabled = !!busy;
    remove.textContent = t(subsConfirm[rowKey] ? "subs.confirm" : "subs.remove");
    remove.addEventListener("click", function () { removeSub(sub); });
    buttons.appendChild(poll);
    buttons.appendChild(remove);
    if (auth && auth.isAdmin() && others > 0) {   // ends it for the others too
      var everyone = document.createElement("button");
      everyone.type = "button";
      everyone.className = "ghost small danger";
      everyone.disabled = !!busy;
      everyone.textContent = t(subsConfirm[rowKey + ":all"] ? "subs.confirm_all" : "subs.remove_all");
      everyone.addEventListener("click", function () { removeSub(sub, true); });
      buttons.appendChild(everyone);
    }
    li.appendChild(buttons);
    return li;
  }

  function fillSubs(listId, items) {
    var list = $(listId);
    list.textContent = "";
    items.forEach(function (sub) { list.appendChild(subRow(sub)); });
  }

  function renderSubs() {
    $("subs-load-error").textContent = subsLoadFailed ? t("subs.load_error") : "";
    if (!subsData) {
      $("subs-empty").hidden = true;
      $("subs-youtube-group").hidden = true;
      $("subs-podcast-group").hidden = true;
      return;
    }
    var channels = subsData.filter(function (sub) { return sub.kind !== "podcast"; });
    var podcasts = subsData.filter(function (sub) { return sub.kind === "podcast"; });
    fillSubs("subs-youtube-list", channels);
    fillSubs("subs-podcast-list", podcasts);
    $("subs-youtube-group").hidden = channels.length === 0;
    $("subs-podcast-group").hidden = podcasts.length === 0;
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
  function removeSub(sub, everyone) {
    var key = subKey(sub);
    var asked = everyone ? key + ":all" : key;
    if (!subsConfirm[asked]) {
      subsConfirm[asked] = true;
      renderSubs();
      window.setTimeout(function () {
        if (subsConfirm[asked]) { delete subsConfirm[asked]; renderSubs(); }
      }, 5000);
      return;
    }
    delete subsConfirm[asked];
    subsBusy[key] = "remove";
    renderSubs();
    subsRequest("DELETE", subPath(sub) + (everyone ? "?everyone=1" : "")).then(function (reply) {
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

  function startSubs() {
    loadSubs();
    window.setInterval(function () {
      if (document.visibilityState !== "hidden") loadSubs();
    }, SUBS_POLL_MS);
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible") loadSubs();
    });
  }

  /* ---------------------------------------------------------------- How to guide (#33) */

  function openHowto() {
    var dialog = $("howto");
    if (typeof dialog.showModal === "function") {
      if (!dialog.open) dialog.showModal();   // Esc closes it, focus stays inside, then returns here
    } else {
      dialog.setAttribute("open", "");
    }
  }

  function closeHowto() {
    var dialog = $("howto");
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  }

  function startHowto() {
    $("howto-open").addEventListener("click", openHowto);
    $("howto-close").addEventListener("click", closeHowto);
    // A click on the dark area around the guide (the dialog element itself) closes it.
    $("howto").addEventListener("click", function (event) {
      if (event.target === $("howto")) closeHowto();
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
    try {
      dash = window.Dash ? window.Dash.create({
        t: t,
        $: $,
        lang: function () { return lang; },
        timeText: timeText,
        statusText: statusText,
        getJson: getJson,
        post: post,
        request: request,
        isAdmin: function () { return !auth || auth.isAdmin(); },
        retryJob: function (id) { return post("/api/jobs/" + id + "/retry", {}).catch(function () { return null; }); },
        dismissJob: function (id) { return post("/api/jobs/" + id + "/dismiss", {}).catch(function () { return null; }); },
        restoreJob: function (id) { return request("DELETE", "/api/jobs/" + id + "/dismiss").catch(function () { return null; }); },
        dismissAllFailed: function () { return post("/api/jobs/dismiss-failed", {}).catch(function () { return null; }); },
      }) : null;   // an old cached dashboard.js must not take the form down with it
    } catch (e) { dash = null; }
    try {
      auth = window.Auth ? window.Auth.create({
        t: t, $: $, post: post, request: request, getJson: getJson, timeText: timeText,
      }) : null;
    } catch (e) { auth = null; }
    applyLanguage();
    document.querySelectorAll("input[name=mode]").forEach(function (radio) {
      radio.addEventListener("change", function () { setMode(radio.value); scheduleProbe(); });
    });
    $("media-type").addEventListener("change", showType);
    $("yt-target").addEventListener("change", applyTarget);
    $("subscribe").addEventListener("change", showBackfill);
    $("pack").addEventListener("change", function () {
      $("f-episode").disabled = $("pack").checked;
      if ($("pack").checked) $("f-episode").value = "";
    });
    $("pc-all").addEventListener("change", hideAllConfirm);
    $("link").addEventListener("input", onLinkChanged);
    $("link").addEventListener("change", onLinkChanged);   // autofill and some phone keyboards only send this
    $("link").addEventListener("blur", onLinkChanged);
    $("link").addEventListener("paste", function () { window.setTimeout(onLinkChanged, 0); });
    $("form").addEventListener("submit", onSubmit);
    $("reset").addEventListener("click", function () { resetFields(); clearErrors(); });
    $("lang").addEventListener("click", function () {
      lang = lang === "de" ? "en" : "de";
      try { window.localStorage.setItem("ytp-lang", lang); } catch (e) { /* private mode: fine */ }
      applyLanguage();
      clearErrors();
    });
    startHowto();
    startVersionCheck();
    if (auth) auth.wire();
    $("js-warning").hidden = true;   // everything above ran: the scripts are alive
    // Who is asking decides what starts: nothing talks to the app before the sign-in (#44).
    if (!auth) { startApp(); return; }
    auth.check().then(function (account) {
      if (account) startApp();
      auth.start(dash ? dash.view() : "input");
      scrollToAnchor();
    });
  }

  /* The browser scrolls to #subscriptions when the page loads, but until the sign-in is known that
     part is hidden and there is nothing to scroll to: do it once it shows. */
  function scrollToAnchor() {
    var id = (window.location.hash || "").replace(/^#/, "");
    if (!/^[A-Za-z][\w-]*$/.test(id) || (auth && !auth.isIn())) return;
    var target = document.getElementById(id);
    if (target && !target.hidden && typeof target.scrollIntoView === "function") target.scrollIntoView();
  }

  function startApp() {
    pollVpn();
    window.setInterval(function () {
      if (document.visibilityState !== "hidden") { pollVpn(); if (dash) dash.refreshAlerts(); }
    }, VPN_POLL_MS);
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible") { pollVpn(); if (dash) dash.refreshAlerts(); }
    });
    if (dash) dash.start();
    startJobPolling();
    startSubs();
  }

  /* ---------------------------------------------------------------- new version (#43) */

  /* The page names its own version (a meta tag); the server says which one it runs now. After an
     update an open page asks for a reload instead of mixing new data with old scripts. */
  function startVersionCheck() {
    var meta = document.querySelector('meta[name="ytp-static-version"]');
    var mine = meta ? meta.getAttribute("content") : "";
    $("update-banner-reload").addEventListener("click", function () { window.location.reload(); });
    function check() {
      fetch("/api/version", { credentials: "same-origin", cache: "no-store" })
        .then(function (response) { return response.ok ? response.json() : null; })
        .then(function (data) {
          if (data && typeof data.version === "string" && mine && data.version !== mine) {
            $("update-banner").hidden = false;
          }
        })
        .catch(function () { /* offline: nothing to say */ });
    }
    window.setTimeout(check, 3000);
    window.setInterval(function () { if (document.visibilityState !== "hidden") check(); }, 60000);
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible") check();
    });
  }

  init();
})();
