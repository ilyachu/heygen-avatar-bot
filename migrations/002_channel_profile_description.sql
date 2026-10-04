ALTER TABLE channel_profiles ADD COLUMN description TEXT NOT NULL DEFAULT '';
ALTER TABLE channel_profiles ADD COLUMN channel_kind TEXT NOT NULL DEFAULT 'thematic';
