ALTER TABLE posts ADD COLUMN generation_rationale TEXT;
ALTER TABLE posts ADD COLUMN generation_warnings_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE posts ADD COLUMN notification_chat_id TEXT;
ALTER TABLE posts ADD COLUMN notification_message_id INTEGER;
ALTER TABLE posts ADD COLUMN notification_error TEXT;
ALTER TABLE posts ADD COLUMN claim_until TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_campaign_slot
    ON posts(webinar_id, channel_id, post_type);
