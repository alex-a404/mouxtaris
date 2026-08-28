package main

import (
	"context"
	"log"
	"os"

	tgbotapi "github.com/go-telegram-bot-api/telegram-bot-api/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"mouxtaris.com/dispatcher/internal/bot"
	resolve "mouxtaris.com/dispatcher/internal/resolver"
	"mouxtaris.com/dispatcher/internal/store"
)

func main() {
	ctx := context.Background()

	dbURL := mustEnv("DATABASE_URL")
	botToken := mustEnv("TG_TOKEN")

	pool, err := pgxpool.New(ctx, dbURL)
	if err != nil {
		log.Fatalf("postgres: %v", err)
	}
	defer pool.Close()
	if err := pool.Ping(ctx); err != nil {
		log.Fatalf("postgres ping: %v", err)
	}

	api, err := tgbotapi.NewBotAPI(botToken)
	if err != nil {
		log.Fatalf("telegram: %v", err)
	}
	api.Debug = false
	log.Printf("bot: authorised as @%s", api.Self.UserName)

	areas, err := resolve.LoadGeoJSON("cyprus_admin.geojson", "overrides.json")
	if err != nil {
		log.Fatalf("gazetteer: %v", err)
	}
	log.Printf("bot: loaded %d areas", len(areas))

	areaRepo := store.NewAreaRepository(pool)
	if err := areaRepo.UpsertAreas(ctx, toAreaSeeds(areas)); err != nil {
		log.Fatalf("seed areas: %v", err)
	}

	userRepo := store.NewUserRepository(pool)
	resolver := resolve.New(areas)

	b := bot.New(api, userRepo, resolver)
	b.Run(ctx)
}

func mustEnv(k string) string {
	v := os.Getenv(k)
	if v == "" {
		log.Fatalf("missing env %s", k)
	}
	return v
}

func toAreaSeeds(areas []*resolve.Area) []store.AreaSeed {
	seeds := make([]store.AreaSeed, len(areas))
	for i, a := range areas {
		seeds[i] = store.AreaSeed{
			Key:       a.Key,
			NameEL:    a.NameEL,
			NameEN:    a.NameEN,
			Level:     a.Level,
			ParentKey: a.ParentKey,
		}
	}
	return seeds
}
