CREATE TABLE IF NOT EXISTS areas (
    key         TEXT PRIMARY KEY,            -- stable feature key (@id/wikidata/name)
    name_el     TEXT NOT NULL,               -- Greek, used for matching source text
    name_en     TEXT NOT NULL,               -- English, used for display
    level       SMALLINT NOT NULL,           -- 5 district, 6 municipality, 8 town/village
    parent_key  TEXT REFERENCES areas(key)   -- one hop up; NULL for a top-level node
);
CREATE INDEX IF NOT EXISTS areas_parent_idx ON areas(parent_key);

CREATE TABLE IF NOT EXISTS users (
    telegram_id BIGINT PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS subscriptions (
    telegram_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    area_key    TEXT   NOT NULL REFERENCES areas(key),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (telegram_id, area_key)
);

CREATE INDEX IF NOT EXISTS subs_area_idx ON subscriptions(area_key);
