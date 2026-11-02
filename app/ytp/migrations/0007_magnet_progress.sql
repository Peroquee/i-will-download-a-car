-- When a magnet job last made progress (#40): the reconciler gives up on a torrent that has had
-- no sources for MAGNET_STALL_DAYS. `progress_bytes` is how much of the torrent qBittorrent had
-- then, `progress_at` when that was last seen to change (UTC, like the other timestamps).
-- NULL until the reconciler first sees the torrent; cleared again when a failed job is retried.
ALTER TABLE jobs ADD COLUMN progress_bytes INTEGER;
ALTER TABLE jobs ADD COLUMN progress_at TEXT;
