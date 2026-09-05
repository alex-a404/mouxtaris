package outages

import (
	"crypto/sha1"
	"encoding/hex"
	"strings"
	"sync"
	"time"
)

type ChangeKind int

const (
	Unchanged ChangeKind = iota // already known, nothing material changed -> don't notify
	Created                     // first time we've seen this outage -> notify
	Updated                     // known outage, restoration time moved -> notify (new wording)
)

// resolveGrace is how long an outage must be absent from scrape snapshots
// before Reconcile treats it as resolved. Without this, a single missed or
// partial scrape (site hiccup, pagination glitch) would drop the outage from
// known and cause it to be re-announced as brand new on the next poll.
const resolveGrace = 30 * time.Minute

type (
	Service struct {
		mu    sync.Mutex
		known map[string]knownOutage
	}

	knownOutage struct {
		Report   OutageReport
		LastSeen time.Time
	}

	Change struct {
		Kind   ChangeKind
		Report OutageReport
	}
)

func NewService() *Service {
	return &Service{known: make(map[string]knownOutage)}
}

func (s *Service) ProcessReport(r OutageReport) Change {
	key := naturalKey(r)
	now := time.Now()

	s.mu.Lock()
	defer s.mu.Unlock()

	prev, ok := s.known[key]
	if !ok {
		s.known[key] = knownOutage{Report: r, LastSeen: now}
		return Change{Kind: Created, Report: r}
	}

	if restorationChanged(prev.Report, r) {
		s.known[key] = knownOutage{Report: r, LastSeen: now}
		return Change{Kind: Updated, Report: r}
	}

	s.known[key] = knownOutage{Report: r, LastSeen: now} // refresh last-seen even on no-op
	return Change{Kind: Unchanged, Report: r}
}

func (s *Service) Reconcile(presentKeys map[string]struct{}) []OutageReport {
	now := time.Now()

	s.mu.Lock()
	defer s.mu.Unlock()

	var gone []OutageReport
	for key, entry := range s.known {
		if _, still := presentKeys[key]; still {
			continue
		}
		if now.Sub(entry.LastSeen) < resolveGrace {
			continue // missing from this snapshot, but not long enough to call it resolved
		}
		gone = append(gone, entry.Report)
		delete(s.known, key)
	}
	return gone
}

func (s *Service) KeyOf(r OutageReport) string { return naturalKey(r) }

func naturalKey(r OutageReport) string {
	basis := strings.Join([]string{
		strings.ToLower(string(r.OutageType)),
		strings.ToLower(r.District),     // district label
		norm(r.TownVillage),             // coarse location field
		norm(r.AreaSubdistrict),         // finer location field
		norm(r.CustomPartOfArea),        // free-text detail
		strings.TrimSpace(r.FromDateTime), // start, as published
	}, "|")
	sum := sha1.Sum([]byte(basis))
	return hex.EncodeToString(sum[:8])
}

func restorationChanged(a, b OutageReport) bool {
	return strings.TrimSpace(a.ToDateTime) != strings.TrimSpace(b.ToDateTime)
}

func norm(s string) string {
	return strings.Join(strings.Fields(strings.ToLower(strings.TrimSpace(s))), " ")
}

