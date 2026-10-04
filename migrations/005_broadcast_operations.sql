ALTER TABLE posts ADD COLUMN operation_key TEXT;

CREATE TABLE IF NOT EXISTS broadcast_operations (
    operation_key TEXT PRIMARY KEY,
    created_by INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'processing',
    publish_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_operation_channel
    ON posts(operation_key, channel_id)
    WHERE operation_key IS NOT NULL;
