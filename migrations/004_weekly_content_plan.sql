ALTER TABLE channels ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1
    CHECK (is_active IN (0, 1));

ALTER TABLE webinars ADD COLUMN week_start TEXT;

ALTER TABLE posts ADD COLUMN plan_month TEXT;
ALTER TABLE posts ADD COLUMN week_start TEXT;
ALTER TABLE posts ADD COLUMN requires_link INTEGER NOT NULL DEFAULT 0
    CHECK (requires_link IN (0, 1));
ALTER TABLE posts ADD COLUMN include_webinar_link INTEGER NOT NULL DEFAULT 0
    CHECK (include_webinar_link IN (0, 1));

CREATE TABLE IF NOT EXISTS weekly_content_templates (
    post_type TEXT PRIMARY KEY,
    weekday_offset INTEGER NOT NULL CHECK (weekday_offset BETWEEN 0 AND 6),
    publish_time TEXT NOT NULL DEFAULT '10:00:00',
    requires_link INTEGER NOT NULL DEFAULT 0 CHECK (requires_link IN (0, 1)),
    include_webinar_link INTEGER NOT NULL DEFAULT 0
        CHECK (include_webinar_link IN (0, 1)),
    sort_order INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT OR IGNORE INTO weekly_content_templates (
    post_type, weekday_offset, publish_time, requires_link,
    include_webinar_link, sort_order
) VALUES
    ('useful', 1, '10:00:00', 0, 0, 10),
    ('warming', 3, '10:00:00', 0, 1, 20),
    ('selling', 5, '10:00:00', 1, 1, 30);

CREATE TABLE IF NOT EXISTS monthly_useful_topic_slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_month TEXT NOT NULL,
    week_start TEXT NOT NULL,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    post_id INTEGER NOT NULL UNIQUE REFERENCES posts(id) ON DELETE CASCADE,
    topic TEXT NOT NULL DEFAULT '',
    created_by INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (plan_month, week_start, channel_id)
);


CREATE UNIQUE INDEX IF NOT EXISTS idx_webinars_week_start
    ON webinars(week_start);
CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_weekly_type
    ON posts(week_start, channel_id, post_type);
CREATE INDEX IF NOT EXISTS idx_posts_plan_month_week
    ON posts(plan_month, week_start, post_type);
CREATE INDEX IF NOT EXISTS idx_monthly_topic_slots_month
    ON monthly_useful_topic_slots(plan_month, week_start, channel_id);
