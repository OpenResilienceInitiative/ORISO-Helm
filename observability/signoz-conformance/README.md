# SigNoz live conformance

This contract distinguishes three different truths:

1. **Rendered:** Helm contains the identity, retention, and gate configuration.
2. **Observed:** ClickHouse and the SigNoz API contain fresh, queryable evidence.
3. **Routed:** a human-triggered test alert reached the configured destination.

`scripts/signoz_conformance.py` is read-only unless `--snapshot-out` is
explicitly provided. It exits non-zero when evidence is
missing or stale and writes no telemetry, dashboards, alerts, or routes.

## Run against PreDev

Run from a machine whose current Kubernetes context can read the `caritas`
namespace:

```bash
python3 scripts/signoz_conformance.py \
  --contract observability/signoz-conformance/contract.yaml \
  --namespace caritas \
  --release oriso-platform \
  --ssh-host root@PREDEV_HOST \
  --signoz-url https://signoz.oriso-dev.site
```

Set `SIGNOZ_API_KEY` from a short-lived SigNoz service account to include
dashboard, alert-rule, and route-test readback. Never put the key in command
history, YAML, issue comments, or CI logs.

The report has one line per requirement and ends with `PASS` or `FAIL`.
Failures are release blockers, not warnings. In particular, an imported
dashboard with no live data is a failure even when its JSON exists.

## Deployment and route-test handoff

The chart's post-upgrade Job applies a three-day TTL to ClickHouse internal
diagnostic tables without forcing `MATERIALIZE TTL`; old parts age out through
normal merges. After merge and PreDev deployment:

1. run the live conformance command;
2. import/update dashboards and alerts through the authenticated SigNoz UI/API;
3. trigger one labelled test alert per route;
4. record the delivery timestamp in the SigNoz route-test evidence;
5. rerun conformance and attach the redacted output to the release evidence.

No PR or render-only check is evidence that routes delivered successfully.
