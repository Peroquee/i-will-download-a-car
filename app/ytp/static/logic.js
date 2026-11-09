/* Pure helpers of the submission form (no DOM), so they can be tested with node. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.Logic = factory();
  }
})(this, function () {
  "use strict";

  var MAX_NUMBER = 9999;
  var MIN_YEAR = 1870;

  function detectMode(text) {
    var value = String(text || "").trim().toLowerCase();
    if (value.indexOf("magnet:") === 0) return "magnet";
    if (/^https?:\/\/([a-z0-9-]+\.)*(youtube\.com|youtu\.be)(\/|$)/.test(value)) return "youtube";
    /* Any other web address is taken for a podcast feed; the server decides whether it is one. */
    if (/^https?:\/\/[^\s\/?#]+/.test(value)) return "podcast";
    return null;
  }

  /* Worth asking YouTube about: a YouTube address that names an 11-character video id. */
  function looksLikeVideoUrl(text) {
    var value = String(text || "").trim();
    return detectMode(value) === "youtube" && /(v=|youtu\.be\/|\/shorts\/|\/embed\/|\/live\/)[A-Za-z0-9_-]{11}/.test(value);
  }

  /* Worth reading as a feed: a web address that is not YouTube. */
  function looksLikeFeedUrl(text) {
    return detectMode(String(text || "").trim()) === "podcast";
  }

  function format(template, vars) {
    return String(template).replace(/\{(\w+)\}/g, function (all, key) {
      return vars && key in vars ? String(vars[key]) : all;
    });
  }

  function toNumber(text) {
    var value = String(text == null ? "" : text).trim();
    return /^\d+$/.test(value) ? parseInt(value, 10) : value;
  }

  /* Seasons (#52): "2", "1-3", "1,2,5" or "1-3,5" -> sorted unique list, or null. Mirrors
     ytp.metadata.parse_seasons; the server stays the judge. */
  var MAX_SEASONS = 50;
  function parseSeasons(text) {
    var spec = String(text == null ? "" : text).trim();
    if (spec === "") return null;
    if (spec.length > 1000) return null;
    var seen = {};
    var count = 0;
    var parts = spec.split(",");
    for (var i = 0; i < parts.length; i++) {
      var token = parts[i].trim();
      var match = /^([0-9]+)\s*(?:-\s*([0-9]+))?$/.exec(token);
      if (!match) return null;
      var start = parseInt(match[1], 10);
      var end = match[2] === undefined ? start : parseInt(match[2], 10);
      if (start > end) return null;
      for (var season = start; season <= end; season++) {
        if (season > MAX_NUMBER) return null;
        if (!seen[season]) {
          seen[season] = true;
          count += 1;
          if (count > MAX_SEASONS) return null;
        }
      }
    }
    return Object.keys(seen).map(Number).sort(function (a, b) { return a - b; });
  }

  /* values: {title, year, season, episode, pack, author, artist, album} */
  function magnetMetadata(type, values) {
    var text = function (key) { return String(values[key] == null ? "" : values[key]).trim(); };
    if (type === "movies") return { type: type, title: text("title"), year: toNumber(values.year) };
    if (type === "series") {
      /* The season stays text here: it may be a range or a list, and the server parses it. */
      var meta = { type: type, title: text("title"), season: text("season") };
      if (!values.pack && text("episode") !== "") meta.episode = toNumber(values.episode);
      return meta;
    }
    if (type === "audiobooks") return { type: type, author: text("author"), title: text("title") };
    if (type === "music") return { type: type, artist: text("artist"), album: text("album") };
    throw new Error("unknown type " + type);
  }

  function magnetPayload(link, type, values) {
    return { magnet: String(link).trim(), metadata: magnetMetadata(type, values) };
  }

  /* target: "video" (the default), "podcast" or "music"; audioOnly means nothing for a video.
     maxHeight is the chosen cap (0 = best); audio-only and podcast downloads ignore it. */
  function youtubePayload(link, channel, title, target, audioOnly, album, maxHeight) {
    var body = { url: String(link).trim(), channel: String(channel).trim(), title: String(title).trim() };
    if (target === "podcast" || target === "music") {
      body.target = target;
      if (audioOnly) body.audio_only = true;
    }
    /* Music: channel is the artist, title the track; an empty album means a single (the server names it). */
    if (target === "music" && album != null && String(album).trim()) body.album = String(album).trim();
    if (target !== "podcast" && !audioOnly && maxHeight != null && String(maxHeight).trim() !== "") {
      body.max_height = toNumber(maxHeight);
    }
    return body;
  }

  /* The body of POST /api/subscriptions for a channel; target "video" sends nothing extra. */
  function subscribePayload(channel, name, backfill, target, audioOnly) {
    var body = { channel: String(channel).trim() };
    if (name && String(name).trim()) body.name = String(name).trim();
    if (backfill != null) body.backfill = backfill;
    if (target === "podcast") {
      body.target = "podcast";
      if (audioOnly) body.audio_only = true;
    }
    return body;
  }

  var MAX_PODCAST_BACKFILL = 20;
  var MAX_CHANNEL_BACKFILL = 15;

  /* The requests a podcast form sends: {episode, subscribe}, each null when not wanted.
     values: {name, episode (a key or ""), subscribe (bool), backfill (text)} */
  function podcastPayloads(link, values) {
    var feed = String(link).trim();
    var name = String(values.name == null ? "" : values.name).trim();
    var episode = null;
    var subscribe = null;
    if (values.episode) {
      episode = { feed: feed, key: String(values.episode) };
      if (name) episode.name = name;
    }
    if (values.subscribe) {
      subscribe = { feed: feed, backfill: toNumber(values.backfill === "" ? "0" : values.backfill) };
      if (name) subscribe.name = name;
    }
    var backlog = null;
    if (values.all) {
      backlog = { feed: feed };
      if (name) backlog.name = name;
    }
    return { episode: episode, subscribe: subscribe, backlog: backlog };
  }

  var REQUIRED = {
    movies: ["title", "year"],
    series: ["title", "season"],
    audiobooks: ["author", "title"],
    music: ["artist", "album"],
  };

  /* A first look before sending; the server stays the judge. Returns {field: errorKey}. */
  function clientErrors(mode, type, link, values, nowYear) {
    var errors = {};
    if (!String(link || "").trim()) errors.link = "err.required";
    if (mode === "youtube") {
      if (!String(values.channel || "").trim()) errors.channel = "err.required";
      if (!String(values.title || "").trim()) errors.title = "err.required";
      /* Subscribing with the download: how many older videos to take as well (YouTube shows 15). */
      var older = String(values.backfill == null ? "" : values.backfill).trim();
      if (values.subscribe && older !== "" && (!/^\d+$/.test(older) || +older > MAX_CHANNEL_BACKFILL)) {
        errors.backfill = "err.int_out_of_range";
      }
      return errors;
    }
    if (mode === "podcast") {
      if (!String(values.name || "").trim()) errors.name = "err.required";
      if (!values.episode && !values.subscribe && !values.all) errors.episode = "pc.nothing";
      var back = String(values.backfill == null ? "" : values.backfill).trim();
      if (values.subscribe && back !== "" && (!/^\d+$/.test(back) || +back > MAX_PODCAST_BACKFILL)) {
        errors.backfill = "err.int_out_of_range_20";
      }
      return errors;
    }
    (REQUIRED[type] || []).forEach(function (key) {
      if (!String(values[key] == null ? "" : values[key]).trim()) errors[key] = "err.required";
    });
    if (type === "series") {
      var seasonSpec = String(values.season == null ? "" : values.season).trim();
      var seasons = seasonSpec === "" ? null : parseSeasons(seasonSpec);
      if (seasonSpec !== "" && !seasons) errors.season = "err.season_invalid";
      /* An episode number names one episode of one season; several seasons are a pack. */
      var episode = String(values.episode == null ? "" : values.episode).trim();
      if (!values.pack && seasons && seasons.length > 1 && episode !== "") {
        errors.episode = "err.episode_multi_season";
      }
    }
    var year = String(values.year == null ? "" : values.year).trim();
    if (type === "movies" && year !== "" && /^\d+$/.test(year) && (+year < MIN_YEAR || +year > nowYear + 1)) {
      errors.year = "err.year_out_of_range";
    }
    ["year", "episode"].forEach(function (key) {
      var v = String(values[key] == null ? "" : values[key]).trim();
      if (v !== "" && !/^\d+$/.test(v) && !errors[key]) errors[key] = "err.int_parsing";
    });
    return errors;
  }

  /* FastAPI's 422 detail -> {field: errorType}. "link" is the pasted text itself. */
  function serverErrors(detail) {
    var result = {};
    (Array.isArray(detail) ? detail : []).forEach(function (item) {
      var loc = item.loc || [];
      var last = loc[loc.length - 1];
      var field;
      if (loc[1] === "magnet" || loc[1] === "url" || loc[1] === "feed") field = "link";
      else if (loc[1] === "metadata") field = typeof last === "string" ? last : "link";
      else field = typeof last === "string" ? last : "link";
      if (!(field in result)) result[field] = { type: item.type, msg: item.msg };
    });
    return result;
  }

  /* How long ago an ISO timestamp is: {unit: "now"|"minutes"|"hours"|"days", n}; null if unreadable. */
  function age(iso, nowMs) {
    var then = Date.parse(iso);
    if (isNaN(then)) return null;
    var seconds = Math.max(0, Math.floor((nowMs - then) / 1000));
    if (seconds < 60) return { unit: "now", n: 0 };
    if (seconds < 3600) return { unit: "minutes", n: Math.floor(seconds / 60) };
    if (seconds < 86400) return { unit: "hours", n: Math.floor(seconds / 3600) };
    return { unit: "days", n: Math.floor(seconds / 86400) };
  }

  /* The one-line summary of the downloads on the input page, from an /api/jobs answer:
     {running, waiting, failed, progress}. The counts come from the server's totals when it sends
     them (the list holds only the newest jobs); progress is the mean of the downloads that report
     one, rounded, or null when none does. */
  function jobsSummary(data) {
    var jobs = data && Array.isArray(data.jobs) ? data.jobs : [];
    var counts = data && data.counts && typeof data.counts === "object" ? data.counts : null;
    function count(status) {
      if (counts && typeof counts[status] === "number") return counts[status];
      return jobs.filter(function (job) { return job && job.status === status; }).length;
    }
    var shares = jobs.filter(function (job) {
      return job && job.status === "downloading" && typeof job.progress === "number" && isFinite(job.progress);
    }).map(function (job) { return Math.max(0, Math.min(100, job.progress)); });
    var sum = shares.reduce(function (a, b) { return a + b; }, 0);
    return {
      running: count("downloading") + count("organizing"),
      waiting: count("queued"),
      failed: count("failed"),
      progress: shares.length ? Math.round(sum / shares.length) : null,
    };
  }

  /* Which i18n key names a job's kind (the library it ends up in). */
  function kindKey(kind) {
    return { movies: "type.movies", series: "type.series", audiobooks: "type.audiobooks",
             music: "type.music", youtube: "mode.youtube", podcast: "type.podcast" }[kind] || null;
  }

  /* What the VPN bar shows for an /api/vpn answer. `view` is the colour/state, in order of
     priority: down, mismatch (the egress check caught a leak), blind, starting, unknown, ok.
     `alert` = the bar must be unmissable. Anything unreadable counts as unknown, never as ok. */
  function vpnView(data) {
    var d = data && typeof data === "object" ? data : {};
    var state = ["ok", "down", "blind", "starting"].indexOf(d.state) >= 0 ? d.state : "unknown";
    var view = state;
    if (state === "ok" && d.egress === "mismatch") view = "mismatch";
    if (state === "unknown" && d.egress === "mismatch") view = "mismatch";
    return {
      view: view,
      alert: view === "down" || view === "mismatch" || view === "blind",
      paused: d.paused === true,
      since: typeof d.since === "string" ? d.since : null,
      country: view === "ok" && d.egress === "ok" && typeof d.egress_country === "string" ? d.egress_country.slice(0, 60) : null,
    };
  }

  /* ---------------------------------------------------------------- dashboard (#38) */

  /* "1.5 GB", "320 KB"; null/negative = unknown. The decimal mark follows the language. */
  function formatBytes(value, lang) {
    if (typeof value !== "number" || !isFinite(value) || value < 0) return null;
    var units = ["B", "KB", "MB", "GB", "TB"];
    var n = value;
    var i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
    var text = i === 0 || n >= 100 ? String(Math.round(n)) : n.toFixed(1);
    if (lang === "de") text = text.replace(".", ",");
    return text + " " + units[i];
  }

  function formatSpeed(value, lang) {
    var size = formatBytes(value, lang);
    return size === null || !value ? null : size + "/s";
  }

  /* "5 min", "1 h 20 min", "2 d 3 h"; null = unknown (also qBittorrent's "infinity"). */
  function formatEta(seconds) {
    if (typeof seconds !== "number" || !isFinite(seconds) || seconds < 0 || seconds >= 8640000) return null;
    var minutes = Math.round(seconds / 60);
    if (seconds < 60) return "< 1 min";
    if (minutes < 60) return minutes + " min";
    var hours = Math.floor(minutes / 60);
    if (hours < 24) return hours + " h" + (minutes % 60 ? " " + (minutes % 60) + " min" : "");
    var days = Math.floor(hours / 24);
    return days + " d" + (hours % 24 ? " " + (hours % 24) + " h" : "");
  }

  /* Which view of the one page is wanted: "dashboard" for /dashboard, "accounts" for /accounts
     (#44), "subscriptions" for /#subscriptions (the list of subscriptions, a view of its own),
     else the form. */
  function pageView(pathname, hash) {
    var path = String(pathname || "");
    if (/^\/dashboard\/?$/.test(path)) return "dashboard";
    if (/^\/accounts\/?$/.test(path)) return "accounts";
    return String(hash || "") === "#subscriptions" ? "subscriptions" : "input";
  }

  function alertAnchor(id) { return "alert-" + id; }

  /* The alert id named by a location hash ("#alert-12"), or null. */
  function alertIdFromHash(hash) {
    var match = /^#alert-(\d{1,9})$/.exec(String(hash || ""));
    return match ? Number(match[1]) : null;
  }

  /* The text keys of an alert and the values to fill in. `known` says whether a key exists. A
     kind the page has no text for falls back to the English title and detail from the server. */
  function alertTexts(alert, known) {
    var kind = alert && typeof alert.kind === "string" ? alert.kind : "";
    var base = "alert." + kind;
    var params = alert && alert.params && typeof alert.params === "object" ? alert.params : {};
    if (kind && known(base + ".title")) {
      return {
        title: base + ".title",
        what: known(base + ".what") ? base + ".what" : null,
        did: known(base + ".did") ? base + ".did" : null,
        todo: known(base + ".todo") ? base + ".todo" : null,
        params: params,
        fallback: false,
      };
    }
    return { title: null, what: null, did: null, todo: null, params: params, fallback: true };
  }

  /* The newest unread alerts for the strip, at most `max`. */
  function stripAlerts(alerts, max) {
    return (Array.isArray(alerts) ? alerts : []).filter(function (a) { return a && !a.acknowledged; }).slice(0, max);
  }

  return {
    formatBytes: formatBytes,
    formatSpeed: formatSpeed,
    formatEta: formatEta,
    pageView: pageView,
    alertAnchor: alertAnchor,
    alertIdFromHash: alertIdFromHash,
    alertTexts: alertTexts,
    stripAlerts: stripAlerts,
    age: age,
    kindKey: kindKey,
    jobsSummary: jobsSummary,
    vpnView: vpnView,
    detectMode: detectMode,
    looksLikeVideoUrl: looksLikeVideoUrl,
    looksLikeFeedUrl: looksLikeFeedUrl,
    podcastPayloads: podcastPayloads,
    MAX_PODCAST_BACKFILL: MAX_PODCAST_BACKFILL,
    MAX_CHANNEL_BACKFILL: MAX_CHANNEL_BACKFILL,
    format: format,
    parseSeasons: parseSeasons,
    magnetPayload: magnetPayload,
    youtubePayload: youtubePayload,
    subscribePayload: subscribePayload,
    clientErrors: clientErrors,
    serverErrors: serverErrors,
    MIN_YEAR: MIN_YEAR,
    MAX_NUMBER: MAX_NUMBER,
  };
});
