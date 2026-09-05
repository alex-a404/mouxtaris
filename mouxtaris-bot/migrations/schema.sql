CREATE TABLE IF NOT EXISTS areas (
    key         TEXT PRIMARY KEY,            -- stable feature key (@id/wikidata/name)
    name_el     TEXT NOT NULL,               -- Greek, used for matching source text
    name_en     TEXT NOT NULL,               -- English, used for display
    level       SMALLINT NOT NULL,           -- 5 district, 6 municipality, 8 town/village
    parent_key  TEXT REFERENCES areas(key),  -- one hop up; NULL for a top-level node
    lat         DOUBLE PRECISION,            -- centroid, for map display; NULL if the
    lon         DOUBLE PRECISION             -- source feature had no geometry (e.g. a
                                              -- manually added overrides.json entry)
);
CREATE INDEX IF NOT EXISTS areas_parent_idx ON areas(parent_key);

ALTER TABLE areas ADD COLUMN IF NOT EXISTS lat DOUBLE PRECISION;
ALTER TABLE areas ADD COLUMN IF NOT EXISTS lon DOUBLE PRECISION;

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

CREATE TABLE IF NOT EXISTS outages (
    key          TEXT PRIMARY KEY,             -- outages.Service's natural key
    source       TEXT NOT NULL,                -- "eac" | "eoa"
    outage_type  TEXT NOT NULL,                -- "power" | "water"
    outage_cause TEXT NOT NULL,                -- "fault" | "scheduled"
    area_key     TEXT REFERENCES areas(key),   -- NULL if never resolved to a known area
    area_name    TEXT NOT NULL,                -- display name as of last_seen
    district     TEXT NOT NULL,
    raw_location TEXT NOT NULL,
    from_at      TIMESTAMPTZ,                  -- NULL if unparseable
    to_at        TIMESTAMPTZ,
    first_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at  TIMESTAMPTZ                   -- NULL while still open
);
CREATE INDEX IF NOT EXISTS outages_open_idx ON outages (resolved_at) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS outages_area_idx ON outages (area_key);

DROP VIEW IF EXISTS outages_view; -- superseded: resolved_at itself now
                                   -- accounts for a passed to_at (see
                                   -- OutageRepositoryImpl.MarkPastSchedule)

DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'grafana_public') THEN
        GRANT SELECT ON outages, areas TO grafana_public;
    END IF;
END
$$;
