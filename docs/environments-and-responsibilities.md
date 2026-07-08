# Environments & responsibilities

Tracking: #25. Background: `infrastructure-report-2026-07.md` §8–9.

The single page that says **who operates what, deployed how, from which config**
— the thing currently missing from every repo. Fill the `?` cells with the team
and finalise with the hoster (question list below).

## Who we are

We (Open Resilience Initiative) are the **developers** of ORISO. We do **not**
operate production. An external **hoster** runs production on a **gridscale
managed Kubernetes** cluster via **ArgoCD** (GitOps), consuming this chart.
Everything here must therefore stay GitOps-consumable: deterministic renders,
pinned image tags, no cluster-local assumptions, no secrets in git.

## Environments

| Environment | Cluster / host | Operated by | Deployment mechanism | Config source |
|---|---|---|---|---|
| Local / test | kind / Docker (dev laptop or Docker cloud) | each developer | `helm upgrade --install … -f values-dev.yaml` | ORISO-Helm working tree + local `values.yaml`/`secrets.yaml` |
| PreDev / Dev | Hetzner ("Dreambau" box) — *confirm* | us (developers) | Helm (→ ArgoCD once aligned) | ORISO-Helm `dev` branch + `values-dev.yaml` |
| Staging / UAT | ? | ? | ? | ? |
| Production | gridscale managed Kubernetes | external hoster | **ArgoCD** (GitOps) | ORISO-Helm chart release + `values-prod.yaml` + hoster-managed secrets |

> Deprecated: **ORISO-Kubernetes** was a former deployment path. It is being
> retired (see `runbooks/deprecate-oriso-kubernetes.md`); do not deploy from it.

## Config & secrets model

- **Values**: `values.yaml.default` is the prod-safe baseline; environments layer
  `values-dev.yaml` / `values-prod.yaml` on top (see README + issue #24).
- **Secrets**: `secrets.yaml` is gitignored and hand-filled today. For the dev
  cluster and the hoster handover this should move to a GitOps-native secret
  mechanism (SOPS+age / Sealed Secrets / External Secrets) — to be agreed with
  the hoster (question 2). The test-user store already uses SOPS+age (issue #23)
  and is a natural template.
- **Images**: GHCR, pinned tags (issue #22). No `latest`/`pullPolicy: Never` in
  what the hoster consumes.

## Open questions for the hoster (ArgoCD @ gridscale)

1. **Chart delivery**: does ArgoCD track a **packaged chart release** (from
   `.github/workflows/release-helm-chart.yml`) or the `main` branch of this repo?
   (Releases are the cleaner, versioned contract.)
2. **Secrets**: preferred mechanism — Sealed Secrets, SOPS+age, or External
   Secrets backed by their Vault? This decides how we restructure `secrets.yaml`.
3. **Ingress / cert-manager**: this chart currently *installs* an nginx ingress
   controller and cluster-scoped resources (IngressClass, ClusterRole,
   cert-manager, issuers). Managed clusters usually provide these — we need
   `enabled:` toggles so the hoster disables ours. Which do they provide?
4. **Storage**: gridscale storage classes (chart assumes `local-path` for
   SigNoz persistence) and any ReadWriteMany needs.
5. **Networking**: LoadBalancer specifics / exposed ports for LiveKit (SFU) and
   Matrix federation.
6. **Keycloak**: who owns realm config after go-live — our `realm.json` import
   vs. their runtime changes? How is drift handled?
7. **Observability**: SigNoz self-hosted in-cluster vs. their platform;
   resource budget (chart requests 1–4 CPU / 4–16Gi for SigNoz).

## Loose ends to chase

- **oriso.org** returned HTTP 503 during the July review — confirm what serves
  it and whether it's in scope here or hoster-side.
- Mail: ORISO-Kubernetes #63 ("dedicated mail server for oriso.org") — decide
  owner (ours vs hoster) and where verification/OTP mail is relayed.
