package resolve

import (
	"strings"
	"testing"

	"mouxtaris.com/dispatcher/internal/outages"
)

func testResolver(t *testing.T) *Resolver {
	t.Helper()
	areas, err := LoadGeoJSON("../../cyprus_admin.geojson", "../../overrides.json")
	if err != nil {
		t.Fatalf("LoadGeoJSON: %v", err)
	}
	return New(areas)
}

// TestResolveLatinAgainstGreekOnlyToken covers names whose name:en drops a word
// the Greek name carries, so EAC's English feed spells a word that exists only
// in the Greek. "Κοινότητα Πύργου Λεμεσού" has name:en "Pyrgos Community", so
// "Lemesou" had nothing to score against and the whole row failed to resolve.
func TestResolveLatinAgainstGreekOnlyToken(t *testing.T) {
	r := testResolver(t)
	cases := []struct {
		district, town, want string
	}{
		{"Lemesos", "Pyrgos Lemesou", "Pyrgos Community"},
		{"Lemesos", "Πύργος Λεμεσού", "Pyrgos Community"},
		{"Lemesos", "Pyrgos", "Pyrgos Community"},
	}
	for _, c := range cases {
		_, name, _, ok := r.Resolve(outages.OutageReport{District: c.district, TownVillage: c.town})
		if !ok {
			t.Errorf("Resolve(%q, %q): did not resolve, want %q", c.district, c.town, c.want)
			continue
		}
		if name != c.want {
			t.Errorf("Resolve(%q, %q) = %q, want %q", c.district, c.town, c.town, c.want)
		}
	}
}

// TestEveryNameResolvesToItself guards the whole gazetteer against the class of
// bug above: each place must resolve from its Greek name, its English name, and
// a transliterated form of its Greek name (what the EAC English feed prints).
// A match on a co-referent twin (same real place at another admin level) counts,
// since Chain() still climbs through the loser.
func TestEveryNameResolvesToItself(t *testing.T) {
	r := testResolver(t)
	for _, p := range r.places {
		district := r.districtOf(p)
		for _, probe := range []string{
			PrimaryName(p.nameEL),
			PrimaryName(p.nameEN),
			greekToLatin(PrimaryName(p.nameEL)),
		} {
			if strings.TrimSpace(probe) == "" {
				continue
			}
			key, _, _, ok := r.Resolve(outages.OutageReport{District: district, TownVillage: probe})
			if !ok {
				t.Errorf("%s: %q did not resolve", p.key, probe)
				continue
			}
			if key != p.key && !r.coreferent(p, r.byKey[key]) {
				t.Errorf("%s: %q resolved to unrelated %s", p.key, probe, key)
			}
		}
	}
}
