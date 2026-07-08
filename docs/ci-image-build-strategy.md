# CI image-build strategy — GitHub Actions

Status: proposed. Tracking: #22. Background: `infrastructure-report-2026-07.md` §5.

Goal: every deployed image is built and pushed by **GitHub Actions** (not the
self-hosted Maven servers), tagged **immutably** so ArgoCD on gridscale can
diff/roll back deterministically, and every PR produces a **preview image** the
AI/dev loop can spin up in Docker cloud *before* merge.

## Where we already are (verified 2026-07-08, ORISO-UserService)

The service repos already build in Actions on GitHub-hosted runners:

- `.github/workflows/ci-main.yml` → on push to `main`/`dev`: `maven-build`
  (Java 17) then `docker-build-push` → GHCR.
- `.github/actions/docker-build-push` uses `docker/metadata-action` and pushes:
  - `sha-<commit>` (immutable — good, already there)
  - `latest` on `refs/heads/main`
  - `dev` on `refs/heads/dev`
- `REGISTRY=ghcr.io`, `ORG=openresilienceinitiative`.
- Feature-branch and PR workflows also exist (`ci-feature-branch.yml`,
  `ci-pull-request.yml`).

So the pipeline exists. The self-hosted Maven build is a **parallel legacy
path**, and three gaps remain.

## Gaps to close

1. **No PR-preview image.** `ci-pull-request.yml` builds but (per the action)
   doesn't push a `pr-<n>` tag. Without it there's nothing to `docker run` /
   deploy-to-throwaway-namespace for pre-merge validation.
2. **`latest` is still produced and consumed.** This chart pins six workloads
   to `latest` and `healthDashboard` to `rebuild` + `pullPolicy: Never`.
   `latest` is non-reproducible for ArgoCD; `pullPolicy: Never` cannot work on
   a managed cluster (the image only exists on a node that built it locally).
3. **No release/semver tag.** Nothing emits `vX.Y.Z` on a tagged release, so
   the chart can't pin to a version.

## Tagging convention (target)

| Trigger | Tags pushed | Consumed by |
|---|---|---|
| push `main` | `sha-<sha>`, `vX.Y.Z` (if the commit is tagged), `latest` (compat only) | release pinning |
| push `dev` | `sha-<sha>`, `dev` | dev cluster (`dev` acceptable pre-handover) |
| pull_request | `pr-<number>`, `sha-<sha>` | **pre-merge validation** (Docker cloud / throwaway ns) |
| tag `v*` | `vX.Y.Z`, `vX.Y`, `sha-<sha>` | ArgoCD prod (immutable) |

`sha-<sha>` is always the source of truth: immutable and unambiguous. Moving
tags (`latest`, `dev`) stay only for humans/convenience, never for prod pinning.

The recommended `docker/metadata-action` `tags:` block that produces exactly
this is in [`ci/docker-metadata-tags.reference.yml`](./ci/docker-metadata-tags.reference.yml)
— drop it into each service's `docker-build-push` composite action.

## Rollout (next steps)

1. **Audit** every service + frontend repo: confirm the feature/PR/main
   workflows exist and share the build+push semantics above. Copy the
   UserService pattern where missing. (Cross-repo; outside ORISO-Helm.)
2. **Add PR-preview push**: in `ci-pull-request.yml`, push `pr-${{ github.event.number }}`.
   Requires `permissions: packages: write` and `pull_request` (not
   `pull_request_target`, to avoid leaking write to forks).
3. **Add release semver**: a `tag`-triggered job (or extend `ci-main.yml`) that
   emits `vX.Y.Z`/`vX.Y`.
4. **Migrate this chart off `latest`** (see below) once immutable tags are
   guaranteed for every service.
5. **Decommission self-hosted Maven** after one release cycle in which every
   deployed image demonstrably came from Actions (check the GHCR provenance).
6. Optional, if the hoster requires: build provenance/attestations + `cosign`
   signing.

## ORISO-Helm-side changes (separate PR, coordinated)

Not done here because it needs the real immutable tags to pin to and a dev
smoke test:

- `admin`, `agencyService`, `consultingTypeService`, `tenantService`,
  `userService`, `tokenService` → replace `imageVersion/tag: "latest"` with a
  pinned `sha-<sha>` or `vX.Y.Z`.
- `healthDashboard` → replace `tag: rebuild` + `pullPolicy: Never` with a real
  GHCR tag and `pullPolicy: IfNotPresent` (this is the one that outright breaks
  on gridscale).
- Long-term: let the release workflow (or Renovate) bump these automatically.

## Verification (per service, before trusting the pipeline)

- [ ] Open a throwaway PR → confirm a `pr-<n>` image appears in GHCR.
- [ ] `docker pull ghcr.io/openresilienceinitiative/<svc>:pr-<n>` from a clean host.
- [ ] Push to `dev` → `sha-<sha>` + `dev` tags appear.
- [ ] Tag `v*` → semver tags appear.
- [ ] Deploy the `sha-` tag to pre-dev; app comes up.
