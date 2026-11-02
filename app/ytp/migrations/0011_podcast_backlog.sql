-- "Download the whole back catalogue" of a podcast feed (#45). Additive: nothing existing changes.
--
-- `podcast_backlogs`: one run per feed. `podcast_backlog_items`: the episodes still to be queued
-- (and, with `submitted` = 1, the ones that were), newest first by `position`. The feeder
-- (podcast_backlog.py) puts a few at a time into `jobs`, so the 300 episodes of a feed do not
-- flood the job list or the podcast worker, and it stops while the disk is nearly full.
-- `podcast_backlog_owners`: who asked (the episodes become their jobs); a second person asking
-- for the same feed while it runs joins the run instead of starting another.
CREATE TABLE podcast_backlogs (
    id            INTEGER PRIMARY KEY,
    feed_url      TEXT NOT NULL,
    folder        TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT 'running'
                  CHECK (state IN ('running', 'paused', 'done', 'cancelled')),
    total         INTEGER NOT NULL CHECK (total >= 0),
    skipped       INTEGER NOT NULL DEFAULT 0,
    size_bytes    INTEGER,
    paused_reason TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- at most one unfinished run per feed
CREATE UNIQUE INDEX podcast_backlogs_active_feed
    ON podcast_backlogs (feed_url) WHERE state IN ('running', 'paused');

CREATE TABLE podcast_backlog_items (
    backlog_id INTEGER NOT NULL REFERENCES podcast_backlogs (id) ON DELETE CASCADE,
    position   INTEGER NOT NULL,
    key        TEXT NOT NULL,
    metadata   TEXT NOT NULL,
    size_bytes INTEGER,
    submitted  INTEGER NOT NULL DEFAULT 0 CHECK (submitted IN (0, 1)),
    PRIMARY KEY (backlog_id, key)
);

CREATE INDEX podcast_backlog_items_next
    ON podcast_backlog_items (backlog_id, submitted, position);

CREATE TABLE podcast_backlog_owners (
    backlog_id INTEGER NOT NULL REFERENCES podcast_backlogs (id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    PRIMARY KEY (backlog_id, account_id)
);
