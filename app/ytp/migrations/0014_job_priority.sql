-- High-priority downloads (issue #51).
--
-- A job can be marked high priority (1) when it is created; 0 is the normal download. While a
-- high-priority job is downloading, the app holds normal work back: normal YouTube/podcast jobs
-- are not claimed and normal torrents are paused (see priority.py). Existing jobs keep working
-- unchanged: they are all normal.
ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0;

-- Queue-picking looks for the highest priority among the queued jobs of a type (jobs.py).
CREATE INDEX jobs_status_priority ON jobs (status, priority DESC, id);

-- Torrents the APP paused itself while a high-priority download runs. Only these hashes are
-- resumed once no high-priority job downloads any more; a torrent the user paused is never in
-- this table and is therefore never resumed by the app. The rows live in the database (not in
-- memory) so a restart in the middle of a high-priority download still resumes them later.
CREATE TABLE priority_paused (
    infohash  TEXT PRIMARY KEY,
    paused_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    CHECK (infohash <> '')
) WITHOUT ROWID;
