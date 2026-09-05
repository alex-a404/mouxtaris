package outages

import (
	"strings"
	"time"
)

// timeLayouts are the formats EAC/EOA are observed to publish FromDateTime
// and ToDateTime in.
var timeLayouts = []string{time.RFC3339, "2006-01-02T15:04:05", "2006-01-02 15:04:05"}

// ParseTime best-effort parses an outage timestamp string, reporting whether
// it succeeded (blank or unrecognised input is not an error -- EAC leaves
// ToDateTime blank for an unresolved fault).
func ParseTime(s string) (time.Time, bool) {
	s = strings.TrimSpace(s)
	if s == "" {
		return time.Time{}, false
	}
	for _, layout := range timeLayouts {
		if t, err := time.Parse(layout, s); err == nil {
			return t, true
		}
	}
	return time.Time{}, false
}

type OutageType string

const (
	OutageTypeWater OutageType = "water"
	OutageTypePower OutageType = "power"
)

type OutageCause string

const (
	OutageCauseFault     OutageCause = "fault"
	OutageCauseScheduled OutageCause = "scheduled"
)

type OutageReport struct {
	OutageType       OutageType  `json:"outage_type"`
	OutageCause      OutageCause `json:"outage_cause"`
	FromDateTime     string      `json:"outage_from"`
	ToDateTime       string      `json:"outage_to"`
	District         string      `json:"district"`
	TownVillage      string      `json:"town_village"`
	AreaSubdistrict  string      `json:"area_subdistrict"`
	CustomPartOfArea string      `json:"part_of_area"`
}

func (o *OutageReport) StatusLabel() string {
	if o.OutageCause == OutageCauseFault {
		return "fault reported"
	}
	if o.OutageCause == OutageCauseScheduled {
		return "scheduled interruption"
	}
	return ""
}

func (o *OutageReport) RawLocation() string {
	parts := make([]string, 0, 3)
	for _, s := range []string{o.TownVillage, o.AreaSubdistrict, o.CustomPartOfArea} {
		if strings.TrimSpace(s) != "" {
			parts = append(parts, s)
		}
	}
	return strings.Join(parts, " / ")
}
