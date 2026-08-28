package resolve

import (
	"encoding/json"
	"log"
	"os"
	"sort"
	"strings"
	"unicode"

	"golang.org/x/text/runes"
	"golang.org/x/text/transform"
	"golang.org/x/text/unicode/norm"

	"mouxtaris.com/dispatcher/internal/outages"
)

// ---------------------------------------------------------------------------
// Public surface: implements dispatch.Resolver.
//   Resolve(outage) -> (areaKey, displayName, ok)
//   Chain(areaKey)  -> [areaKey, parentKey, ...] coarsening upward
//
// Parents are computed from GEOMETRY at construction (point-in-polygon), exactly
// like the Python _build_hierarchy. This makes co-reference (same place at two
// admin levels) collapse correctly AND makes Chain() climb to the district, so
// district-level subscriptions match town-level outages.
// ---------------------------------------------------------------------------

type Resolver struct {
	places    []*place
	byKey     map[string]*place
	minScore  float64
	minMargin float64
}

func New(areas []*Area) *Resolver {
	r := &Resolver{
		byKey:     make(map[string]*place, len(areas)),
		minScore:  0.72,
		minMargin: 0.04,
	}
	for _, a := range areas {
		p := &place{
			key:      a.Key,
			nameEL:   a.NameEL,
			nameEN:   a.NameEN,
			level:    a.Level,
			parent:   a.ParentKey, // may be pre-set by overrides; else computed below
			variants: buildVariants(a),
			rings:    a.rings,
		}
		p.cx, p.cy, p.hasCentroid = centroid(a.rings)
		p.minx, p.miny, p.maxx, p.maxy, p.hasBBox = bbox(a.rings)
		r.places = append(r.places, p)
		r.byKey[a.Key] = p
	}
	r.buildHierarchy()
	return r
}

// buildHierarchy assigns each place's parent = the smallest higher-level polygon
// whose ring contains the place's centroid. Overrides that already set a parent
// are respected.
func (r *Resolver) buildHierarchy() {
	polys := make([]*place, 0, len(r.places))
	for _, p := range r.places {
		if len(p.rings) > 0 && p.hasBBox {
			polys = append(polys, p)
		}
	}
	for _, p := range r.places {
		if p.parent != "" || !p.hasCentroid {
			continue // already set (override), or nothing to place
		}
		var best *place
		bestArea := 0.0
		for _, q := range polys {
			if q == p || q.level >= p.level {
				continue
			}
			if p.cx < q.minx || p.cx > q.maxx || p.cy < q.miny || p.cy > q.maxy {
				continue
			}
			if !q.containsPoint(p.cx, p.cy) {
				continue
			}
			a := (q.maxx - q.minx) * (q.maxy - q.miny)
			if best == nil || a < bestArea {
				best, bestArea = q, a
			}
		}
		if best != nil {
			p.parent = best.key
		}
	}
}

// Resolve returns the matched area's key and display name, plus whether the
// match came from o.AreaSubdistrict — callers use that to avoid re-appending
// the subdistrict when it's already what the display name represents.
func (r *Resolver) Resolve(o outages.OutageReport) (key, name string, matchedSubdistrict, ok bool) {
	district := o.District
	// CustomPartOfArea ("part_of_area") is free-text filler from EAC (e.g. "South
	// East side", "Part of the area"), not a place name — never use it to resolve.
	fields := []struct {
		text          string
		isSubdistrict bool
	}{
		{o.AreaSubdistrict, true},
		{o.TownVillage, false},
	}
	for _, f := range fields {
		text := strings.TrimSpace(f.text)
		if text == "" {
			continue
		}
		if p, ok := r.best(text, district); ok {
			return p.key, displayName(p), f.isSubdistrict, true
		}
		// Retry without a directional qualifier (Πάνω/Κάτω/Νέο/Παλαιό) the
		// source appended but that has no dedicated sub-area in our data --
		// e.g. "Stroumpi Kato" has no separate "Kato Stroumpi" feature, only
		// "Stroumpi" itself, so the extra word drags the score under
		// threshold. Falling back to the whole settlement beats dropping
		// the notification entirely. Only tried after the full text fails,
		// so a place that DOES have a dedicated Kato/Pano feature already
		// matched above and never reaches this fallback.
		if stripped, ok := stripQualifier(text); ok {
			if p, ok := r.best(stripped, district); ok {
				return p.key, displayName(p), f.isSubdistrict, true
			}
		}
	}
	return "", "", false, false
}

// stripQualifier removes a leading or trailing directional qualifier word
// (matched against the qualifiers table, e.g. "Kato", "Pano", "Neo", "Old")
// from text, reporting whether one was found.
func stripQualifier(text string) (string, bool) {
	words := strings.Fields(text)
	if len(words) < 2 {
		return "", false
	}
	if _, ok := qualifiers[fold(words[len(words)-1])]; ok {
		return strings.Join(words[:len(words)-1], " "), true
	}
	if _, ok := qualifiers[fold(words[0])]; ok {
		return strings.Join(words[1:], " "), true
	}
	return "", false
}

func (r *Resolver) Chain(areaKey string) []string {
	var out []string
	seen := map[string]bool{}
	for k := areaKey; k != ""; {
		if seen[k] {
			break
		}
		seen[k] = true
		out = append(out, k)
		p, ok := r.byKey[k]
		if !ok {
			break
		}
		k = p.parent
	}
	return out
}

// ---------------------------------------------------------------------------

type place struct {
	key      string
	nameEL   string
	nameEN   string
	level    int
	parent   string
	variants [][]string

	rings                  [][][2]float64
	cx, cy                 float64
	hasCentroid            bool
	minx, miny, maxx, maxy float64
	hasBBox                bool
}

func (p *place) containsPoint(x, y float64) bool {
	for _, ring := range p.rings {
		if pointInRing(x, y, ring) {
			return true
		}
	}
	return false
}

func displayName(p *place) string {
	if strings.TrimSpace(p.nameEN) != "" {
		return p.nameEN
	}
	return p.nameEL
}

const tokenHit = 0.80

func (r *Resolver) best(text, district string) (*place, bool) {
	q := tokenise(text)
	if len(q) == 0 {
		return nil, false
	}
	// If the query is Greek, also prepare a transliterated form so it can match
	// features that only have a Latin name (missing name:el).
	var qLat []string
	if hasGreek(text) {
		qLat = tokenise(greekToLatin(text))
	}
	dkey := foldDistrict(district)

	type scored struct {
		s float64
		p *place
	}
	var cands []scored
	for _, p := range r.places {
		s := 0.0
		for _, v := range p.variants {
			if sc := score(q, v); sc > s {
				s = sc
			}
			if len(qLat) > 0 {
				if sc := score(qLat, v); sc > s {
					s = sc
				}
			}
		}
		if s <= 0 {
			continue
		}
		if dkey != "" {
			if pd := r.districtOf(p); pd != "" && pd != dkey {
				continue
			}
		}
		cands = append(cands, scored{s, p})
	}
	if len(cands) == 0 && dkey != "" {
		for _, p := range r.places {
			s := 0.0
			for _, v := range p.variants {
				if sc := score(q, v); sc > s {
					s = sc
				}
				if len(qLat) > 0 {
					if sc := score(qLat, v); sc > s {
						s = sc
					}
				}
			}
			if s > 0 {
				cands = append(cands, scored{s * 0.9, p})
			}
		}
	}
	if len(cands) == 0 {
		return nil, false
	}

	sort.Slice(cands, func(i, j int) bool { return cands[i].s > cands[j].s })

	// DEBUG: show the top candidate and runner-up score for each query
	if len(cands) > 0 {
		second := 0.0
		if len(cands) > 1 {
			second = cands[1].s
		}
		log.Printf("resolve %q: top=%.3f %q  2nd=%.3f", text, cands[0].s, cands[0].p.nameEL, second)
	}

	top := cands[0]
	if top.s < r.minScore {
		return nil, false
	}

	// collapse co-referent ties (same place at different admin levels); only a
	// genuinely distinct runner-up within the margin makes it ambiguous.
	if len(cands) > 1 {
		second := cands[1]
		if top.s-second.s < r.minMargin && !r.coreferent(top.p, second.p) {
			return nil, false
		}
	}
	// if the winner is co-referent with a finer/coarser twin, prefer the finest
	// so Chain() has the longest climb.
	winner := top.p
	for _, c := range cands {
		if top.s-c.s < r.minMargin && r.coreferent(top.p, c.p) && c.p.level > winner.level {
			winner = c.p
		}
	}
	return winner, true
}

func score(query, cand []string) float64 {
	if len(query) == 0 || len(cand) == 0 {
		return 0
	}
	qCov := 0.0
	for _, q := range query {
		best := 0.0
		for _, c := range cand {
			if s := tokSim(q, c); s > best {
				best = s
			}
		}
		if best >= tokenHit {
			qCov += best
		}
	}
	qCov /= float64(len(query))

	cCov := 0.0
	for _, c := range cand {
		best := 0.0
		for _, q := range query {
			if s := tokSim(c, q); s > best {
				best = s
			}
		}
		if best >= tokenHit {
			cCov += best
		}
	}
	cCov /= float64(len(cand))

	return 0.75*qCov + 0.25*cCov
}

func tokSim(a, b string) float64 {
	if a == b {
		return 1.0
	}
	ra, rb := []rune(a), []rune(b)
	n := len(ra)
	if len(rb) < n {
		n = len(rb)
	}
	common := 0
	for common < n && ra[common] == rb[common] {
		common++
	}
	ratio := levRatio(ra, rb)
	// Only trust a shared prefix as a near-match when the words are close in
	// overall length (typo / grammatical-ending variance, e.g. Greek case
	// endings). A short word that happens to be a complete prefix of a much
	// longer, different word (e.g. "Foini" vs "Finikaria") must NOT be boosted,
	// or unrelated places sharing a name root get falsely tied/coreferent.
	lenDiff := len(ra) - len(rb)
	if lenDiff < 0 {
		lenDiff = -lenDiff
	}
	if common >= 5 && lenDiff <= 2 {
		ratio = maxf(ratio, 0.90)
	} else if common >= 4 && n <= 7 && lenDiff <= 2 {
		ratio = maxf(ratio, 0.85)
	}
	return ratio
}

func levRatio(a, b []rune) float64 {
	la, lb := len(a), len(b)
	if la == 0 && lb == 0 {
		return 1
	}
	d := levenshtein(a, b)
	maxLen := la
	if lb > maxLen {
		maxLen = lb
	}
	return 1 - float64(d)/float64(maxLen)
}

func levenshtein(a, b []rune) int {
	prev := make([]int, len(b)+1)
	for j := range prev {
		prev[j] = j
	}
	for i := 1; i <= len(a); i++ {
		cur := make([]int, len(b)+1)
		cur[0] = i
		for j := 1; j <= len(b); j++ {
			cost := 1
			if a[i-1] == b[j-1] {
				cost = 0
			}
			cur[j] = min3(prev[j]+1, cur[j-1]+1, prev[j-1]+cost)
		}
		prev = cur
	}
	return prev[len(b)]
}

// ---------------------------------------------------------------------------

const corefSim = 0.85

// coreferent: two features are the same real place if their names essentially
// agree (admin nouns already dropped in tokenise), OR one is nested in the other
// with reasonably agreeing names. The first clause makes it work even when
// parent links are absent, matching Python's intent.
func (r *Resolver) coreferent(a, b *place) bool {
	best := 0.0
	for _, va := range a.variants {
		for _, vb := range b.variants {
			if s := score(va, vb); s > best {
				best = s
			}
		}
	}
	if best >= corefSim {
		return true
	}
	return r.nested(a, b) && best >= 0.6
}

func (r *Resolver) nested(a, b *place) bool {
	for k := a.parent; k != ""; {
		if k == b.key {
			return true
		}
		p, ok := r.byKey[k]
		if !ok {
			break
		}
		k = p.parent
	}
	for k := b.parent; k != ""; {
		if k == a.key {
			return true
		}
		p, ok := r.byKey[k]
		if !ok {
			break
		}
		k = p.parent
	}
	return false
}

// SearchResult is a candidate area returned by Search.
type SearchResult struct {
	Key      string
	NameEN   string
	Level    int
	District string // English display name of the parent district, may be empty
}

// Search returns up to limit areas whose name best matches text, ordered by score.
// The threshold is intentionally lower than Resolve's to surface useful candidates
// for a user to pick from.
func (r *Resolver) Search(text string, limit int) []SearchResult {
	q := tokenise(text)
	if len(q) == 0 {
		return nil
	}
	var qLat []string
	if hasGreek(text) {
		qLat = tokenise(greekToLatin(text))
	}

	type scored struct {
		s float64
		p *place
	}
	var cands []scored
	for _, p := range r.places {
		s := 0.0
		for _, v := range p.variants {
			if sc := score(q, v); sc > s {
				s = sc
			}
			if len(qLat) > 0 {
				if sc := score(qLat, v); sc > s {
					s = sc
				}
			}
		}
		if s >= 0.40 {
			cands = append(cands, scored{s, p})
		}
	}
	sort.Slice(cands, func(i, j int) bool { return cands[i].s > cands[j].s })

	var results []SearchResult
	for _, c := range cands {
		if len(results) >= limit {
			break
		}
		results = append(results, SearchResult{
			Key:      c.p.key,
			NameEN:   displayName(c.p),
			Level:    c.p.level,
			District: r.districtDisplayOf(c.p),
		})
	}
	return results
}

func (r *Resolver) districtDisplayOf(p *place) string {
	seen := map[string]bool{}
	for k := p.key; k != ""; {
		if seen[k] {
			break
		}
		seen[k] = true
		cur, ok := r.byKey[k]
		if !ok {
			break
		}
		if cur.level == 5 {
			return displayName(cur)
		}
		k = cur.parent
	}
	return ""
}

func (r *Resolver) districtOf(p *place) string {
	seen := map[string]bool{}
	for k := p.key; k != ""; {
		if seen[k] {
			break
		}
		seen[k] = true
		cur, ok := r.byKey[k]
		if !ok {
			break
		}
		if cur.level == 5 {
			return foldDistrict(cur.nameEL + " " + cur.nameEN)
		}
		k = cur.parent
	}
	return ""
}

// ---------------------------------------------------------------------------
// Geometry
// ---------------------------------------------------------------------------

func centroid(rings [][][2]float64) (float64, float64, bool) {
	var sx, sy float64
	n := 0
	for _, r := range rings {
		for _, pt := range r {
			sx += pt[0]
			sy += pt[1]
			n++
		}
	}
	if n == 0 {
		return 0, 0, false
	}
	return sx / float64(n), sy / float64(n), true
}

func bbox(rings [][][2]float64) (float64, float64, float64, float64, bool) {
	first := true
	var minx, miny, maxx, maxy float64
	for _, r := range rings {
		for _, pt := range r {
			if first {
				minx, miny, maxx, maxy = pt[0], pt[1], pt[0], pt[1]
				first = false
				continue
			}
			if pt[0] < minx {
				minx = pt[0]
			}
			if pt[0] > maxx {
				maxx = pt[0]
			}
			if pt[1] < miny {
				miny = pt[1]
			}
			if pt[1] > maxy {
				maxy = pt[1]
			}
		}
	}
	return minx, miny, maxx, maxy, !first
}

func pointInRing(x, y float64, ring [][2]float64) bool {
	inside := false
	n := len(ring)
	j := n - 1
	for i := 0; i < n; i++ {
		xi, yi := ring[i][0], ring[i][1]
		xj, yj := ring[j][0], ring[j][1]
		if (yi > y) != (yj > y) {
			denom := yj - yi
			if denom == 0 {
				denom = 1e-15
			}
			if x < (xj-xi)*(y-yi)/denom+xi {
				inside = !inside
			}
		}
		j = i
	}
	return inside
}

// ---------------------------------------------------------------------------
// Normalisation
// ---------------------------------------------------------------------------

var generic = map[string]bool{
	"δημος": true, "δημοσ": true, "κοινοτητα": true, "ενορια": true,
	"επαρχια": true, "περιοχη": true, "συμβουλιο": true,
	"τουριστικη": true, "τουριστικο": true, "τουριστικοσ": true,
	"municipality": true, "community": true, "district": true, "quarter": true,
	"area": true, "council": true, "and": true, "of": true, "the": true, "και": true,
	"tourist": true, "touristic": true,
}

// genericLatin is the Latin transliteration of every Greek entry in generic
// (e.g. "περιοχη" -> "periochi"), so a generic administrative/descriptive word
// is filtered out even when the source text spells it in transliterated Greek
// rather than Greek script or English -- EAC/EOA feeds mix all three freely.
var genericLatin = buildGenericLatin()

func buildGenericLatin() map[string]bool {
	out := make(map[string]bool, len(generic))
	for g := range generic {
		if hasGreek(g) {
			out[greekToLatin(g)] = true
		}
	}
	return out
}

var qualifiers = map[string]string{
	"ano": "πανω", "pano": "πανω", "upper": "πανω", "πανω": "πανω",
	"kato": "κατω", "lower": "κατω", "κατω": "κατω",
	"neo": "νεο", "nea": "νεο", "new": "νεο", "νεο": "νεο", "νεα": "νεο",
	"paleo": "παλαιο", "old": "παλαιο", "παλαιο": "παλαιο", "παλιο": "παλαιο",
	// EAC/EOA spell the district "Pafos" (matching the Greek transliteration
	// produced by greekToLatin); OSM's name:en is "Paphos". Their tokSim
	// (0.667) falls just short of tokenHit (0.80), so without this every
	// "Pafos"-qualified query (e.g. "Pafos Kato") ties with every unrelated
	// "Kato *" village instead of preferring the actual Paphos-named place.
	"pafos": "paphos",
}

var deaccent = transform.Chain(norm.NFD, runes.Remove(runes.In(unicode.Mn)), norm.NFC)

func fold(s string) string {
	s = strings.ToLower(strings.TrimSpace(s))
	if out, _, err := transform.String(deaccent, s); err == nil {
		s = out
	}
	s = strings.ReplaceAll(s, "ς", "σ")
	var b strings.Builder
	for _, r := range s {
		if unicode.IsLetter(r) || unicode.IsDigit(r) || unicode.IsSpace(r) {
			b.WriteRune(r)
		} else {
			b.WriteRune(' ')
		}
	}
	return strings.Join(strings.Fields(b.String()), " ")
}

// greekToLatin transliterates Greek to a Latin form so a Greek query can match
// a feature that only has an English/Latin name (some OSM towns lack name:el).
// Handles the common digraphs first, then single letters.
// μπ is deliberately absent here: EAC/EOA and the OSM data both use the
// literal ELOT-style rendering (μ->m, π->p, e.g. "Λέμπας"->"Lempa",
// "Αλάμπρα"->"Alampra"), not the phonetic "b"/"mb" digraph. Letting μ and π
// fall through individually below gives the correct "mp".
var grDigraphs = []struct{ gr, la string }{
	{"ου", "ou"}, {"αι", "ai"}, {"ει", "ei"}, {"οι", "oi"}, {"αυ", "av"},
	{"ευ", "ev"}, {"ντ", "nt"}, {"γκ", "gk"}, {"τσ", "ts"},
	{"τζ", "tz"}, {"θ", "th"}, {"χ", "ch"}, {"ψ", "ps"},
}

var grLetters = map[rune]string{
	'α': "a", 'β': "v", 'γ': "g", 'δ': "d", 'ε': "e", 'ζ': "z", 'η': "i",
	'θ': "th", 'ι': "i", 'κ': "k", 'λ': "l", 'μ': "m", 'ν': "n", 'ξ': "x",
	'ο': "o", 'π': "p", 'ρ': "r", 'σ': "s", 'τ': "t", 'υ': "y", 'φ': "f",
	'χ': "ch", 'ψ': "ps", 'ω': "o",
}

func greekToLatin(s string) string {
	s = strings.ToLower(s)
	if out, _, err := transform.String(deaccent, s); err == nil {
		s = out
	}
	s = strings.ReplaceAll(s, "ς", "σ")
	for _, d := range grDigraphs {
		s = strings.ReplaceAll(s, d.gr, d.la)
	}
	var b strings.Builder
	for _, r := range s {
		if l, ok := grLetters[r]; ok {
			b.WriteString(l)
		} else {
			b.WriteRune(r)
		}
	}
	return b.String()
}

func hasGreek(s string) bool {
	for _, r := range s {
		if r >= 0x0370 && r <= 0x03ff {
			return true
		}
	}
	return false
}

func tokenise(s string) []string {
	s = fold(s)
	var out []string
	for _, t := range strings.Fields(s) {
		if q, ok := qualifiers[t]; ok {
			t = q
		}
		if generic[t] || genericLatin[t] || len([]rune(t)) < 2 {
			continue
		}
		out = append(out, t)
	}
	return out
}

func foldDistrict(s string) string {
	f := fold(s)
	switch {
	case strings.Contains(f, "λευκωσια"), strings.Contains(f, "nicosia"), strings.Contains(f, "lefkosia"):
		return "lefkosia"
	case strings.Contains(f, "λεμεσο"), strings.Contains(f, "limassol"), strings.Contains(f, "lemeso"):
		return "lemesos"
	case strings.Contains(f, "λαρνακα"), strings.Contains(f, "larnaca"), strings.Contains(f, "larnaka"):
		return "larnaka"
	case strings.Contains(f, "παφο"), strings.Contains(f, "paphos"), strings.Contains(f, "pafos"):
		return "pafos"
	case strings.Contains(f, "αμμοχωστο"), strings.Contains(f, "famagusta"), strings.Contains(f, "ammochostos"):
		return "ammochostos"
	}
	return ""
}

func buildVariants(a *Area) [][]string {
	var vs [][]string
	seen := map[string]bool{}
	for _, name := range []string{a.NameEL, a.NameEN} {
		for _, part := range strings.Split(name, ";") {
			toks := tokenise(part)
			if len(toks) == 0 {
				continue
			}
			joined := strings.Join(toks, " ")
			if !seen[joined] {
				seen[joined] = true
				vs = append(vs, toks)
			}
		}
	}
	return vs
}

// ---------------------------------------------------------------------------
// GeoJSON loading (+ overrides merge), now including geometry rings
// ---------------------------------------------------------------------------

type Area struct {
	Key       string
	NameEL    string
	NameEN    string
	Level     int
	ParentKey string
	rings     [][][2]float64
}

func LoadGeoJSON(geojsonPath, overridesPath string) ([]*Area, error) {
	raw, err := os.ReadFile(geojsonPath)
	if err != nil {
		return nil, err
	}
	var fc struct {
		Features []struct {
			Properties map[string]any  `json:"properties"`
			Geometry   json.RawMessage `json:"geometry"`
		} `json:"features"`
	}
	if err := json.Unmarshal(raw, &fc); err != nil {
		return nil, err
	}

	overrides := map[string]struct {
		NameEN    *string `json:"name_en"`
		ParentKey *string `json:"parent_key"`
	}{}
	if overridesPath != "" {
		if ob, err := os.ReadFile(overridesPath); err == nil {
			_ = json.Unmarshal(ob, &overrides)
		}
	}

	var areas []*Area
	for i, f := range fc.Features {
		p := f.Properties
		key := featureKey(p, i)
		a := &Area{
			Key:    key,
			NameEL: str(p, "name", "name:el"),
			NameEN: str(p, "name:en", "int_name", "name"),
			Level:  levelOf(p),
			rings:  parseRings(f.Geometry),
		}
		if ov, ok := overrides[key]; ok {
			if ov.NameEN != nil {
				a.NameEN = *ov.NameEN
			}
			if ov.ParentKey != nil {
				a.ParentKey = *ov.ParentKey
			}
		}
		if a.NameEL == "" && a.NameEN == "" {
			continue
		}
		areas = append(areas, a)
	}
	return areas, nil
}

// parseRings extracts outer rings from Polygon / MultiPolygon geometry.
func parseRings(raw json.RawMessage) [][][2]float64 {
	if len(raw) == 0 {
		return nil
	}
	var g struct {
		Type        string          `json:"type"`
		Coordinates json.RawMessage `json:"coordinates"`
	}
	if json.Unmarshal(raw, &g) != nil {
		return nil
	}
	switch g.Type {
	case "Polygon":
		var poly [][][2]float64
		if json.Unmarshal(g.Coordinates, &poly) != nil || len(poly) == 0 {
			return nil
		}
		return [][][2]float64{poly[0]} // outer ring
	case "MultiPolygon":
		var mp [][][][2]float64
		if json.Unmarshal(g.Coordinates, &mp) != nil {
			return nil
		}
		var out [][][2]float64
		for _, poly := range mp {
			if len(poly) > 0 {
				out = append(out, poly[0])
			}
		}
		return out
	}
	return nil
}

func featureKey(p map[string]any, idx int) string {
	for _, k := range []string{"@id", "wikidata", "name"} {
		if v, ok := p[k].(string); ok && v != "" {
			return v
		}
	}
	return "idx_" + itoa(idx)
}

func levelOf(p map[string]any) int {
	switch v := p["admin_level"].(type) {
	case string:
		return atoiSafe(v)
	case float64:
		return int(v)
	}
	return 99
}

func str(p map[string]any, keys ...string) string {
	for _, k := range keys {
		if v, ok := p[k].(string); ok && strings.TrimSpace(v) != "" {
			return v
		}
	}
	return ""
}

func min3(a, b, c int) int {
	m := a
	if b < m {
		m = b
	}
	if c < m {
		m = c
	}
	return m
}
func maxf(a, b float64) float64 {
	if a > b {
		return a
	}
	return b
}
func atoiSafe(s string) int {
	n := 0
	for _, r := range strings.TrimSpace(s) {
		if r < '0' || r > '9' {
			return 99
		}
		n = n*10 + int(r-'0')
	}
	return n
}
func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b []byte
	for n > 0 {
		b = append([]byte{byte('0' + n%10)}, b...)
		n /= 10
	}
	return string(b)
}
