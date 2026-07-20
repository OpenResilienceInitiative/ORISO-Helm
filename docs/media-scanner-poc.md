# Media scanning PoC (WP-5, epic ORISO-Admin#366, ADR-014)

Fail-closed media scanning in front of Matrix media downloads. **Off by default**
(`mediaScanner.enabled: false`) — a proof of concept that must be enabled per
environment only after the images are pinned and the Mistral sub-processor is
signed off (see *Data protection* below).

## What it deploys

When `mediaScanner.enabled: true`, `templates/media-scanner/`:

- **matrix-content-scanner** (Element project) as a ClusterIP service on `:8080`,
  proxying media downloads — a client receives a file only after the scanner
  clears it.
- **ClamAV** as a sidecar (clamd on `localhost:3310`).
- A **scan script** (`scan.sh`) run once per file, chaining ClamAV then, for
  images, the Mistral vision check (`ai-check.sh`).

## Fail-closed contract

Every failure path blocks the file (`scan.sh` exits non-zero → scanner does not
release it; it just sits in the media repo, undownloadable):

- missing/empty file
- ClamAV reports infected **or** errors
- (AI on) Mistral unreachable, times out, returns non-2xx, or an answer that is
  not an explicit `SAFE` → block
- a file is only released on an explicit clean result

This mirrors the client blur state: *not yet scanned* → blurred, *clean* →
shown, *failed* → blocked tile (WP-4).

## The AI check is the swap point

`ai-check.sh` is the only place that talks to Mistral. Replacing it with a
self-hosted vision model later changes nothing else in the pipeline — the
decision recorded in the grilling session.

## Two independent switches

- `mediaScanner.enabled` (infra, here): is the scanner deployed and in the media
  path at all.
- `featureMediaAiScan*` (per-tenant/per-chat-type, TenantService): does the app
  ask the scanner for an AI verdict. ClamAV virus scanning rides on the
  inline-display switch, not on the AI switch.

## Data protection (KDG/AVV) — required before enabling AI

The Mistral API is an **external sub-processor** of chat images (potentially from
minors / intimate content). Before `aiCheck.enabled: true` in any real
environment:

1. Add Mistral SAS (La Plateforme, FR/EU) to the KDG/AVV sub-processor list.
2. Contract/technically ensure **zero retention** (the request sends
   `X-Zero-Retention: true`; confirm it is honoured for the account).
3. Provide the API key out-of-band (`aiCheck.existingSecret`), never inline in a
   committed values file.

Until then run virus-scanning only (`aiCheck.enabled: false`) or keep the whole
scanner off and rely on the WP-4 phase-1 behaviour (guest images blurred, the
counsellor reveals them).

## Enabling (example, per environment overlay)

```yaml
mediaScanner:
  enabled: true
  image: ghcr.io/matrix-org/matrix-content-scanner:<pinned-tag>
  aiCheck:
    enabled: true
    existingSecret: media-scanner-mistral   # key: mistral-api-key
```

## PoC caveats

- Validate `config.yaml` keys against the pinned matrix-content-scanner release
  before promoting — the upstream schema changes between versions.
- Wire Synapse / the clients to route media downloads through the scanner
  (`media-scanner:8080`); that ingress/config step is environment-specific and
  not part of this chart PoC.
- ClamAV needs ~1–2 GiB and a minute to load signatures (readiness reflects it).
