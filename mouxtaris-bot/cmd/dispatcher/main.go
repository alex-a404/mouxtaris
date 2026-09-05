package main

import (
	"context"
	"errors"
	"log"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"time"

	"github.com/gin-gonic/gin"
	tgbotapi "github.com/go-telegram-bot-api/telegram-bot-api/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"mouxtaris.com/dispatcher/internal/dispatch"
	"mouxtaris.com/dispatcher/internal/metrics"
	"mouxtaris.com/dispatcher/internal/outages"
	resolve "mouxtaris.com/dispatcher/internal/resolver"
	"mouxtaris.com/dispatcher/internal/store"
)

// pushLog is written as JSON to stdout (captured by systemd journal) so Alloy
// can ship it to Loki and Grafana can render a per-source feed of every
// report a scraper push, with its raw text and resolution outcome, without
// hunting through metrics for it.
var pushLog = slog.New(slog.NewJSONHandler(os.Stdout, nil))

func main() {
	ctx := context.Background()

	dbURL := mustEnv("DATABASE_URL")
	botToken := mustEnv("TG_TOKEN")
	ingestToken := mustEnv("INGEST_TOKEN")
	addr := envOr("LISTEN_ADDR", ":8080")

	// Postgres pool
	pool, err := pgxpool.New(ctx, dbURL)
	if err != nil {
		log.Fatalf("postgres: %v", err)
	}
	defer pool.Close()
	if err := pool.Ping(ctx); err != nil {
		log.Fatalf("postgres ping: %v", err)
	}

	// Telegram bot
	bot, err := tgbotapi.NewBotAPI(botToken)
	if err != nil {
		log.Fatalf("telegram: %v", err)
	}
	bot.Debug = false

	// User repo
	userRepo := store.NewUserRepository(pool)
	areas, err := resolve.LoadGeoJSON("cyprus_admin.geojson", "overrides.json")
	log.Printf("loaded %d areas", len(areas))
	if err != nil {
		log.Fatalf("gazetteer: %v", err)
	}

	areaRepo := store.NewAreaRepository(pool)
	if err := areaRepo.UpsertAreas(ctx, store.ToAreaSeeds(areas)); err != nil {
		log.Fatalf("seed areas: %v", err)
	}

	resolver := resolve.New(areas)
	dispatcher := dispatch.NewService(bot, resolver, userRepo)
	outageRepo := store.NewOutageRepository(pool)

	// Separate outages.Service per source: Reconcile() marks anything missing
	// from a pushed snapshot as resolved, so a shared Service would let an
	// EOA (water) push wrongly resolve open EAC (power) outages, and vice versa.
	//
	// Each is persisted to its own file under STATE_DIRECTORY so a dispatcher
	// restart doesn't forget every outage it already announced and re-notify
	// subscribers about all of them. STATE_DIRECTORY must live outside the
	// rsync'd source tree (see deploy/install.md) -- systemd's
	// StateDirectory= sets it to a path that survives redeploys.
	stateDir := envOr("STATE_DIRECTORY", ".")
	eacStatePath := filepath.Join(stateDir, "eac_known.json")
	eoaStatePath := filepath.Join(stateDir, "eoa_known.json")
	if err := outages.EnsureDir(eacStatePath); err != nil {
		log.Fatalf("state dir: %v", err)
	}

	eacSvc := outages.NewService()
	if err := eacSvc.LoadFile(eacStatePath); err != nil {
		log.Printf("load eac state: %v", err)
	}
	eoaSvc := outages.NewService()
	if err := eoaSvc.LoadFile(eoaStatePath); err != nil {
		log.Printf("load eoa state: %v", err)
	}

	router := gin.Default()
	router.POST("/ingest/eac", ingestHandler(ingestToken, eacSvc, dispatcher, outageRepo, eacStatePath))
	router.POST("/ingest/eoa", ingestHandler(ingestToken, eoaSvc, dispatcher, outageRepo, eoaStatePath))
	router.GET("/healthz", func(c *gin.Context) { c.String(http.StatusOK, "ok") })

	// Gated by the same ingest token: port 8080 is already reachable from the
	// public internet (the EAC scraper runs on a separate machine), so an
	// unauthenticated /metrics would leak operational data to anyone.
	router.GET("/metrics", func(c *gin.Context) {
		if c.GetHeader("X-Ingest-Token") != ingestToken {
			c.Status(http.StatusUnauthorized)
			return
		}
		promhttp.Handler().ServeHTTP(c.Writer, c.Request)
	})

	srv := &http.Server{Addr: addr, Handler: router, ReadTimeout: 15 * time.Second}
	log.Printf("listening on %s", addr)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatalf("server: %v", err)
	}
}

func ingestHandler(token string, svc *outages.Service, dispatcher *dispatch.Service, outageRepo *store.OutageRepositoryImpl, statePath string) gin.HandlerFunc {
	return func(c *gin.Context) {
		source := c.GetHeader("X-Scraper-Source")
		if source == "" {
			source = "unknown"
		}

		if c.GetHeader("X-Ingest-Token") != token {
			metrics.IngestRequests.WithLabelValues(source, "unauthorized").Inc()
			c.JSON(http.StatusUnauthorized, gin.H{"error": "bad token"})
			return
		}

		var reports []outages.OutageReport
		if err := c.ShouldBindJSON(&reports); err != nil {
			metrics.IngestRequests.WithLabelValues(source, "bad_request").Inc()
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}

		// Process the snapshot: classify each row, notify on new/changed, then
		// reconcile to clear outages that vanished from this snapshot.
		present := make(map[string]struct{}, len(reports))
		var created, updated int
		for _, r := range reports {
			key := svc.KeyOf(r)
			present[key] = struct{}{}
			change := svc.ProcessReport(r)

			areaKey, area, resolved := dispatcher.Resolve(r)
			pushLog.Info("push",
				"source", source,
				"change", changeLabel(change.Kind),
				"outage_type", r.OutageType,
				"raw_location", r.RawLocation(),
				"district", r.District,
				"from", r.FromDateTime,
				"to", r.ToDateTime,
				"resolved", resolved,
				"area", area,
			)

			if err := outageRepo.UpsertOpen(c.Request.Context(), toOutageRow(key, source, r, areaKey, area, resolved)); err != nil {
				log.Printf("upsert outage: %v", err)
			}

			switch change.Kind {
			case outages.Created:
				created++
				notify(c.Request.Context(), dispatcher, source, change.Report)
			case outages.Updated:
				updated++
				notify(c.Request.Context(), dispatcher, source, change.Report)
			}
		}
		gone := svc.Reconcile(present)
		if err := svc.SaveFile(statePath); err != nil {
			log.Printf("save state %s: %v", statePath, err)
		}
		if len(gone) > 0 {
			goneKeys := make([]string, len(gone))
			for i, r := range gone {
				goneKeys[i] = svc.KeyOf(r)
			}
			if err := outageRepo.MarkResolved(c.Request.Context(), goneKeys, time.Now()); err != nil {
				log.Printf("mark resolved: %v", err)
			}
		}
		if err := outageRepo.MarkPastSchedule(c.Request.Context()); err != nil {
			log.Printf("mark past schedule: %v", err)
		}

		metrics.IngestRequests.WithLabelValues(source, "ok").Inc()
		metrics.IngestRows.WithLabelValues(source, "received").Add(float64(len(reports)))
		metrics.IngestRows.WithLabelValues(source, "created").Add(float64(created))
		metrics.IngestRows.WithLabelValues(source, "updated").Add(float64(updated))
		metrics.IngestRows.WithLabelValues(source, "resolved").Add(float64(len(gone)))
		metrics.LastSuccess.WithLabelValues(source).Set(float64(time.Now().Unix()))

		c.JSON(http.StatusOK, gin.H{
			"received": len(reports),
			"created":  created,
			"updated":  updated,
			"resolved": len(gone),
		})
	}
}

func toOutageRow(key, source string, r outages.OutageReport, areaKey, areaName string, resolved bool) store.OutageRow {
	row := store.OutageRow{
		Key:         key,
		Source:      source,
		OutageType:  string(r.OutageType),
		OutageCause: string(r.OutageCause),
		AreaName:    areaName,
		District:    r.District,
		RawLocation: r.RawLocation(),
	}
	if resolved {
		row.AreaKey = &areaKey
	}
	if t, ok := outages.ParseTime(r.FromDateTime); ok {
		row.FromAt = &t
	}
	if t, ok := outages.ParseTime(r.ToDateTime); ok {
		row.ToAt = &t
	}
	return row
}

func changeLabel(k outages.ChangeKind) string {
	switch k {
	case outages.Created:
		return "created"
	case outages.Updated:
		return "updated"
	default:
		return "unchanged"
	}
}

func notify(ctx context.Context, dispatcher *dispatch.Service, source string, r outages.OutageReport) {
	if err := dispatcher.Notify(ctx, r); err != nil {
		log.Printf("notify: %v", err)
		if errors.Is(err, dispatch.ErrUnresolved) {
			metrics.ResolveFailures.WithLabelValues(source).Inc()
		}
	}
}
