CREATE TABLE IF NOT EXISTS channel_profiles (
    channel_id INTEGER PRIMARY KEY REFERENCES channels(id) ON DELETE CASCADE,
    audience TEXT NOT NULL DEFAULT '',
    purpose TEXT NOT NULL DEFAULT '',
    key_meanings_json TEXT NOT NULL DEFAULT '[]',
    rubrics_json TEXT NOT NULL DEFAULT '[]',
    tone_of_voice TEXT NOT NULL DEFAULT '',
    cta_rules TEXT NOT NULL DEFAULT '',
    forbidden_topics_json TEXT NOT NULL DEFAULT '[]',
    analysis_status TEXT NOT NULL DEFAULT 'pending',
    analysis_version INTEGER NOT NULL DEFAULT 0,
    source_message_count INTEGER NOT NULL DEFAULT 0,
    analyzed_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS channel_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    telegram_message_id INTEGER,
    published_at TEXT,
    text TEXT NOT NULL DEFAULT '',
    source_hash TEXT NOT NULL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_channel_messages_telegram_id
    ON channel_messages(channel_id, telegram_message_id)
    WHERE telegram_message_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_channel_messages_source_hash
    ON channel_messages(channel_id, source_hash)
    WHERE telegram_message_id IS NULL;

CREATE TABLE IF NOT EXISTS content_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER REFERENCES channels(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    content_type TEXT NOT NULL DEFAULT 'idea',
    status TEXT NOT NULL DEFAULT 'idea',
    rubrics_json TEXT NOT NULL DEFAULT '[]',
    created_by INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_content_items_channel_status
    ON content_items(channel_id, status);

CREATE TABLE IF NOT EXISTS webinars (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    speaker TEXT NOT NULL DEFAULT '',
    audience TEXT NOT NULL DEFAULT '',
    problem TEXT NOT NULL DEFAULT '',
    promise TEXT NOT NULL DEFAULT '',
    agenda TEXT NOT NULL DEFAULT '',
    offer TEXT NOT NULL DEFAULT '',
    cta TEXT NOT NULL DEFAULT '',
    registration_url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    created_by INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS webinar_channels (
    webinar_id INTEGER NOT NULL REFERENCES webinars(id) ON DELETE CASCADE,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    PRIMARY KEY (webinar_id, channel_id)
);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    webinar_id INTEGER REFERENCES webinars(id) ON DELETE SET NULL,
    channel_id INTEGER NOT NULL REFERENCES channels(id),
    content_item_id INTEGER REFERENCES content_items(id) ON DELETE SET NULL,
    post_type TEXT NOT NULL DEFAULT 'useful',
    topic TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    media_type TEXT,
    media_file_id TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    publish_at TEXT,
    notified_at TEXT,
    published_at TEXT,
    telegram_message_id TEXT,
    generation_error TEXT,
    publish_error TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER,
    approved_by INTEGER,
    published_by INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_posts_status_publish_at
    ON posts(status, publish_at);
CREATE INDEX IF NOT EXISTS idx_posts_webinar
    ON posts(webinar_id);

CREATE TABLE IF NOT EXISTS post_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    actor_type TEXT NOT NULL,
    actor_id TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_post_events_post_created
    ON post_events(post_id, created_at);

CREATE TABLE IF NOT EXISTS web_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS login_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    used_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_login_tokens_expiry
    ON login_tokens(expires_at);
