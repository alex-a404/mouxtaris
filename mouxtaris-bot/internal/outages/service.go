package outages

import (
	"crypto/sha1"
	"encoding/hex"
	"strings"
	"sync"
)

type ChangeKind int

const (
	Unchanged ChangeKind = iota // already known, nothing material changed -> don't notify
	Created                     // first time we've seen this outage -> notify
	Updated                     // known outage, restoration time moved -> notify (new wording)
)

type (
	Service struct {
		mu    sync.Mutex
		known map[string]OutageReport
	}

	Change struct {
		Kind   ChangeKind
		Report OutageReport
	}
)

func NewService() *Service {
	return &Service{known: make(map[string]OutageReport)}
}

func (s *Service) ProcessReport(r OutageReport) Change {
	key := naturalKey(r)

	s.mu.Lock()
	defer s.mu.Unlock()

	prev, ok := s.known[key]
	if !ok {
		s.known[key] = r
		return Change{Kind: Created, Report: r}
	}

	if restorationChanged(prev, r) {
		s.known[key] = r
		return Change{Kind: Updated, Report: r}
	}

	s.known[key] = r // refresh last-seen even on no-op
	return Change{Kind: Unchanged, Report: r}
}

func (s *Service) Reconcile(presentKeys map[string]struct{}) []OutageReport {
	s.mu.Lock()
	defer s.mu.Unlock()

	var gone []OutageReport
	for key, r := range s.known {
		if _, still := presentKeys[key]; !still {
			gone = append(gone, r)
			delete(s.known, key)
		}
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

