# ORISO Infrastructure Report — July 2026

Status: draft for team review
Scope: ORISO-Kubernetes deprecation, target deployment picture, image builds,
test users / 2FA, dev-mode evaluation, AI working agreements.

---

## 1. Executive summary

- **ORISO-Helm (this repo) is now the single deployment source of truth.**
  The legacy **ORISO-Kubernetes** repo is functionally superseded but is *not*
  archived, has **12 open issues**, live CI workflows, and no deprecation
  notice — which is exactly why pull requests still land there (see §3).
- **The end picture:** we are the *developers* of ORISO. The *hoster* (an
  external team) will run the platform on a **gridscale managed Kubernetes
  cluster, deployed via ArgoCD** (GitOps). Our job is to make this chart
  consumable by ArgoCD: versioned chart releases, pinned image tags, and a
  secrets story that does not depend on a hand-edited local `secrets.yaml`.
  None of this hoster context is written down in any repo today — this
  document is the first place it is recorded.
- **Image builds are further along than assumed:** service repos (verified on
  ORISO-UserService) already build with Maven **inside GitHub Actions** on
  GitHub-hosted runners and push to `ghcr.io/openresilienceinitiative/*` on
  pushes to `main` and `dev`. The remaining work is standardising this across
  all repos, adding immutable tags, and decommissioning the self-hosted Maven
  builds (§5).
- **2FA does not block backend test-user creation.** OTP is a *login-time*
  challenge; users created via the Keycloak Admin API / service endpoints
  never see it. In this chart OTP is currently even disabled
  (`IDENTITY_OTP_ALLOWED_FOR_* = "false"`). The real gap is a shared, secure,
  structured store for test credentials (§6).
- **Recommendation on "development mode":** do not build a global
  less-secure mode and never make encryption toggleable. Use per-environment
  values overlays (`values-dev.yaml`) that only relax *test friction*
  (seeded users, mail catcher, verbose errors), keeping security features
  identical to production (§7).

---

## 2. The repository landscape (verified 2026-07-08)

| Repo | Role | State |
|---|---|---|
| **ORISO-Helm** | Canonical deployment chart (umbrella chart v2.0.1, vendored subcharts, ingress, SQL pre-seed mirrors, runbooks) | Active. Integration branch **`dev`**, release branch `main` (currently behind `dev`) |
| **ORISO-Kubernetes** | Legacy deployment repo (`helm/`, `ingress/`, `configmaps/`, `docs/`) | **Superseded but not archived.** 12 open issues, CI workflows, README says "Production Ready" |
| ORISO-UserService, -AgencyService, -TenantService, -ConsultingTypeService | Java/Maven backend services | Active; GH Actions CI builds + pushes images to GHCR |
| ORISO-Frontend, -Admin, -ElementCall, -HealthDashboard, -Livekit, -SignOZ | Frontend / tooling | Active |
| ORISO-Docs | Mintlify docs (docs.oriso.site) | Active; contains no hosting/infra documentation |
| ORISO-Keycloak, -Database, -Redis, -Matrix, -Element, -Debian, -Nginx | Old per-component deploy repos | **Already archived** — the model for what ORISO-Kubernetes should become |

Branch model in this repo: feature branches → PR into **`dev`** → (release)
merge into `main` → `release-helm-chart.yml` packages the chart. Recent
merged PRs (#8, #9, #12–#15) all target `dev`.

---

## 3. Why a PR "got shipped to Kubernetes" yesterday

Verified directly on GitHub:

- **ORISO-Kubernetes #87** ("Fix agency registration ingress rewrite",
  2026-07-08) — closed *not planned* with the comment "this Kubernetes repo
  is no longer used for this path".
- **ORISO-Kubernetes #89** ("Configure UserService agency admin URL",
  2026-07-08) — closed with "this deployment source has moved to ORISO-Helm;
  this Kubernetes chart PR should not be used".
- **ORISO-Kubernetes #84** (service-health-exporter fix, 2026-07-07) — same
  pattern.

Root cause: nothing tells a human or an AI agent that the repo is dead.
It is not archived, its README still says "Production Ready", it has open
issues and green CI, and no `CLAUDE.md`/`AGENTS.md` in the org points agents
at ORISO-Helm. An agent asked to "fix the agency ingress" finds matching
manifests in ORISO-Kubernetes and opens the PR there. The fix is
organisational, not technical — see the deprecation checklist below.

## 4. Deprecating ORISO-Kubernetes — impact and checklist

What still lives (only) there, and what to do about it:

1. **12 open issues** — triage each: migrate to ORISO-Helm or the relevant
   service repo, or close. Notably #71 (`latest` image tags →
   non-reproducible deployments — still true in this chart, see §5), #72
   (Keycloak-to-Helm migration + security gaps), #53 (minimal DB schema
   startup), #81/#83 (frontend toolchain epics — belong in the frontend
   repos), #63 (mail server — ops).
2. **Standalone `ingress/` + `configmaps/` manifests** — PR #87's language
   ("PreDev hotpatch … live PreDev manifest inspection") indicates the
   PreDev cluster was historically patched from these raw manifests. Before
   archiving, confirm PreDev is fully reconciled from ORISO-Helm and no
   `kubectl apply -f ingress/...` habit or cronjob remains.
3. **`docs/`** (multitenancy spec, redis-commander hardening) — copy into
   ORISO-Helm `docs/` or ORISO-Docs.
4. **CI workflows** — disable so the repo stops looking alive.
5. **README** — replace body with a deprecation banner linking to
   ORISO-Helm *before* archiving (archives are read-only afterwards).
6. **Cross-references** — search all org repos and ORISO-Docs for links to
   ORISO-Kubernetes and update them.
7. **Finally: archive the repo** (like ORISO-Nginx etc.). Archived repos
   reject new PRs — this permanently prevents yesterday's failure mode.

Risk if we skip steps 1–2 and archive immediately: losing track of the open
security issues, and a PreDev environment whose live state silently diverges
from any repo.

---

## 5. Image builds: from self-hosted Maven to GitHub Actions

**Current state (verified in ORISO-UserService):** `ci-main.yml` runs a
Maven build (Java 17) and a `docker-build-push` action to
`ghcr.io/openresilienceinitiative/oriso-userservice` on pushes to `main` and
`dev`, on `ubuntu-latest` GitHub-hosted runners. Feature-branch and PR
workflows exist alongside. So the GitHub-Actions build pipeline already
exists — the self-hosted Maven builds are a parallel legacy path, not the
only path.

**Gaps to close:**

1. **Audit every service/frontend repo** for the same three workflows
   (feature / PR / main) and identical build+push semantics; copy the
   UserService pattern where missing.
2. **Immutable tags.** Push `sha-<git-sha>` (and `dev`, `vX.Y.Z` on release)
   in addition to — or instead of — `latest`. This chart currently pins six
   workloads to `latest` (admin, agency-, consultingtype-, tenant-,
   userservice, livekit-token-service), which ArgoCD cannot meaningfully
   diff or roll back (ORISO-Kubernetes issue #71 flagged exactly this).
   `healthDashboard` is worse: `tag: rebuild, pullPolicy: Never` only works
   on a node that built the image locally — it can never deploy on gridscale.
3. **Preview images for the AI/PR loop.** On every PR, push
   `ghcr.io/...:pr-<n>` so a change can be spun up in Docker cloud / a
   throwaway namespace and validated *before* merge (this is the workflow
   codified in `CLAUDE.md`).
4. **Then decommission the self-hosted Maven builds** — after one release
   cycle in which every deployed image demonstrably came from Actions.
   (Server access is only needed for this final step.)
5. Optional hardening: build provenance/attestations and image signing
   (cosign) — the hoster may require it.

**Chart-side follow-up:** bump `imageVersion` fields to pinned tags as part
of each release; long-term, let a release workflow (or Renovate) update them.

---

## 6. Test users, 2FA, and a shared credential store

**Facts from this chart:** the Keycloak realm (`online-beratung`) has TOTP
configured as an *available* required action (not a default), and the
userservice currently runs with `IDENTITY_OTP_ALLOWED_FOR_USERS/CONSULTANTS/
ADMINS = "false"`. So 2FA is not what blocks mass test-user creation today.

**How to create many users without ever touching 2FA:** OTP is enforced at
interactive login only. Create users through the backend instead:

- **Keycloak Admin REST API** (`POST /admin/realms/<realm>/users`) with the
  existing technical-user/service-account credentials — set password,
  `emailVerified: true`, and *no* `CONFIGURE_TOTP` required action.
- Or the platform's own admin/registration endpoints where business objects
  (sessions, agencies) must exist too.
- For API tests, use the **direct-grant token endpoint** — a TOTP-less user
  gets a token with username+password alone.
- A small seeding job/script (`initializeDummyData` already exists as a
  flag) should be the standard way to produce N users in dev.

**Shared credential store — recommendation:** do *not* keep test credentials
in Google Docs or per-person files. Requirements were: central, structured,
multi-user, safe-but-accessible-to-AI, and writable when a dev flow creates
a new user. The best fit with zero new infrastructure:

> **A private `ORISO-TestData` git repo containing a structured
> `test-users.yaml`, encrypted with [SOPS](https://github.com/getsops/sops) +
> age.** Each team member has an age key; a dedicated **AI/automation key**
> is provided to agents via environment secret. `sops` decrypts in place for
> whoever holds any authorised key; git history gives audit and review.

- Structured: one YAML with entries like
  `{env: predev, tenant: t1, role: consultant, username, password, notes}`.
- Multi-user: adding/removing a person = re-encrypting with an updated key
  list (one command), not re-sharing files.
- AI access: the agent's key is just another recipient — revocable at any
  time; the "separate password" idea maps exactly to the separate age key.
- Write-back: a seeding script appends the user it just created and
  re-encrypts, so dev-created users are never lost.
- **Production credentials never go in this store** — that stays the
  hoster's domain (their Vault/ArgoCD secret management).

Alternatives considered: Vaultwarden/Bitwarden collection (nice UI, extra
service to run, weaker AI/scripting story); HashiCorp Vault (right answer at
larger scale, significant operational cost now). SOPS can be upgraded to
Vault later without changing the data model.

The same SOPS approach is also the natural path for `secrets.yaml` of the
*dev cluster* itself, and it is ArgoCD-compatible (via SOPS plugins or
Sealed Secrets) — worth aligning with the hoster early.

## 7. "Development mode" — evaluation

Verdict: **a single global dev-mode switch is the wrong shape; per-concern
values overlays are cheap and safe.**

- **Encryption stays on everywhere, always** — your own instinct is right.
  A build/config path where encryption can be off *will* eventually be on in
  the wrong place (this is a counselling platform; message content is
  maximally sensitive). It also makes dev non-representative exactly where
  the current big task (enabling E2E encryption) needs representative
  environments. Instead of toggling encryption, invest in **surfacing its
  errors**: encryption-related failures should be loud in logs/SigNoz and in
  API error responses in dev profiles, not swallowed.
- What *may* legitimately differ per environment, via `values-dev.yaml` vs
  `values-prod.yaml` overlays on this chart:
  - seeded tenants/users (`initializeDummyData`, seeding job from §6)
  - OTP posture for *test* users (already env-driven:
    `IDENTITY_OTP_ALLOWED_FOR_*`)
  - a mail catcher (Mailpit) instead of a real SMTP relay, so
    verification/OTP mails are instantly readable in tests
  - log levels, verbose error responses, relaxed rate limits
  - resource requests/replicas
- Complexity: **low** — Helm supports layered values files natively
  (`-f values.yaml -f values-dev.yaml`); ArgoCD supports the same per
  Application. Estimated effort: one PR introducing the overlay files plus
  templating the handful of currently hardcoded flags.
- One cleanup this exposes: `values.yaml.default` ships
  `springProfilesActive: "dev"` for agency-, tenant- and userservice — a
  production default file should say `prod`, with `dev` moved into
  `values-dev.yaml`.

---

## 8. Where documentation is missing (Hetzner / gridscale / oriso.org)

There is **no README or doc in any org repo** describing: current dev
hosting (Hetzner/"Dreambau box"), the future gridscale managed cluster, the
ArgoCD handover contract, or what runs behind oriso.org (the site returned
HTTP 503 during this review — worth checking). Suggested single page to
write next (in ORISO-Docs or here under `docs/`): "Environments &
responsibilities" — one table: environment / cluster / who operates it /
deployment mechanism / config source. The hoster conversation (§9) fills in
the unknowns.

## 9. Open questions for the hoster (ArgoCD @ gridscale)

1. Will ArgoCD consume the **packaged chart releases** (from
   `release-helm-chart.yml`) or track the `main` branch of this repo?
   (Chart releases are the cleaner contract.)
2. How do they want **secrets** delivered — Sealed Secrets, SOPS+age,
   External Secrets + their Vault? (Determines how we restructure
   `secrets.yaml`.)
3. Ingress/cert-manager: this chart currently *installs* an nginx ingress
   controller and cluster-scoped resources (IngressClass, ClusterRole,
   cert-manager). Managed clusters usually provide these — we likely need
   `enabled:` switches so the hoster can turn ours off.
4. gridscale storage classes (chart currently assumes `local-path` for
   SigNoz) and LoadBalancer specifics for LiveKit/Matrix federation ports.
5. Who owns Keycloak realm config drift after go-live (our `realm.json`
   import vs their runtime changes)?

---

## 10. Recommended next steps (ordered)

1. **Merge the AI working agreements** (`CLAUDE.md`, this PR) and copy the
   "which repo for what" table into every service repo's agent instructions.
2. **Deprecation pass on ORISO-Kubernetes** (checklist §4): triage the 12
   issues, verify PreDev runs only from ORISO-Helm, add deprecation banner,
   disable CI, archive. Target: this month, so the failure mode of §3 is
   structurally impossible.
3. **CI audit across service repos** + immutable image tags + PR preview
   tags (§5); then schedule decommissioning of the self-hosted Maven path.
4. **Stand up the SOPS-encrypted `ORISO-TestData` repo** and the user
   seeding script (§6).
5. **Introduce `values-dev.yaml`/`values-prod.yaml` overlays** and fix the
   `latest`/`rebuild`/`springProfilesActive` defaults (§7, §5).
6. **Meet the hoster** with the question list in §9; write the
   "Environments & responsibilities" page from the answers (§8).
7. Keep encryption always-on; scope the E2E-encryption enablement as its own
   epic with dev-first rollout and loud error reporting.
