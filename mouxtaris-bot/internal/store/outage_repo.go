package store

import (
	"context"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

// OutageRow is one outage's current state, for the public map dashboard.
type OutageRow struct {
	Key         string
	Source      string
	OutageType  string
	OutageCause string
	AreaKey     *string // nil if never resolved to a known area
	AreaName    string
	District    string
	RawLocation string
	FromAt      *time.Time
	ToAt        *time.Time
}

type OutageRepositoryImpl struct {
	db *pgxpool.Pool
}

func NewOutageRepository(db *pgxpool.Pool) *OutageRepositoryImpl {
	return &OutageRepositoryImpl{db: db}
}

// UpsertOpen records an outage as currently open (whether it's brand new,
// changed, or just confirmed still present on this poll). resolved_at is
// reset to NULL on every call -- if a row is being upserted, it's present
// in the latest snapshot, so any prior resolution was premature.
func (o *OutageRepositoryImpl) UpsertOpen(ctx context.Context, row OutageRow) error {
	_, err := o.db.Exec(ctx,
		`INSERT INTO outages (key, source, outage_type, outage_cause, area_key, area_name, district, raw_location, from_at, to_at)
		 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
		 ON CONFLICT (key) DO UPDATE SET
		   outage_cause = EXCLUDED.outage_cause,
		   area_key     = EXCLUDED.area_key,
		   area_name    = EXCLUDED.area_name,
		   district     = EXCLUDED.district,
		   raw_location = EXCLUDED.raw_location,
		   to_at        = EXCLUDED.to_at,
		   last_seen    = now(),
		   resolved_at  = NULL`,
		row.Key, row.Source, row.OutageType, row.OutageCause,
		row.AreaKey, row.AreaName, row.District, row.RawLocation, row.FromAt, row.ToAt,
	)
	if err != nil {
		return fmt.Errorf("upsert outage %q: %w", row.Key, err)
	}
	return nil
}

// MarkResolved stamps resolved_at for outages that Reconcile has decided are
// gone (missing from scrape snapshots for longer than the grace period).
func (o *OutageRepositoryImpl) MarkResolved(ctx context.Context, keys []string, at time.Time) error {
	if len(keys) == 0 {
		return nil
	}
	_, err := o.db.Exec(ctx,
		`UPDATE outages SET resolved_at = $2 WHERE key = ANY($1) AND resolved_at IS NULL`,
		keys, at,
	)
	if err != nil {
		return fmt.Errorf("mark resolved: %w", err)
	}
	return nil
}

// MarkPastSchedule stamps resolved_at = to_at for any still-open outage whose
// own stated restoration time has already passed. This is the second, and
// for EOA/water sources the primary, way an outage gets resolved: those
// scrapers pull an announcement archive rather than a live status board, so
// an old post can keep reappearing in every snapshot indefinitely -- without
// this, MarkResolved's feed-absence check alone would never fire for them.
// Must run after UpsertOpen in the same request, since UpsertOpen
// unconditionally resets resolved_at to NULL for every row still present in
// the snapshot.
func (o *OutageRepositoryImpl) MarkPastSchedule(ctx context.Context) error {
	_, err := o.db.Exec(ctx,
		`UPDATE outages SET resolved_at = to_at WHERE resolved_at IS NULL AND to_at IS NOT NULL AND to_at <= now()`,
	)
	if err != nil {
		return fmt.Errorf("mark past schedule: %w", err)
	}
	return nil
}
