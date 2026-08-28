package store

import "context"

type SubscriptionInfo struct {
	AreaKey string
	NameEN  string
}

type UserRepository interface {
	SubscribersForAreas(ctx context.Context, areaKeys []string) ([]int64, error)
	UpsertUser(ctx context.Context, telegramID int64) error
	Subscribe(ctx context.Context, telegramID int64, areaKey string) error
	Unsubscribe(ctx context.Context, telegramID int64, areaKey string) error
	ListSubscriptions(ctx context.Context, telegramID int64) ([]SubscriptionInfo, error)
	DeleteUser(ctx context.Context, telegramID int64) error
}
