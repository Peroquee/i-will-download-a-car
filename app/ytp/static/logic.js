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

  /* values: {title, year, season, episode, pack, author, artist, album} */
  function magnetMetadata(type, values) {
    var text = function (key) { return String(values[key] == null ? "" : values[key]).trim(); };
    if (type === "movies") return { type: type, title: text("title"), year: toNumber(values.year) };
    if (type === "series") {
      var meta = { type: type, title: text("title"), season: toNumber(values.season) };
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

  function youtubePayload(link, channel, title) {
    return { url: String(link).trim(), channel: String(channel).trim(), title: String(title).trim() };
  }

  var MAX_PODCAST_BACKFILL = 20;

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
    return { episode: episode, subscribe: subscribe };
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
      return errors;
    }
    if (mode === "podcast") {
      if (!String(values.name || "").trim()) errors.name = "err.required";
      if (!values.episode && !values.subscribe) errors.episode = "pc.nothing";
      var back = String(values.backfill == null ? "" : values.backfill).trim();
      if (values.subscribe && back !== "" && (!/^\d+$/.test(back) || +back > MAX_PODCAST_BACKFILL)) {
        errors.backfill = "err.int_out_of_range_20";
      }
      return errors;
    }
    (REQUIRED[type] || []).forEach(function (key) {
      if (!String(values[key] == null ? "" : values[key]).trim()) errors[key] = "err.required";
    });
    var year = String(values.year == null ? "" : values.year).trim();
    if (type === "movies" && year !== "" && /^\d+$/.test(year) && (+year < MIN_YEAR || +year > nowYear + 1)) {
      errors.year = "err.year_out_of_range";
    }
    ["year", "season", "episode"].forEach(function (key) {
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

  return {
    age: age,
    kindKey: kindKey,
    vpnView: vpnView,
    detectMode: detectMode,
    looksLikeVideoUrl: looksLikeVideoUrl,
    looksLikeFeedUrl: looksLikeFeedUrl,
    podcastPayloads: podcastPayloads,
    MAX_PODCAST_BACKFILL: MAX_PODCAST_BACKFILL,
    format: format,
    magnetMetadata: magnetMetadata,
    magnetPayload: magnetPayload,
    youtubePayload: youtubePayload,
    clientErrors: clientErrors,
    serverErrors: serverErrors,
    MIN_YEAR: MIN_YEAR,
    MAX_NUMBER: MAX_NUMBER,
  };
});
