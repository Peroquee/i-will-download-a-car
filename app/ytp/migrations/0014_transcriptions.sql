-- Videos waiting for a Whisper transcription (docs/transcription.md). Additive: nothing existing
-- changes.
--
-- A YouTube video that arrives without subtitles gets a row here. The transcription PC (woken
-- over the network by scripts/transcriber/wake.py) takes rows one at a time over the app's
-- /api/transcriber/* routes, streams the video, and sends back an SRT, which the app files next
-- to the video as `<video stem>.<lang>.srt` for Jellyfin.
--
--   queued -> running -> done          running -> queued (the PC failed or stopped answering,
--                                                         fewer than 3 attempts) or failed
--
-- `media_path` is relative to DATA_ROOT (the app's /data), so a different mount point on the
-- host changes nothing. `media_key` is a random per-attempt key that lets the PC fetch exactly
-- this one file while the row is `running`, without the worker token on a command line.
CREATE TABLE transcriptions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       INTEGER REFERENCES jobs (id) ON DELETE SET NULL,
    media_path   TEXT NOT NULL UNIQUE,
    -- The spoken language YouTube states (a hint for Whisper); NULL = let Whisper detect it.
    language     TEXT,
    status       TEXT NOT NULL DEFAULT 'queued',
    attempts     INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    worker       TEXT,
    media_key    TEXT,
    progress     INTEGER,
    heartbeat_at TEXT,
    -- What came back: the language Whisper heard, and the file (NULL = no speech, no file).
    result_lang  TEXT,
    result_path  TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    CHECK (status IN ('queued', 'running', 'done', 'failed')),
    CHECK (media_path <> '' AND substr(media_path, 1, 1) <> '/'),
    CHECK (attempts >= 0),
    CHECK (progress IS NULL OR progress BETWEEN 0 AND 100)
);

CREATE INDEX transcriptions_status ON transcriptions (status, id);

CREATE TRIGGER transcriptions_touch_updated_at
AFTER UPDATE ON transcriptions
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE transcriptions SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = NEW.id;
END;
