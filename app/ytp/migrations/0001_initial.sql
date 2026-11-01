-- Initial schema (issue #9): the job queue and the YouTube subscriptions.
--
-- Rules for every migration file:
--   * the name is NNNN_what_it_does.sql, numbered without gaps from 0001;
--   * a file is never edited after it has shipped (the app refuses to start if one
--     changed, see ytp/db.py): add a new file instead;
--   * no BEGIN / COMMIT inside: the runner wraps each file and its bookkeeping row in
--     one transaction, so a failure leaves the database on the previous version.
-- Timestamps are UTC, ISO 8601 with milliseconds and a trailing Z, so they sort as text.

CREATE TABLE jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    -- What is being downloaded: a torrent from a magnet link, or a YouTube video.
    type        TEXT NOT NULL,
    -- The natural key of the thing: the torrent's infohash (type 'magnet') or the video id
    -- (type 'youtube'). A second submission of the same thing must violate the unique index
    -- below instead of racing a "look first" check (#13). Normalising the value (lower-case
    -- hex infohash) is the caller's job.
    external_id TEXT NOT NULL,
    -- Per-type details as a JSON object (title, year, artist, target library, ...).
    metadata    TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'queued',
    -- Why a job failed, for people to read; NULL unless status is 'failed'.
    error       TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    CHECK (type IN ('magnet', 'youtube')),
    CHECK (external_id <> ''),
    CHECK (json_valid(metadata) AND json_type(metadata) = 'object'),
    CHECK (status IN ('queued', 'downloading', 'organizing', 'done', 'failed'))
);

-- Per type: an infohash and a video id cannot collide, the same magnet twice must.
CREATE UNIQUE INDEX jobs_type_external_id ON jobs (type, external_id);
-- The reconciler looks for unfinished work (#15); the job list sorts newest first (#26).
CREATE INDEX jobs_status ON jobs (status);
CREATE INDEX jobs_created_at ON jobs (created_at DESC);

-- Keeps updated_at honest without every caller remembering it. Only when the caller did not
-- set it itself. (Recursive triggers are off in SQLite, so this does not fire itself.)
CREATE TRIGGER jobs_touch_updated_at
AFTER UPDATE ON jobs
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE jobs SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = NEW.id;
END;

CREATE TABLE subscriptions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    -- The channel id (UC...), not the handle: the RSS feed is keyed by it.
    channel_id   TEXT NOT NULL UNIQUE,
    channel_name TEXT NOT NULL,
    -- When the channel was last polled, and what went wrong the last time (NULL = fine).
    last_checked TEXT,
    last_error   TEXT,
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    CHECK (channel_id <> ''),
    CHECK (enabled IN (0, 1))
);

-- The poller walks enabled channels, least recently checked first (#20).
CREATE INDEX subscriptions_enabled_last_checked ON subscriptions (enabled, last_checked);
