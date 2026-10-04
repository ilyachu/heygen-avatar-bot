CREATE TABLE IF NOT EXISTS quality_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    post_version INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    total_score REAL NOT NULL CHECK (total_score >= 0 AND total_score <= 100),
    criteria_json TEXT NOT NULL,
    is_blocking INTEGER NOT NULL DEFAULT 0 CHECK (is_blocking IN (0, 1)),
    issues_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_quality_reviews_post_latest
    ON quality_reviews(post_id, id DESC);

CREATE TABLE IF NOT EXISTS post_metric_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    collected_at TEXT NOT NULL,
    collection_window TEXT NOT NULL DEFAULT '',
    views INTEGER NOT NULL DEFAULT 0 CHECK (views >= 0),
    forwards INTEGER NOT NULL DEFAULT 0 CHECK (forwards >= 0),
    reactions INTEGER NOT NULL DEFAULT 0 CHECK (reactions >= 0),
    reactions_json TEXT NOT NULL DEFAULT '{}',
    provider TEXT NOT NULL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_post_metric_snapshots_window
    ON post_metric_snapshots(post_id, collection_window)
    WHERE collection_window != '';

CREATE UNIQUE INDEX IF NOT EXISTS idx_post_metric_snapshots_collected_at
    ON post_metric_snapshots(post_id, collected_at)
    WHERE collection_window = '';

CREATE INDEX IF NOT EXISTS idx_post_metric_snapshots_post_latest
    ON post_metric_snapshots(post_id, collected_at DESC);
