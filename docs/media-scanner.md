# Media scanner (fail-closed media downloads)

Part of epic ORISO-Admin#366 phase 2, issue #283, decision ADR-019.

A client only receives a media file after matrix-content-scanner has cleared it.
Everything below is **off by default**: an untouched chart deploys no scanner,
adds no route, and points no client anywhere. Turning it on is a deliberate,
per-environment act.

## What it deploys

With `mediaScanner.enabled: true`:

| Object | Purpose |
| --- | --- |
| `Deployment/media-scanner` | matrix-content-scanner plus a ClamAV sidecar |
| `Service/media-scanner` | ClusterIP `:8080` |
| `ConfigMap/media-scanner-config` | scanner config plus the scan scripts |
| `Ingress/media-scanner-ingress` | `/_matrix/media_proxy` → scanner |
| `Ingress/media-scanner-direct-media-block-ingress` | 403 on the unscanned route |

and `VITE_MEDIA_SCANNER_URL` in the frontend configmap, so clients know where
the scanner lives. While the scanner is off that value is the empty string.

## The fail-closed contract

The scan script exits non-zero for every outcome that is not a clean file, and
the scanner then simply does not release it — the file stays in the media
repository, undownloadable.

| Exit | Meaning | Cached? |
| --- | --- | --- |
| 0 | clean | yes |
| 1 | the file was rejected (infected, unsafe imagery) | yes |
| 2 | the check could not be performed (clamd down, timeout, garbled reply) | **no** |

Separating 1 from 2 matters: a signature-update outage or a restarting clamd
must not pin a perfectly clean file to "blocked" for the whole cache TTL. Both
still block right now — only the memory of the verdict differs.

Two further edges are closed on purpose:

- **No fallback backend.** If the scanner is down or scaled to zero, the edge
  answers 5xx. There is no rule that quietly sends the request to Synapse.
- **No unscanned route.** `blockDirectMediaAccess` (default `true`) answers 403
  on `/_matrix/media/{v3,r0,v1}/{download,thumbnail}` and
  `/_matrix/client/v1/media/{download,thumbnail}`. Uploads stay open — the
  scanner has to be able to fetch the file it is judging.

## Enabling it (ClamAV only)

1. Create the request secret out-of-band. It derives the key pair the scanner
   uses to decrypt Olm-encrypted POST bodies, i.e. the encrypted-media path
   where the client forwards its file keys (ORISO-Frontend#1072). Exactly 32
   random bytes, base64-encoded:

   ```sh
   kubectl -n <namespace> create secret generic media-scanner-request-secret \
     --from-literal=request-secret="$(head -c 32 /dev/urandom | base64)"
   ```

2. Point the chart at it and switch the scanner on:

   ```yaml
   mediaScanner:
     enabled: true
     requestSecret:
       existingSecret: media-scanner-request-secret
   ```

   The chart refuses to render without that reference rather than inventing a
   secret or accepting one from a values file.

3. Roll out:

   ```bash
   helm upgrade --install caritas ./ -n caritas --create-namespace \
     --wait-for-jobs --timeout 15m \
     -f values.yaml -f secrets.yaml
   ```

   Then verify:
   - `GET https://<domain>/_matrix/media_proxy/unstable/public_key` returns a key
   - an EICAR file uploaded in a chat is never retrievable through the client
     media path
   - with the scanner scaled to zero, media downloads fail — no silent
     direct-Synapse bypass
   - `GET https://<domain>/_matrix/media/v3/download/...` answers 403

Set `blockDirectMediaAccess: false` only while a client that does not yet speak
the scanner protocol is still in the field, and treat that as a temporary,
noted exception — it reopens the bypass.

## Enabling the AI image check

The vision check sends chat images — potentially from minors, potentially
intimate — to an external provider. It is therefore gated twice, and the chart
enforces both gates by refusing to render:

- `aiCheck.subProcessorAgreementSigned: true` — a signed zero-retention
  KDG/AVV sub-processor agreement is on record and the provider is listed in the
  sub-processor documentation (ORISO-Admin#734).
- `aiCheck.existingSecret` — the API key exists as a referenced secret under the
  key `api-key`. There is no inline key path.

```yaml
mediaScanner:
  aiCheck:
    enabled: true
    subProcessorAgreementSigned: true
    existingSecret: media-scanner-ai-key
```

On provider outage, timeout, non-2xx, or any answer that is not an explicit
`SAFE`, the file is quarantined. `ai-check.py` is the single integration point:
swapping it for a self-hosted model changes nothing else in the pipeline.

## Image pins

Both images are pinned by digest, because a rebuilt `latest` in the media path
would change what judges user files without any review.

| Image | Tag equivalent |
| --- | --- |
| `docker.io/vectorim/matrix-content-scanner` | `v1.3.0` |
| `docker.io/clamav/clamav` | `1.5.4` |

When bumping the scanner pin, re-validate `config.yaml` against that release's
`config.sample.yaml` — the upstream schema changes between releases (`server`
became `web`, the Olm pickle settings became `crypto.request_secret_path`).

## Resources

ClamAV needs roughly 1–2 GiB and about a minute to load its signature database.
Until clamd answers, the pod stays out of the service, so downloads fail at the
edge rather than bypassing the scan.

## Guard

`tests/render_media_scanner_test.py` renders the chart and asserts the four
promises above: off by default, pinned by digest, secrets by reference only, and
fail-closed routing including the AI gate. Run it with `python3` from the chart
root; it needs `helm` and PyYAML.
