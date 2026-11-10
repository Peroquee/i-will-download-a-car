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
  var ytDefaultMaxHeight = null;   // the account's effective default resolution (#47, #65)
  var ytCanChoose = true;          // the admin's resolution right for this account (#65)

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
    renderSettings();
    renderServerDefaults();
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

  /* A request whose body is plain text, not JSON (the YouTube cookies file, #46). */
  function requestText(method, path, text) {
    var options = {
      method: method, credentials: "same-origin",
      headers: { "Accept": "application/json", "Content-Type": "text/plain; charset=utf-8" },
      body: text,
    };
    return api(path, options).then(function (response) {
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
    /* The resolution is a video setting; podcast and audio-only downloads have none. */
    $("yt-resolution-row").hidden = target === "podcast" || ytAudioOnly();
    var text = TARGET_TEXT[target];
    var labels = [["yt-channel-label", text[0]], ["yt-title-label", text[1]], ["yt-subscribe-note", text[2]]];
    labels.forEach(function (pair) {
      $(pair[0]).dataset.i18n = pair[1];
      $(pair[0]).textContent = t(pair[1]);
    });
  }

  /* The account's effective default resolution and the fixed choices the API accepts (#47, #65).
     The page preselects; it never fetches formats from YouTube. Without the resolution right
     (can_choose false) the dropdown is disabled and a short note says an admin decides. */
  function loadYoutubeOptions() {
    getJson("/api/youtube/options").then(function (data) {
      if (!data || typeof data.max_height !== "number") return;
      ytDefaultMaxHeight = data.max_height;
      ytCanChoose = data.can_choose !== false;
      var select = $("yt-resolution");
      var value = String(data.max_height);
      var known = Array.prototype.some.call(select.options, function (option) {
        return option.value === value;
      });
      if (!known) {
        var option = document.createElement("option");
        option.value = value;
        option.textContent = t("yt.resolution_default", { value: value });
        select.appendChild(option);
      }
      select.value = value;
      select.disabled = !ytCanChoose;
      $("yt-resolution-note").hidden = ytCanChoose;
      $("yt-sub-resolution").disabled = !ytCanChoose;
      showBackfill();   // the subscription row depends on the right too
    }).catch(function () { /* keep the first choice; the server default still applies */ });
  }

  /* What the form sends for the resolution: nothing while audio only, and nothing while the
     chosen value is the server default (then the server's own default applies, also when the
     configured default is not one of the fixed choices). */
  function ytMaxHeight() {
    if (ytTarget() === "podcast" || ytAudioOnly()) return null;
    var value = String($("yt-resolution").value);
    if (value === "") return null;
    if (ytDefaultMaxHeight !== null && value === String(ytDefaultMaxHeight)) return null;
    return value;
  }

  /* The resolution chosen for a NEW subscription (#65): empty = the server default. Podcast
     subscriptions have none, and without the right the row is not offered at all. */
  function ytSubscriptionMaxHeight() {
    if (ytTarget() !== "video" || !ytCanChoose) return null;
    var value = String($("yt-sub-resolution").value);
    return value === "" ? null : Number(value);
  }

  /* ---------------------------------------------------------------- subscribe (#19) */
  /* Subscribing is offered for a video or a podcast episode, not for music (applyTarget). */
  function wantsSubscription() { return ytTarget() !== "music" && $("subscribe").checked; }

  /* "Older videos" and the subscription's own resolution only mean something while "subscribe"
     is ticked. Podcast subscriptions have no resolution (they keep the server default, #47/#65),
     and an account without the resolution right does not get the field at all. */
  function showBackfill() {
    var on = $("subscribe").checked;
    $("yt-backfill-row").hidden = !on;
    $("yt-sub-resolution-row").hidden = !on || ytTarget() !== "video" || !ytCanChoose;
  }

  /* Ticked "subscribe" next to a YouTube download: the video's address names the channel, and
     "older videos" says how many of the channel's latest videos to take as well (0 = none). */
  function subscribeToChannel(link, name, backfill, downloadText, maxHeight) {
    var body = L.subscribePayload(
      link, name, backfill, ytTarget(), ytAudioOnly(), maxHeight
    );
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
    var highPriority = $("high-priority").checked;
    var path = current === "youtube" ? "/api/youtube/download" : "/api/magnet";
    var body = current === "youtube"
      ? L.youtubePayload(link, data.channel, data.title, ytTarget(), ytAudioOnly(), data.album, ytMaxHeight(), highPriority)
      : L.magnetPayload(link, mediaType(), data, highPriority);
    $("submit").disabled = true;
    post(path, body).then(function (reply) {
      $("submit").disabled = false;
      if (reply.status === 201 || reply.status === 200) {
        var vars = { id: reply.data.id, status: statusText(reply.data.status) };
        var text = t(reply.data.duplicate ? "result.duplicate" : "result.created", vars);
        var wantSubscribe = current === "youtube" && wantsSubscription();
        var channelName = data.channel;
        var backfill = +(String(data.backfill || "").trim() || "0");
        // Read before resetFields() clears the form (#65); the subscription is sent after it.
        var subscribeMaxHeight = current === "youtube" ? ytSubscriptionMaxHeight() : null;
        showResult(text, true);
        if (!reply.data.duplicate) resetFields();
        pollJobs();
        if (wantSubscribe) subscribeToChannel(link, channelName, backfill, text, subscribeMaxHeight);
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
    var payloads = L.podcastPayloads(link, data, $("high-priority").checked);
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
    $("high-priority").checked = false;
    $("subscribe").checked = false;
    $("yt-backfill").value = "0";
    $("yt-sub-resolution").value = "";
    showBackfill();
    $("yt-target").value = "video";
    shownTarget = "video";
    $("yt-resolution").value = ytDefaultMaxHeight === null ? "0" : String(ytDefaultMaxHeight);
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
    var detailKey = vpnStale ? null : L.vpnDetailKey(info);
    if (detailKey) parts.push(t(detailKey));
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
    Promise.all([getJson("/api/subscriptions"), getJson("/api/podcasts"), loadDefaults()])
      .then(function (answers) {
        var channels = answers[0].subscriptions.map(function (sub) { sub.kind = "channel"; return sub; });
        var podcasts = answers[1].podcasts;   // these carry kind: "podcast" already
        subsData = channels.concat(podcasts).sort(function (a, b) {
          return subName(a).toLowerCase().localeCompare(subName(b).toLowerCase());
        });
        subsLoadFailed = false;
      })
      .catch(function () { subsLoadFailed = true; })
      .then(function () { subsLoading = false; renderSubs(); renderServerDefaults(); });
  }

  function subsRequest(method, path, body) { return request(method, path, body); }

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

  /* The resolution of a channel subscription (#65): what new videos use, or the server default.
     Everyone who follows it sees the value; only an account with the resolution right gets a
     working dropdown (a subscription is shared, so the value applies to all followers). */
  function resolutionLabel(value) {
    if (value === null || value === undefined) return t("subs.resolution_default");
    if (value === 0) return t("yt.resolution_best");
    var key = "yt.resolution_" + value;
    var label = t(key);
    return label === key ? value + "p" : label;
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
    if (sub.kind !== "podcast") {
      metaText += " \u00b7 " + t("subs.resolution_meta", { value: resolutionLabel(sub.max_height) });
    }
    metaText += " \u00b7 " + t("subs.schedule_meta", { value: scheduleSummary(sub) });
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
    var settings = document.createElement("button");
    settings.type = "button";
    settings.id = "subs-settings-" + sub.kind + "-" + sub.id;
    settings.className = "ghost small";
    settings.disabled = !!busy;
    settings.setAttribute("aria-haspopup", "dialog");
    settings.textContent = t("subs.settings");
    settings.addEventListener("click", function () { openSettings(sub, settings.id); });
    buttons.appendChild(poll);
    buttons.appendChild(settings);
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
    // The rows are rebuilt on every refresh: a focused button of the list keeps the focus.
    var focused = document.activeElement && /^subs-/.test(document.activeElement.id || "")
      ? document.activeElement.id : "";
    fillSubs("subs-youtube-list", channels);
    fillSubs("subs-podcast-list", podcasts);
    var again = focused ? $(focused) : null;
    if (again && again !== document.activeElement && typeof again.focus === "function") again.focus();
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

  /* ---------------------------------------------------------------- subscription settings (#79) */

  /* The Settings window of a subscription (resolution, check interval, check time) and the admin's
     server-wide defaults share one set of controls. "Server default" is a real choice: the value
     is null on the wire and follows whatever the admin set (or .env says) at the time. */
  var PRESET_MINUTES = [15, 30, 60, 120, 180, 360, 720, 1440];
  var MINUTES_MAX = 1440;
  var defaultsData = null;       // GET /api/settings/defaults: what "server default" means now

  function loadDefaults() {
    return getJson("/api/settings/defaults").then(function (data) {
      defaultsData = data;
      return data;
    }).catch(function () { return null; });
  }

  function minMinutes(kind) {
    var min = defaultsData && defaultsData.limits && defaultsData.limits.min_minutes
      ? defaultsData.limits.min_minutes[kind === "podcast" ? "podcast" : "channel"] : 0;
    return min || (kind === "podcast" ? 30 : 15);
  }

  function intervalText(minutes) {
    if (minutes === 0) return t("sched.paused");
    var amount = minutes % 60 === 0 ? t("sched.hours", { n: minutes / 60 }) : t("sched.minutes", { n: minutes });
    return t("sched.every", { interval: amount });
  }

  function scheduleText(minutes, at) {
    var text = intervalText(minutes);
    return at && minutes !== 0 ? text + " · " + t("sched.from", { time: at }) : text;
  }

  /* The server's interval and start time for a kind of subscription, or null before they are known. */
  function defaultSchedule(kind) {
    if (!defaultsData) return null;
    var prefix = kind === "podcast" ? "podcast" : "subscription";
    return { minutes: defaultsData[prefix + "_poll_minutes"], at: defaultsData[prefix + "_poll_at"] || null };
  }

  function heightText(value) { return value === 0 ? t("yt.resolution_best") : value + "p"; }

  /* The row's summary: what this subscription uses, and that it follows the server if it does. */
  function scheduleSummary(sub) {
    var kind = sub.kind === "podcast" ? "podcast" : "channel";
    var base = defaultSchedule(kind);
    var minutes = sub.poll_minutes == null ? (base ? base.minutes : null) : sub.poll_minutes;
    var at = sub.poll_at == null ? (base ? base.at : null) : (sub.poll_at || null);
    var follows = sub.poll_minutes == null && sub.poll_at == null;
    if (minutes === null) return t("sched.default_unknown");
    var text = scheduleText(minutes, at);
    return follows ? t("sched.default_is", { value: text }) : text;
  }

  function optionEl(value, label) {
    var option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    return option;
  }

  function fieldRow(labelKey, forId) {
    var row = document.createElement("div");
    row.className = "row";
    var label = document.createElement("label");
    label.className = "field";
    label.setAttribute("for", forId);
    label.dataset.i18n = labelKey;
    label.textContent = t(labelKey);
    row.appendChild(label);
    return row;
  }

  /* The interval and check-time controls inside `container`. `server` = the admin's defaults (no
     "server default" choice: empty means the initial .env value). Returns {fill, read, relabel,
     disable}. read() gives {ok, poll_minutes, poll_at} or {ok: false, error, vars}. */
  function scheduleControls(container, prefix, kind, server) {
    container.textContent = "";
    var intervalId = prefix + "-interval";
    var minutesId = prefix + "-minutes";
    var modeId = prefix + "-at-mode";
    var atId = prefix + "-at";

    var intervalRow = fieldRow("sched.interval", intervalId);
    var interval = document.createElement("select");
    interval.id = intervalId;
    var minutes = document.createElement("input");
    minutes.id = minutesId;
    minutes.type = "number";
    minutes.step = "1";
    minutes.hidden = true;
    minutes.setAttribute("aria-label", t("sched.minutes_label"));
    intervalRow.appendChild(interval);
    intervalRow.appendChild(minutes);

    var atRow = fieldRow("sched.at", modeId);
    var mode = document.createElement("select");
    mode.id = modeId;
    var at = document.createElement("input");
    at.id = atId;
    at.type = "time";
    at.hidden = true;
    at.setAttribute("aria-label", t("sched.at"));
    atRow.appendChild(mode);
    atRow.appendChild(at);

    var paused = document.createElement("p");
    paused.className = "note";
    paused.id = prefix + "-paused-note";
    paused.hidden = true;
    container.appendChild(intervalRow);
    container.appendChild(atRow);
    container.appendChild(paused);

    function defaultLabel() {
      var base = defaultSchedule(kind);
      return base ? t("sched.default_is", { value: scheduleText(base.minutes, base.at) }) : t("sched.default_unknown");
    }

    function show() {
      minutes.hidden = interval.value !== "custom";
      at.hidden = mode.value !== "time";
      paused.hidden = interval.value !== "0";
    }

    function relabel() {
      var keepInterval = interval.value;
      var keepMode = mode.value;
      interval.textContent = "";
      interval.appendChild(optionEl("", server ? t("sched.initial") : defaultLabel()));
      interval.appendChild(optionEl("0", t("sched.paused_option")));
      PRESET_MINUTES.forEach(function (n) {
        if (n >= minMinutes(kind)) interval.appendChild(optionEl(String(n), intervalText(n)));
      });
      interval.appendChild(optionEl("custom", t("sched.custom")));
      interval.value = keepInterval;
      mode.textContent = "";
      if (!server) {
        var base = defaultSchedule(kind);
        mode.appendChild(optionEl("", t("sched.default_is", {
          value: base && base.at ? t("sched.from", { time: base.at }) : t("sched.at_none_short"),
        })));
      }
      mode.appendChild(optionEl("none", t("sched.at_none")));
      mode.appendChild(optionEl("time", t("sched.at_time")));
      mode.value = keepMode;
      minutes.min = String(minMinutes(kind));
      minutes.max = String(MINUTES_MAX);
      paused.textContent = t("sched.paused_note");
      show();
    }
    ["change", "input"].forEach(function (type) {
      interval.addEventListener(type, show);
      mode.addEventListener(type, show);
    });

    function fill(ownMinutes, ownAt) {
      var inPresets = ownMinutes === 0
        || (PRESET_MINUTES.indexOf(ownMinutes) >= 0 && ownMinutes >= minMinutes(kind));
      if (ownMinutes == null) interval.value = "";
      else if (inPresets) interval.value = String(ownMinutes);
      else interval.value = "custom";
      minutes.value = ownMinutes != null && !inPresets ? String(ownMinutes) : "";
      if (server) mode.value = ownAt ? "time" : "none";
      else mode.value = ownAt == null ? "" : (ownAt === "" ? "none" : "time");
      at.value = ownAt || "";
      show();
    }

    function read() {
      var wire = null;
      if (interval.value === "custom") {
        var typed = String(minutes.value).trim();
        var n = Number(typed);
        if (!/^\d+$/.test(typed) || n < minMinutes(kind) || n > MINUTES_MAX) {
          return { ok: false, error: "sched.err_minutes", vars: { min: minMinutes(kind), max: MINUTES_MAX } };
        }
        wire = n;
      } else if (interval.value !== "") {
        wire = Number(interval.value);
      }
      var time = null;
      if (mode.value === "time") {
        if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(String(at.value))) return { ok: false, error: "sched.err_time", vars: {} };
        time = String(at.value);
      } else if (mode.value === "none") {
        time = server ? null : "";
      }
      return { ok: true, poll_minutes: wire, poll_at: time };
    }

    function disable(off) {
      [interval, minutes, mode, at].forEach(function (control) { control.disabled = off; });
    }

    relabel();
    return { fill: fill, read: read, relabel: relabel, disable: disable, first: interval };
  }

  /* ---- the Settings window of one subscription */

  var settingsFor = null;        // the subscription the open window belongs to
  var settingsOpener = "";       // id of the button that opened it (focus goes back there)
  var settingsControls = null;
  var settingsBusy = false;

  function settingsKind() { return settingsFor && settingsFor.kind === "podcast" ? "podcast" : "channel"; }
  function mayChangeSettings() { return !auth || auth.canChooseResolution(); }

  function resolutionOptions(select, firstLabel) {
    var keep = select.value;
    select.textContent = "";
    select.appendChild(optionEl("", firstLabel));
    select.appendChild(optionEl("0", t("yt.resolution_best")));
    [2160, 1440, 1080, 720, 480, 360].forEach(function (height) {
      select.appendChild(optionEl(String(height), resolutionLabel(height)));
    });
    select.value = keep;
  }

  function fillSettingsResolution() {
    var shown = defaultsData && defaultsData.max_height != null ? heightText(defaultsData.max_height) : "";
    resolutionOptions($("sub-settings-resolution"),
      shown ? t("sched.default_is", { value: shown }) : t("subs.resolution_default"));
  }

  function settingsError(text) {
    $("sub-settings-error").textContent = text;
  }

  function renderSettings() {
    if (!settingsFor) return;
    var channel = settingsKind() === "channel";
    var may = mayChangeSettings();
    $("sub-settings-title").textContent = t("sched.title", { name: subName(settingsFor) });
    $("sub-settings-resolution-row").hidden = !channel;
    if (channel) fillSettingsResolution();
    settingsControls.relabel();
    $("sub-settings-tz").textContent = defaultsData && defaultsData.timezone
      ? t("sched.tz", { zone: defaultsData.timezone }) : "";
    $("sub-settings-rule").textContent = t("sched.rule");
    $("sub-settings-readonly").hidden = may;
    $("sub-settings-save").hidden = !may;
    $("sub-settings-resolution").disabled = !may;
    settingsControls.disable(!may);
    $("sub-settings-save").disabled = settingsBusy;
  }

  function openSettings(sub, openerId) {
    if (settingsFor) return;   // one window at a time: a second click while one opens must not rebuild it
    settingsFor = sub;
    settingsOpener = openerId;
    settingsBusy = false;
    loadDefaults().then(function () {
      if (settingsFor !== sub) return;
      var dialog = $("sub-settings");
      if (dialog.open) return;   // already showing: never rebuild the controls under a user
      settingsControls = scheduleControls($("sub-settings-schedule"), "sub-settings", settingsKind(), false);
      if (settingsKind() === "channel") {
        fillSettingsResolution();
        $("sub-settings-resolution").value = sub.max_height == null ? "" : String(sub.max_height);
      }
      settingsControls.fill(sub.poll_minutes == null ? null : sub.poll_minutes, sub.poll_at == null ? null : sub.poll_at);
      settingsError("");
      renderSettings();
      document.body.classList.add("modal-open");   // the page behind does not scroll
      if (typeof dialog.showModal === "function") {
        if (!dialog.open) dialog.showModal();   // Esc closes it, focus stays inside
      } else {
        dialog.setAttribute("open", "");
      }
      (settingsKind() === "channel" ? $("sub-settings-resolution") : settingsControls.first).focus();
    }).catch(function () {
      settingsFor = null;   // whatever went wrong: the next click must be able to try again
      settingsOpener = "";
    });
  }

  function closeSettings() {
    var dialog = $("sub-settings");
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
    settingsClosed();   // at once: the browser's own "close" event comes later (and Esc has only that one)
  }

  /* Whatever closed it (Close, Cancel, Esc, a save): the page scrolls again and the focus goes back
     to the button that opened it (looked up again: the list is rebuilt while the window is open). */
  function settingsClosed() {
    // The "close" event of an earlier closing can arrive after the window was opened again, or
    // after closeSettings() has cleaned up already: neither may undo a newer open.
    if ($("sub-settings").open || !settingsFor) return;
    document.body.classList.remove("modal-open");
    var opener = settingsOpener ? $(settingsOpener) : null;
    settingsFor = null;
    settingsOpener = "";
    if (opener && typeof opener.focus === "function") opener.focus();
  }

  function saveSettings() {
    if (!settingsFor || settingsBusy || !mayChangeSettings()) return;
    var sub = settingsFor;
    var read = settingsControls.read();
    if (!read.ok) { settingsError(t(read.error, read.vars)); return; }
    var body = { poll_minutes: read.poll_minutes, poll_at: read.poll_at };
    if (settingsKind() === "channel") {
      var height = $("sub-settings-resolution").value;
      body.max_height = height === "" ? null : Number(height);
    }
    settingsBusy = true;
    settingsError("");
    $("sub-settings-save").disabled = true;
    subsRequest("PATCH", subPath(sub), body).then(function (reply) {
      if (reply.status === 200) {
        subsMessages[subKey(sub)] = { ok: true, text: function () { return t("subs.settings_saved"); } };
        closeSettings();
        loadSubs();
      } else {
        settingsError(failureText(reply, "sched.save_failed"));
      }
    }).catch(function () {
      settingsError(t("sched.save_failed") + " " + t("result.network"));
    }).then(function () {
      settingsBusy = false;
      $("sub-settings-save").disabled = false;
    });
  }

  function startSettings() {
    var dialog = $("sub-settings");
    $("sub-settings-close").addEventListener("click", closeSettings);
    $("sub-settings-cancel").addEventListener("click", closeSettings);
    $("sub-settings-save").addEventListener("click", saveSettings);
    dialog.addEventListener("close", settingsClosed);
    // A click on the dark area around the window (the dialog element itself) closes it.
    dialog.addEventListener("click", function (event) { if (event.target === dialog) closeSettings(); });
    $("sub-settings-form").addEventListener("submit", function (event) { event.preventDefault(); saveSettings(); });
  }

  /* ---- the admin's server-wide defaults (accounts page) */

  var serverForms = null;        // {channel, podcast} controls, built on the first render
  var serverDirty = false;       // the admin is editing: a refresh must not overwrite it

  function fillServerDefaults() {
    if (!defaultsData || !serverForms) return;
    var custom = defaultsData.custom || {};
    $("sd-resolution").value = custom.max_height ? String(defaultsData.max_height) : "";
    serverForms.channel.fill(
      custom.subscription_poll_minutes ? defaultsData.subscription_poll_minutes : null,
      custom.subscription_poll_at ? defaultsData.subscription_poll_at : null);
    serverForms.podcast.fill(
      custom.podcast_poll_minutes ? defaultsData.podcast_poll_minutes : null,
      custom.podcast_poll_at ? defaultsData.podcast_poll_at : null);
    $("sd-tz").textContent = defaultsData.timezone ? t("sched.tz", { zone: defaultsData.timezone }) : "";
  }

  function renderServerDefaults() {
    if (!$("server-defaults") || !auth || !auth.isAdmin()) return;
    var first = !serverForms;
    if (first) {
      serverForms = {
        channel: scheduleControls($("sd-channel-schedule"), "sd-channel", "channel", true),
        podcast: scheduleControls($("sd-podcast-schedule"), "sd-podcast", "podcast", true),
      };
    } else {
      serverForms.channel.relabel();
      serverForms.podcast.relabel();
    }
    resolutionOptions($("sd-resolution"), t("sched.initial"));
    $("sd-rule").textContent = t("sched.rule");
    if (first || !serverDirty) fillServerDefaults();
  }

  function saveServerDefaults() {
    if (!serverForms) return;
    var channel = serverForms.channel.read();
    var podcast = serverForms.podcast.read();
    var bad = !channel.ok ? channel : (!podcast.ok ? podcast : null);
    var result = $("sd-result");
    if (bad) { result.className = "result bad"; result.textContent = t(bad.error, bad.vars); return; }
    var height = $("sd-resolution").value;
    var body = {
      max_height: height === "" ? null : Number(height),
      subscription_poll_minutes: channel.poll_minutes,
      subscription_poll_at: channel.poll_at,
      podcast_poll_minutes: podcast.poll_minutes,
      podcast_poll_at: podcast.poll_at,
    };
    $("sd-save").disabled = true;
    request("PATCH", "/api/settings/defaults", body).then(function (reply) {
      if (reply.status === 200) {
        defaultsData = reply.data;
        serverDirty = false;
        renderServerDefaults();
        result.className = "result ok";
        result.textContent = t("sd.saved");
        renderSubs();
      } else {
        result.className = "result bad";
        result.textContent = failureText(reply, "sd.failed");
      }
    }).catch(function () {
      result.className = "result bad";
      result.textContent = t("sd.failed") + " " + t("result.network");
    }).then(function () { $("sd-save").disabled = false; });
  }

  function startServerDefaults() {
    if (!$("server-defaults")) return;
    $("server-defaults").addEventListener("change", function () { serverDirty = true; $("sd-result").textContent = ""; });
    $("sd-save").addEventListener("click", saveServerDefaults);
    $("sd-form").addEventListener("submit", function (event) { event.preventDefault(); saveServerDefaults(); });
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
        t: t, $: $, post: post, request: request, requestText: requestText, getJson: getJson, timeText: timeText,
      }) : null;
    } catch (e) { auth = null; }
    applyLanguage();
    document.querySelectorAll("input[name=mode]").forEach(function (radio) {
      radio.addEventListener("change", function () { setMode(radio.value); scheduleProbe(); });
    });
    $("media-type").addEventListener("change", showType);
    $("yt-target").addEventListener("change", applyTarget);
    $("yt-audio").addEventListener("change", applyTarget);
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
    startSettings();
    startServerDefaults();
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
     part is hidden and there is nothing to scroll to: do it once it shows. The dashboard's hashes
     are tabs (and an alert, which it brings into view itself): nothing to scroll to there. */
  function scrollToAnchor() {
    if (dash && dash.view() === "dashboard") return;
    var id = (window.location.hash || "").replace(/^#/, "");
    if (!/^[A-Za-z][\w-]*$/.test(id) || (auth && !auth.isIn())) return;
    var target = document.getElementById(id);
    if (target && !target.hidden && typeof target.scrollIntoView === "function") target.scrollIntoView();
  }

  function startApp() {
    pollVpn();
    loadYoutubeOptions();
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
