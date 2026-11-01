-- The original source of a job (#26): for a magnet job the full magnet link, which a retry of a
-- failed job needs (the infohash alone has no trackers). It can contain tracker addresses with a
-- private passkey, so it is never returned by the API, never logged, and only read to retry.
-- NULL for YouTube jobs (the video id and the confirmed names are enough) and for jobs made
-- before this migration (a retry then falls back to a bare infohash link).
ALTER TABLE jobs ADD COLUMN source TEXT;
