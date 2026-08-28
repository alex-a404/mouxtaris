package outages

import "strings"

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
