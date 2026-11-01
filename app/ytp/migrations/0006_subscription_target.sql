-- Where a subscribed channel's new videos go (issue #35): `video` = the Youtube library as
-- before, `podcast` = episodes of a podcast named after the channel, in the Podcasts library,
-- optionally `audio_only` (the audio stream with tags and cover, no video). Existing
-- subscriptions keep behaving as they did: video, with the picture.
ALTER TABLE subscriptions ADD COLUMN target TEXT NOT NULL DEFAULT 'video'
    CHECK (target IN ('video', 'podcast'));
ALTER TABLE subscriptions ADD COLUMN audio_only INTEGER NOT NULL DEFAULT 0
    CHECK (audio_only IN (0, 1));
