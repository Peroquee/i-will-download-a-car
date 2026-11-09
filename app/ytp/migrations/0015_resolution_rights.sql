-- Resolution per subscription and per user, with a resolution privilege (issue #65).
--
-- Values everywhere: 0 = best, or one of 2160, 1440, 1080, 720, 480, 360; NULL = not set.
--
-- `accounts.default_max_height`: the user's own default (preselected in the form, used when a
-- download sends none). `accounts.can_set_resolution`: whether an admin lets this user choose
-- resolutions at all; existing users keep choosing (1), so nothing changes for them.
-- `subscriptions.max_height`: the cap for new videos of that channel; subscriptions are shared,
-- not per user, and NULL means the server default (YOUTUBE_MAX_HEIGHT).
ALTER TABLE accounts ADD COLUMN default_max_height INTEGER NULL
    CHECK (default_max_height IS NULL
           OR default_max_height IN (0, 2160, 1440, 1080, 720, 480, 360));

ALTER TABLE accounts ADD COLUMN can_set_resolution INTEGER NOT NULL DEFAULT 1
    CHECK (can_set_resolution IN (0, 1));

ALTER TABLE subscriptions ADD COLUMN max_height INTEGER NULL
    CHECK (max_height IS NULL
           OR max_height IN (0, 2160, 1440, 1080, 720, 480, 360));
