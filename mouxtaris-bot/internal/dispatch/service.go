package dispatch

import (
	"context"
	"errors"
	"fmt"
	"time"

	"strings"

	tgbotapi "github.com/go-telegram-bot-api/telegram-bot-api/v5"
	"mouxtaris.com/dispatcher/internal/outages"
	"mouxtaris.com/dispatcher/internal/store"
)

// ErrUnresolved is wrapped into Notify's error when the outage's location
// couldn't be matched to a known area -- callers use errors.Is to tell this
// apart from downstream failures (DB lookup, Telegram send) for metrics.
var ErrUnresolved = errors.New("outage could not be resolved to a known area")

type (
	Sender interface {
		Send(tgbotapi.Chattable) (tgbotapi.Message, error)
	}

	Service struct {
		bot      Sender
		resolver Resolver
		userRepo store.UserRepository
	}
)

func NewService(bot Sender, resolver Resolver, userRepo store.UserRepository) *Service {
	return &Service{bot: bot, resolver: resolver, userRepo: userRepo}
}

// Resolve exposes area resolution without sending anything, so callers can
// log/observe what a raw outage report resolves to independent of whether
// it's new enough to notify subscribers about.
func (s *Service) Resolve(outage outages.OutageReport) (areaName string, ok bool) {
	_, displayName, _, ok := s.resolver.Resolve(outage)
	return displayName, ok
}

// Notify determines who to notify when outage recieved
func (s *Service) Notify(ctx context.Context, outage outages.OutageReport) error {
	areaKey, displayName, matchedSubdistrict, ok := s.resolver.Resolve(outage)
	if !ok {
		return fmt.Errorf("dispatch: %w (%q)", ErrUnresolved, outage.RawLocation())
	}

	chain := s.resolver.Chain(areaKey)

	userIDs, err := s.userRepo.SubscribersForAreas(ctx, chain)
	if err != nil {
		return fmt.Errorf("dispatch: find subscribers: %w", err)
	}

	msg := renderOutage(outage, displayName, matchedSubdistrict)
	var sendErr error
	for _, id := range userIDs {
		m := tgbotapi.NewMessage(id, msg)
		m.ParseMode = tgbotapi.ModeHTML
		m.DisableWebPagePreview = true
		if _, err := s.bot.Send(m); err != nil {
			sendErr = fmt.Errorf("dispatch: send to %d: %w", id, err) // keep going
		}
	}
	return sendErr
}

func renderOutage(o outages.OutageReport, areaName string, matchedSubdistrict bool) string {
	icon, kind := "⚡", "Power"
	if o.OutageType == outages.OutageTypeWater {
		icon, kind = "💧", "Water"
	}

	loc := areaName
	// append the finer raw detail EAC provided, which the resolved name may not
	// carry — but skip it when the resolver already matched on area_subdistrict
	// (the resolved name IS that subdistrict, just possibly reworded, e.g.
	// "Apostolos Varnavas & Agios Makarios" vs raw "... kai ..."), otherwise
	// it shows up twice.
	if a := strings.TrimSpace(o.AreaSubdistrict); a != "" && !matchedSubdistrict {
		loc = fmt.Sprintf("%s - %s", loc, a)
	}
	if p := strings.TrimSpace(o.CustomPartOfArea); p != "" {
		loc = fmt.Sprintf("%s (%s)", loc, p)
	}

	msg := fmt.Sprintf(
		"%s<b>%s %s</b>\n"+
			"📍Area: <b>%s</b>\n"+
			"⏰From: %s\n"+
			"Est. restore: %s",
		icon, kind, o.StatusLabel(), loc, fmtTime(o.FromDateTime), fmtTime(o.ToDateTime),
	)
	return msg
}

func fmtTime(s string) string {
	s = strings.TrimSpace(s)
	if s == "" {
		return "—"
	}
	for _, layout := range []string{time.RFC3339, "2006-01-02T15:04:05", "2006-01-02 15:04:05"} {
		if t, err := time.Parse(layout, s); err == nil {
			return t.Format("2 Jan 15:04")
		}
	}
	return s // unparseable: show as-is rather than blank
}
