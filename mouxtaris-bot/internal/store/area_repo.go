package store

import (
	"context"
	"fmt"

	"github.com/jackc/pgx/v5/pgxpool"
)

// AreaSeed is a gazetteer entry to load into the areas table.
type AreaSeed struct {
	Key       string
	NameEL    string
	NameEN    string
	Level     int
	ParentKey string
	Lat, Lon  *float64 // nil if the source feature had no geometry
}

type AreaRepositoryImpl struct {
	db *pgxpool.Pool
}

func NewAreaRepository(db *pgxpool.Pool) *AreaRepositoryImpl {
	return &AreaRepositoryImpl{db: db}
}

func (a *AreaRepositoryImpl) UpsertAreas(ctx context.Context, areas []AreaSeed) error {
	tx, err := a.db.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin: %w", err)
	}
	defer tx.Rollback(ctx)

	for _, ar := range areas {
		if _, err := tx.Exec(ctx,
			`INSERT INTO areas (key, name_el, name_en, level, parent_key, lat, lon)
			 VALUES ($1, $2, $3, $4, NULL, $5, $6)
			 ON CONFLICT (key) DO UPDATE SET
			   name_el = EXCLUDED.name_el,
			   name_en = EXCLUDED.name_en,
			   level   = EXCLUDED.level,
			   lat     = EXCLUDED.lat,
			   lon     = EXCLUDED.lon`,
			ar.Key, ar.NameEL, ar.NameEN, ar.Level, ar.Lat, ar.Lon,
		); err != nil {
			return fmt.Errorf("upsert area %q: %w", ar.Key, err)
		}
	}

	for _, ar := range areas {
		if ar.ParentKey == "" {
			continue
		}
		if _, err := tx.Exec(ctx,
			`UPDATE areas SET parent_key = $2 WHERE key = $1`,
			ar.Key, ar.ParentKey,
		); err != nil {
			return fmt.Errorf("set parent for area %q: %w", ar.Key, err)
		}
	}

	return tx.Commit(ctx)
}
