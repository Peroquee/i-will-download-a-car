-- Indexes that match the queries, and the CHECK constraints the schema was missing (issue #72).
--
-- 1. Indexes.
--    * `jobs_status` (status) is redundant: `jobs_status_priority` (status, priority DESC, id)
--      from 0014 begins with `status` and serves every lookup that only filters by status.
--    * `jobs_created_at` (created_at DESC) serves no query: the job list sorts by `id`, the
--      finished list and the monitor by `updated_at`.
--    * New `jobs_status_updated_at` for `jobs.recent_finished` (status IN ('done', 'failed'),
--      ORDER BY updated_at DESC) and the monitor's look every 15 seconds for failed jobs
--      (status = 'failed' AND updated_at > cursor).
--    * New `podcast_backlog_items_key`: the back-catalogue feeder joins `i.key = j.external_id`
--      (the table's primary key is (backlog_id, key), so an index on `key` alone was missing).
--    * New `podcast_backlog_owners_account`: "which runs belong to this account" filters on
--      account_id alone, and the primary key starts with backlog_id.
--
-- 2. CHECK constraints. SQLite cannot add a CHECK to an existing table, so the four tables are
--    rebuilt (same columns, defaults and rows; every index and trigger is created again, and
--    the jobs AUTOINCREMENT counter is carried over so no job id is ever reused).
--    The runner switches foreign keys off while a file runs, so dropping a referenced table
--    does not cascade into its children; `PRAGMA foreign_key_check` must pass before COMMIT
--    (see ytp/db.py). All existing rows come from writers that always store valid values
--    (priority 0/1, json.dumps of a dict for params/metadata, monitor.vpn_condition).
--    * jobs.priority: only 0 and 1 exist (see 0014); the database now enforces that too.
--    * alerts.params and podcast_backlog_items.metadata: must be a JSON object, like
--      jobs.metadata has been since 0001.
--    * vpn_events.condition: the fixed set of `monitor.vpn_condition`
--      (ok, down, blind, mismatch, starting, unknown).
--
-- `job_dismissals.dismissed_at` (0013) is not read by a query today, but is kept on purpose:
-- it records when an account hid a failed job, which is useful for support and incident
-- analysis. 0013 has shipped, so it is not edited.

-- ------------------------------------------------------------------- jobs (rebuilt)
-- The rebuild also drops jobs_status and jobs_created_at and creates the final index set.
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
    progress_bytes INTEGER,
    progress_at TEXT,
    priority    INTEGER NOT NULL DEFAULT 0,
    note        TEXT,

    CHECK (type IN ('magnet', 'youtube', 'podcast')),
    CHECK (external_id <> ''),
    CHECK (json_valid(metadata) AND json_type(metadata) = 'object'),
    CHECK (status IN ('queued', 'downloading', 'organizing', 'done', 'failed')),
    CHECK (priority IN (0, 1))
);

INSERT INTO jobs_new (
    id, type, external_id, metadata, status, error, created_at, updated_at, source,
    progress_bytes, progress_at, priority, note
)
SELECT
    id, type, external_id, metadata, status, error, created_at, updated_at, source,
    progress_bytes, progress_at, priority, note
FROM jobs;

-- Jobs are AUTOINCREMENT: an id is never handed out twice. The copy above only sets the new
-- table's counter to MAX(id), losing ids that were deleted at the top (production: the old
-- counter is 344 while MAX(id) is 342, so 343 and 344 are free to be reused after a rebuild).
-- Carry the old counter over while both sqlite_sequence rows can still exist; the RENAME moves
-- the row to the new name. When jobs is empty, the copy created no row for jobs_new (and the
-- old counter may still exist), so that case needs an INSERT instead of an UPDATE.
INSERT INTO sqlite_sequence (name, seq)
    SELECT 'jobs_new', seq FROM sqlite_sequence WHERE name = 'jobs'
    AND NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = 'jobs_new');

UPDATE sqlite_sequence
   SET seq = (SELECT seq FROM sqlite_sequence WHERE name = 'jobs')
 WHERE name = 'jobs_new'
   AND seq < (SELECT seq FROM sqlite_sequence WHERE name = 'jobs');

DROP TABLE jobs;  -- drops the old indexes and the two triggers that were ON jobs
ALTER TABLE jobs_new RENAME TO jobs;

CREATE UNIQUE INDEX jobs_type_external_id ON jobs (type, external_id);
CREATE INDEX jobs_status_priority ON jobs (status, priority DESC, id);
CREATE INDEX jobs_status_updated_at ON jobs (status, updated_at);

-- Keeps updated_at honest without every caller remembering it (as in 0001/0005).
CREATE TRIGGER jobs_touch_updated_at
AFTER UPDATE ON jobs
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE jobs SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = NEW.id;
END;

-- Drops the dismissals as soon as a job leaves `failed` (as in 0013).
CREATE TRIGGER job_dismissals_cleared AFTER UPDATE OF status ON jobs
WHEN NEW.status <> 'failed'
BEGIN
    DELETE FROM job_dismissals WHERE job_id = NEW.id;
END;

-- ------------------------------------------------------------------- alerts (rebuilt)
CREATE TABLE alerts_new (
    id              INTEGER PRIMARY KEY,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    kind            TEXT NOT NULL,
    severity        TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
    title           TEXT NOT NULL,
    detail          TEXT NOT NULL DEFAULT '',
    params          TEXT NOT NULL DEFAULT '{}'
                    CHECK (json_valid(params) AND json_type(params) = 'object'),
    dedupe_key      TEXT UNIQUE,
    acknowledged    INTEGER NOT NULL DEFAULT 0 CHECK (acknowledged IN (0, 1)),
    acknowledged_at TEXT
);

INSERT INTO alerts_new (
    id, created_at, kind, severity, title, detail, params, dedupe_key,
    acknowledged, acknowledged_at
)
SELECT
    id, created_at, kind, severity, title, detail, params, dedupe_key,
    acknowledged, acknowledged_at
FROM alerts;

DROP TABLE alerts;
ALTER TABLE alerts_new RENAME TO alerts;

-- ------------------------------------------------------- podcast_backlog_items (rebuilt)
CREATE TABLE podcast_backlog_items_new (
    backlog_id INTEGER NOT NULL REFERENCES podcast_backlogs (id) ON DELETE CASCADE,
    position   INTEGER NOT NULL,
    key        TEXT NOT NULL,
    metadata   TEXT NOT NULL CHECK (json_valid(metadata) AND json_type(metadata) = 'object'),
    size_bytes INTEGER,
    submitted  INTEGER NOT NULL DEFAULT 0 CHECK (submitted IN (0, 1)),
    PRIMARY KEY (backlog_id, key)
);

INSERT INTO podcast_backlog_items_new (backlog_id, position, key, metadata, size_bytes, submitted)
SELECT backlog_id, position, key, metadata, size_bytes, submitted FROM podcast_backlog_items;

DROP TABLE podcast_backlog_items;
ALTER TABLE podcast_backlog_items_new RENAME TO podcast_backlog_items;

CREATE INDEX podcast_backlog_items_next
    ON podcast_backlog_items (backlog_id, submitted, position);
CREATE INDEX podcast_backlog_items_key ON podcast_backlog_items (key);

-- ------------------------------------------------------------------ vpn_events (rebuilt)
CREATE TABLE vpn_events_new (
    id         INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    condition  TEXT NOT NULL
               CHECK (condition IN ('ok', 'down', 'blind', 'mismatch', 'starting', 'unknown')),
    paused     INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
    reason     TEXT NOT NULL DEFAULT ''
);

INSERT INTO vpn_events_new (id, created_at, condition, paused, reason)
    SELECT id, created_at, condition, paused, reason FROM vpn_events;

DROP TABLE vpn_events;
ALTER TABLE vpn_events_new RENAME TO vpn_events;

-- ------------------------------------------------------------------ podcast_backlog_owners
CREATE INDEX podcast_backlog_owners_account ON podcast_backlog_owners (account_id);
