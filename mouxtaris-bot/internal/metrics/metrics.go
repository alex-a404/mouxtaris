package metrics

import (
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
)

var (
	IngestRequests = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "mouxtaris_ingest_requests_total",
		Help: "Ingest HTTP requests, by scraper source and outcome.",
	}, []string{"source", "status"}) // status: ok | unauthorized | bad_request

	IngestRows = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "mouxtaris_ingest_rows_total",
		Help: "Outage rows processed per ingest push, by scraper source and outcome.",
	}, []string{"source", "outcome"}) // outcome: received | created | updated | resolved

	LastSuccess = promauto.NewGaugeVec(prometheus.GaugeOpts{
		Name: "mouxtaris_ingest_last_success_unixtime",
		Help: "Unix timestamp of the last successful ingest push, by scraper source.",
	}, []string{"source"})

	ResolveFailures = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "mouxtaris_resolve_failures_total",
		Help: "Outage rows that could not be resolved to a known area, by scraper source.",
	}, []string{"source"})
)
