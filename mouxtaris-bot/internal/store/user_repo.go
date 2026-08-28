package store

import (
	"context"
	"fmt"

	"github.com/jackc/pgx/v5/pgxpool"
)

type UserRepositoryImpl struct {
	db *pgxpool.Pool
}

func NewUserRepository(db *pgxpool.Pool) *UserRepositoryImpl {
	return &UserRepositoryImpl{db: db}
}

func (u *UserRepositoryImpl) SubscribersForAreas(ctx context.Context, areaKeys []string) ([]int64, error) {
	if len(areaKeys) == 0 {
		return nil, nil
	}
	const q = `
		SELECT DISTINCT telegram_id
		FROM subscriptions
		WHERE area_key = ANY($1)`
	rows, err := u.db.Query(ctx, q, areaKeys)
	if err != nil {
		return nil, fmt.Errorf("query subscribers: %w", err)
	}
	defer rows.Close()

	var ids []int64
	for rows.Next() {
		var id int64
		if err := rows.Scan(&id); err != nil {
			return nil, fmt.Errorf("scan subscriber: %w", err)
		}
		ids = append(ids, id)
	}
	return ids, rows.Err()
}

func (u *UserRepositoryImpl) UpsertUser(ctx context.Context, telegramID int64) error {
	_, err := u.db.Exec(ctx,
		`INSERT INTO users (telegram_id) VALUES ($1) ON CONFLICT DO NOTHING`,
		telegramID,
	)
	return err
}

func (u *UserRepositoryImpl) Subscribe(ctx context.Context, telegramID int64, areaKey string) error {
	_, err := u.db.Exec(ctx,
		`INSERT INTO subscriptions (telegram_id, area_key) VALUES ($1, $2) ON CONFLICT DO NOTHING`,
		telegramID, areaKey,
	)
	return err
}

func (u *UserRepositoryImpl) Unsubscribe(ctx context.Context, telegramID int64, areaKey string) error {
	_, err := u.db.Exec(ctx,
		`DELETE FROM subscriptions WHERE telegram_id = $1 AND area_key = $2`,
		telegramID, areaKey,
	)
	return err
}

func (u *UserRepositoryImpl) ListSubscriptions(ctx context.Context, telegramID int64) ([]SubscriptionInfo, error) {
	const q = `
		SELECT s.area_key, a.name_en
		FROM subscriptions s
		JOIN areas a ON a.key = s.area_key
		WHERE s.telegram_id = $1
		ORDER BY a.name_en`
	rows, err := u.db.Query(ctx, q, telegramID)
	if err != nil {
		return nil, fmt.Errorf("list subscriptions: %w", err)
	}
	defer rows.Close()

	var subs []SubscriptionInfo
	for rows.Next() {
		var s SubscriptionInfo
		if err := rows.Scan(&s.AreaKey, &s.NameEN); err != nil {
			return nil, fmt.Errorf("scan subscription: %w", err)
		}
		subs = append(subs, s)
	}
	return subs, rows.Err()
}
