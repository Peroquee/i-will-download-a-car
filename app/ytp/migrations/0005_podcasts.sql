-- Podcasts (issue #34).
--
-- 1. jobs.type may now also be 'podcast' (one episode: external_id is a hash of the feed address
--    and the episode's GUID, see ytp/podcasts.py). SQLite cannot change a CHECK constraint in
--    place, so the table is rebuilt: same columns, same rows, same ids, the three indexes and the
--    trigger as in 0001. Nothing references jobs, so no foreign key needs care.
-- 2. podcast_subscriptions: the RSS feeds that are polled for new episodes. `title` is the
--    library folder name (it has been through safe_path_component). `feed_url` is the normalised
--    address the owner gave; the unique index makes subscribing twice a no-op.
CREATE TABLE jobs_new (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT NOT NULL,
    external_id TEXT NOT NULL,
    metadata    TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'queued',
    error       TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    source      TEXT,

    CHECK (type IN ('magnet', 'youtube', 'podcast')),
    CHECK (external_id <> ''),
    CHECK (json_valid(metadata) AND json_type(metadata) = 'object'),
    CHECK (status IN ('queued', 'downloading', 'organizing', 'done', 'failed'))
);

INSERT INTO jobs_new (id, type, external_id, metadata, status, error, created_at, updated_at, source)
    SELECT id, type, external_id, metadata, status, error, created_at, updated_at, source FROM jobs;

DROP TABLE jobs;
ALTER TABLE jobs_new RENAME TO jobs;

CREATE UNIQUE INDEX jobs_type_external_id ON jobs (type, external_id);
CREATE INDEX jobs_status ON jobs (status);
CREATE INDEX jobs_created_at ON jobs (created_at DESC);

CREATE TRIGGER jobs_touch_updated_at
AFTER UPDATE ON jobs
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE jobs SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = NEW.id;
END;

CREATE TABLE podcast_subscriptions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_url     TEXT NOT NULL UNIQUE,
    title        TEXT NOT NULL,
    last_checked TEXT,
    last_error   TEXT,
    enabled      INTEGER NOT NULL DEFAULT 1,
    -- How many of the feed's existing episodes the FIRST poll also takes: 0 (default) = only
    -- episodes published after subscribing.
    backfill     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    CHECK (feed_url <> ''),
    CHECK (title <> ''),
    CHECK (enabled IN (0, 1)),
    CHECK (backfill BETWEEN 0 AND 20)
);

CREATE INDEX podcast_subscriptions_enabled_last_checked
    ON podcast_subscriptions (enabled, last_checked);
