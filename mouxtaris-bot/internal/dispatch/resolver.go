package dispatch

import "mouxtaris.com/dispatcher/internal/outages"

type Resolver interface {
	Resolve(outage outages.OutageReport) (areaKey, displayName string, matchedSubdistrict, ok bool)
	Chain(areaKey string) []string
}
