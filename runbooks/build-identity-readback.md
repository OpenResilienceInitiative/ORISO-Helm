# Read back the Admin and App build identity (G121)

An image swap can change the software while the release label stays unchanged.
Both [Admin](../templates/admin/admin-configmap.yaml) and
[App](../templates/frontend/frontend-configmap.yaml) render their runtime release
variables from `.Chart.AppVersion`. `kubectl set image` changes the pod template's
image reference; it does not render Helm templates or update those ConfigMaps.
The container entrypoint can regenerate `env.js`, but it still reads the old
configuration. Restarting the pod alone does not correct that release label.

[Helm 330](https://github.com/OpenResilienceInitiative/ORISO-Helm/issues/330) retains
an **open operator gate**: choose the intended release and deploy the approved
chart/configuration with the existing Helm-upgrade procedure. Confirm which chart
and values the environment actually uses before upgrading. `helm upgrade` renders
the chart again; it is the alternative when the release/configuration must change.
Both deployments consume these ConfigMaps through `envFrom`, so existing containers
also need to be replaced through the approved rollout procedure to consume updated
environment values. A successful Helm command alone is not browser acceptance.

The parent's planned **v2.0.6 is not an installed-target assertion**. This runbook
does not select a release, bump the chart, prescribe new swap commands, or claim a
deployment. [Helm 309](https://github.com/OpenResilienceInitiative/ORISO-Helm/issues/309)
owns swap/restore commands; [Helm 183](https://github.com/OpenResilienceInitiative/ORISO-Helm/issues/183)
owns the mutable-tag race. G121 detects release/build mismatches; it does not fix
those mechanisms or certify every configuration key.

## What the footer proves

Admin and App bake their own full checkout commit into the JavaScript bundle in
`.github/actions/node-build/action.yml`, using `git rev-parse --verify HEAD^{commit}`.
The actual checkout can differ from `github.sha`, particularly with an explicit
checkout ref or a PR merge checkout. Each footer shows the runtime release followed
by seven commit characters. `data-build-commit` holds the full commit and
`data-platform-version` holds the release. Runtime `env.js` cannot override the
build-only accessor.

An ordinary local build without a valid full commit remains unidentified: a
release with `unknown` has an empty machine-readable commit. A missing footer,
missing release, short-only commit, or unknown identity cannot pass G121.

Use the existing image SHA tags, immutable digests, OCI revision labels, and CI
provenance to trace each image to its source checkout. No new tag system is needed.
The browser gate compares against that independently recorded mapping; it does
not inspect a registry or prove that an operator's provenance record is truthful.

## Prepare independent expectations

In the ORISO-E2E checkout containing
[E2E 76](https://github.com/OpenResilienceInitiative/ORISO-E2E/issues/76), use Node 22
and the committed lockfile. Before inspecting the page, record:

1. The operator-approved expected release, from the release decision/approved
   deployment record. Never copy it from the footer or `env.js` under test.
2. The expected **Admin** and **Frontend** full source commits, separately, from
   the respective image-build jobs/OCI revision/provenance records.
3. Each expected immutable image reference (`registry/image@sha256:<64 hex>`),
   tied to that commit. Separately confirm that the environment runs those image
   digests; a mutable tag or an image's name alone is insufficient.
4. The intended environment and canonical App/Admin base URLs. `dev` requires
   `https://dev.oriso.org/` and `/admin`; `predev` requires
   `https://predev.oriso.org/` and `/admin`. `local` only accepts loopback targets.

Store only this non-secret record in `runtime-version-evidence.json`. The following
is a **schema example with deliberately invalid placeholders**, not runnable proof:

```json
{
  "environment": "dev",
  "commits": {
    "admin": "<full Admin source commit from image provenance>",
    "frontend": "<full Frontend source commit from image provenance>"
  },
  "images": {
    "admin": "<Admin registry/image@sha256:64-hex-digest>",
    "frontend": "<Frontend registry/image@sha256:64-hex-digest>"
  },
  "effectiveConfig": {
    "platformVersion": "<operator-selected release, for example vX.Y.Z>",
    "appUrl": "https://dev.oriso.org/",
    "adminUrl": "https://dev.oriso.org/admin"
  }
}
```

Do not fill expectations by reading the application and then call that a passing
deployment check. Keep the source CI/job and release-decision links with the run's
operator evidence. Image identity and release identity are separate inputs.

## Run the committed guard

Run these commands from that ORISO-E2E checkout. They use
`playwright.runtime-version.config.ts` and `tests/runtime-version-readback.spec.ts`.
They do not provision users, sign in, load application credentials, mutate the
cluster, or depend on the legacy PreDev IP/TLS configuration.

For Dev, after filling the independent evidence file:

```bash
export RUNTIME_VERSION_EVIDENCE_FILE=runtime-version-evidence.json
export ORISO_E2E_RUNTIME_EVIDENCE_JSON="$(cat "$RUNTIME_VERSION_EVIDENCE_FILE")"
export BASE_URL=https://dev.oriso.org/
export ADMIN_BASE_URL=https://dev.oriso.org/admin
npm run test:runtime-version -- --project=dev
```

For an authorized PreDev acceptance run, first prepare a separate evidence record
with `environment: "predev"` and the PreDev URLs, then:

```bash
export RUNTIME_VERSION_EVIDENCE_FILE=runtime-version-evidence-predev.json
export ORISO_E2E_RUNTIME_EVIDENCE_JSON="$(cat "$RUNTIME_VERSION_EVIDENCE_FILE")"
export BASE_URL=https://predev.oriso.org/
export ADMIN_BASE_URL=https://predev.oriso.org/admin
npm run test:runtime-version -- --project=predev
```

Missing or malformed expectations fail before page navigation. The runner URLs
must match the independent record and selected environment. Each of the three
viewport checks (390×844, 820×1180, 1440×900) reads both public surfaces, requiring
one visible identity, the exact release, the full commit for that surface, and the
matching visible short label. A redirect to another environment/application fails.
G121 covers the Admin and App public surfaces. The App also renders its identity
in the authenticated layout; the Admin authenticated shell intentionally has no
footer. App authenticated layout, language, keyboard and accessibility acceptance
remain separate browser checks.

The report is under `playwright-report/runtime-version/`; attachments under
`test-results/runtime-version/` contain the selected expectations and observed
readbacks, including a readback on assertion failure. A failing Admin assertion
stops that viewport check before App: absent App evidence means **not evaluated**.
Retain those artifacts together with the independent provenance record.

## Interpret failures and local proof

| Failure                                                     | Meaning / next check                                                                                                                            |
| ----------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| Missing/malformed evidence or mutable image                 | Prerequisites are incomplete. Supply independent release/commit/digest proof; do not skip the gate.                                             |
| Environment, runner URL or readback URL mismatch            | Evidence/runner/navigation points at different targets. Correct the target selection.                                                           |
| Release mismatch with correct commit                        | The new bundle may be present while runtime release configuration is stale. Inspect the installed Helm release, ConfigMaps and pod environment. |
| Full commit mismatch with correct release                   | A stale or unexpected bundle is serving. Compare image digest, CI checkout, cache/service worker state and provenance.                          |
| Mixed Admin/App commits                                     | The two repository builds were misidentified or deployed inconsistently. Compare each image with its own expected source.                       |
| Missing/hidden/duplicate identity or incorrect visible text | Footer delivery/rendering is incomplete. Unknown identity is not deployment proof.                                                              |

Local validation commands:

```bash
npm run test:runtime-version:unit
npm run test:runtime-version:fixtures
npm run test:runtime-version -- --list --project=dev --project=fixtures
```

Unit tests exercise the expectation and comparison rules. Browser fixtures fulfil
all requests locally and exercise DOM readback with correct/wrong/missing/mixed
identities. `--list` proves only discovery. None of these local results proves a
Dev/PreDev deployment; use the explicit environment command for that. A missing
browser installation or unavailable target is an unmet prerequisite, not PASS.

Whole-ticket acceptance remains open until the operator release/Helm decision,
actual deployed image/configuration evidence and real PreDev readback are complete.
