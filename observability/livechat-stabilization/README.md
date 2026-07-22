# Live Chat observability contract

This folder is the privacy and cardinality contract for the stabilization flow:

- `telemetry-catalog.yaml` owns signal names and permitted dimensions;
- `dashboard-livechat-diagnostics.json` owns panel intent and diagnostic causes;
- `dashboard-management-quality.json` owns management questions and measurable
  before-and-after quality targets;
- `service-state-map.yaml` separates Kubernetes workload kind from verified
  statelessness and names the signals needed to change that classification;
- `baseline-2026-07-22.yaml` records the first evidence snapshot and its limits;
- `TRACKING-EVENT-DECISIONS.md` preserves the short periodic signal decisions,
  including rejected tracking ideas and their privacy/cardinality rationale;
- `alerts.yaml` owns alert thresholds and grouping boundaries;
- `RUNBOOK.md` maps symptoms to queries and immutable runtime proof.

The dashboard file is deliberately a build specification rather than guessed
SigNoz export JSON. The native export must be captured from the exact deployed
self-hosted SigNoz version after the emitter PRs land, then validated against
this contract. This prevents a syntactically plausible but non-importable JSON
file from masquerading as an operational dashboard.
