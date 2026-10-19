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

// TestAliasResolvesToPlace covers EOA Lemesos's 2026-10-21 Ariel announcement:
// "Αριέλ" is a Limassol neighbourhood with no OSM feature, so it is a manual
// alias (overrides.json) on Limassol, and the message keeps the raw name.
func TestAliasResolvesToPlace(t *testing.T) {
	r := testResolver(t)
	cases := []struct {
		town, sub, want string
		wantSub         bool
	}{
		{"Αριέλ", "", "Limassol - Αριέλ", false},
		{"Ariel", "", "Limassol - Ariel", false},
		// the display already carries the subdistrict, so the renderer must not
		// append it again
		{"Λεμεσός", "Αριέλ", "Limassol - Αριέλ", true},
	}
	for _, c := range cases {
		key, name, sub, ok := r.Resolve(outages.OutageReport{District: "Lemesos", TownVillage: c.town, AreaSubdistrict: c.sub})
		if !ok || key != "Q28870916" || name != c.want || sub != c.wantSub {
			t.Errorf("Resolve(%q, %q) = (%s, %q, sub=%v, ok=%v), want (Q28870916, %q)", c.town, c.sub, key, name, sub, ok, c.want)
		}
	}
}

// TestUnknownPlaceIsUnresolved: a place with no feature and no alias is a
// resolve failure (counted in metrics) even when the district is known --
// the fix is an alias in overrides.json, not a district fallback.
func TestUnknownPlaceIsUnresolved(t *testing.T) {
	r := testResolver(t)
	if _, _, _, ok := r.Resolve(outages.OutageReport{District: "Lemesos", TownVillage: "Ψψψψψ"}); ok {
		t.Errorf("Resolve of unknown place resolved, want unresolved")
	}
}

// TestAudienceReachesContainedAreas: a subscriber to a smaller area must hear
// about an outage announced for a broader area containing it (Potamos
// Germasogeias for all of Germasogeia), but not about a sibling's outage, and
// a district-level outage must not fan out to every area in the district.
func TestAudienceReachesContainedAreas(t *testing.T) {
	r := testResolver(t)
	audience := func(district, town string) map[string]bool {
		t.Helper()
		key, _, _, ok := r.Resolve(outages.OutageReport{District: district, TownVillage: town})
		if !ok {
			t.Fatalf("Resolve(%q) did not resolve", town)
		}
		set := map[string]bool{}
		for _, k := range r.Audience(key) {
			if p := r.byKey[k]; p != nil {
				set[displayName(p)] = true
			}
		}
		return set
	}
	cases := []struct {
		district, town string
		want, notWant  []string
	}{
		{"Lemesos", "Γερμασόγεια", []string{"Germasogeia Municipality", "Potamos Germasogeias", "Limassol District"}, []string{"Limassol", "Zakaki"}},
		// "Limassol" (city) and "Limassol Municipality" are twins; parishes
		// hang under either, and none of East Limassol Municipality may leak in
		{"Lemesos", "Λεμεσός", []string{"Limassol", "Zakaki", "Agia Zoni", "Limassol District"}, []string{"Germasogeia Municipality", "Potamos Germasogeias"}},
		{"Lemesos", "Ζακάκι", []string{"Zakaki", "Limassol", "Limassol District"}, []string{"Agia Zoni"}},
	}
	// a district-level outage must not fan out to every area in it
	for _, p := range r.places {
		if p.level == 5 && displayName(p) == "Limassol District" {
			for _, k := range r.Audience(p.key) {
				if q := r.byKey[k]; q != nil && (displayName(q) == "Limassol" || displayName(q) == "Zakaki") {
					t.Errorf("Audience(Limassol District) wrongly includes %q", displayName(q))
				}
			}
		}
	}
	for _, c := range cases {
		got := audience(c.district, c.town)
		for _, w := range c.want {
			if !got[w] {
				t.Errorf("Audience(%q) missing %q", c.town, w)
			}
		}
		for _, w := range c.notWant {
			if got[w] {
				t.Errorf("Audience(%q) wrongly includes %q", c.town, w)
			}
		}
	}
}
