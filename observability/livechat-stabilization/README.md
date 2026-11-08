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

## Relation to the provisioned dashboards

The native SigNoz `v0.135.1` dashboards and alerts that the chart provisions
live in `files/signoz/observability-catalog.json` (switch
`signoz.orisoObservability.enabled`, runbook
`runbooks/signoz-managed-observability.md`). The `ORISO Live Chat and Matrix`
dashboard there realises the live-chat and Matrix panels of
`dashboard-livechat-diagnostics.json`, and the catalog alerts cover the urgent
alerts in `alerts.yaml`. `tests/test_livechat_observability_contract.py` fails
when the two drift apart.

Not provisioned yet, because no emitter or native query exists for them:
`service.runtime.identity` (deployment drift), `oriso.admin.provisioning.operations`
(the catalog uses the UserService `userservice.provisioning.compensation.*`
metrics instead), the `deployment_identity_mismatch` and `web_vitals_stale`
alerts, and the management-quality panels, which carry targets and owners
rather than queries.
