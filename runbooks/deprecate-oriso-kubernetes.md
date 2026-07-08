# Runbook — retire the ORISO-Kubernetes repo

Tracking: #21. Background: `docs/infrastructure-report-2026-07.md` §3–4.

**Goal:** archive `OpenResilienceInitiative/ORISO-Kubernetes` without losing
open work, and make it structurally impossible to open PRs there again.

**Why now:** deployment moved to ORISO-Helm, but ORISO-Kubernetes is not
archived — README still says "Production Ready", CI is live, ~12 issues open,
and nothing routes contributors/agents here. PRs #84, #87, #89 all landed there
by mistake this week and were closed "moved to ORISO-Helm". Archiving is the
only step that *prevents* recurrence (archived repos reject new PRs).

**Owner:** a repo admin (archiving needs admin). Do the steps in order; archive
is last and effectively one-way (you can unarchive, but not receive PRs while
archived).

---

## Preconditions

- [ ] Confirm no automation deploys from ORISO-Kubernetes (see step 2).
- [ ] Announce the freeze in the team channel; stop merging there.

## Step 1 — Triage the open issues

Nothing is lost only if every open issue is migrated or consciously closed.
Proposed dispositions (confirm before acting):

| Issue | Title (abbrev.) | Disposition |
|---|---|---|
| #71 | `latest` image tags → non-reproducible deploys | **Migrate → ORISO-Helm #22** (already captured) |
| #72 | Epic: Keycloak → Helm + security gaps | **Migrate → ORISO-Helm** (new issue; real security work) |
| #53 | Support startup with minimal DB schema | **Migrate → ORISO-Helm** (relates to sql-schemas + Liquibase) |
| #50 | Align Jonas' K8s deployment with codebase | **Close** — superseded by ORISO-Helm being the source of truth |
| #54 | Setup understand-anything on k3s | **Migrate → ORISO-Helm or ops**, or close if obsolete |
| #59 | auto-update after every commit | **Migrate → ORISO-Helm #22** (CD concern) |
| #78 | Generate Understand graph for ORISO-Kubernetes | **Close** — repo being archived |
| #81 | EPIC: modernize frontend toolchains | **Migrate → frontend repos** (Frontend/Admin/ElementCall) |
| #83 | Element Web upgrade reference | **Migrate → ORISO-Frontend/-ElementCall** |
| #63 | Dedicated mail server for oriso.org | **Migrate → ops / ORISO-Helm #25** (infra) |
| #66 | Enable CodeRabbit PR reviews | **Migrate → org `.github` or per active repo** |
| #65 | [Question] SubdomainExtractor.java | **Migrate → ORISO-UserService** (code question) or close |

For each "Migrate": open the equivalent issue in the target repo, link back,
then close the ORISO-Kubernetes one with a comment pointing to the new home.

## Step 2 — Verify PreDev is reconciled only from ORISO-Helm

PR #87's description mentioned "live PreDev manifest inspection" and a "PreDev
hotpatch", i.e. the standalone `ingress/`/`configmaps/` manifests may have been
`kubectl apply`-ed directly. Before archiving:

- [ ] `kubectl -n <predev-ns> get ingress,cm -o yaml` and diff against what this
      chart renders (`helm template . -f values.yaml`). No orphan objects that
      exist only in ORISO-Kubernetes.
- [ ] Grep for any CI/cron/scripts doing `kubectl apply -f ingress/` or
      `-f configmaps/` (in ORISO-Kubernetes and any ops repo). Remove/redirect.
- [ ] Confirm the ArgoCD/Helm release for PreDev points at ORISO-Helm.

If PreDev still depends on raw manifests, **stop** and migrate that dependency
into the chart first — otherwise archiving silently freezes drift into place.

## Step 3 — Migrate docs

- [ ] Copy `docs/MULTITENANCY_MODULE_SPECIFICATION.md` and
      `docs/redis-commander-hardening-and-validation.md` into ORISO-Helm
      `docs/` (or ORISO-Docs), preserving history where practical.

## Step 4 — Deprecation banner in README (before archiving)

Archives are read-only, so land this while the repo is still writable. Replace
the README body with:

```markdown
# ⚠️ DEPRECATED — moved to ORISO-Helm

Deployment of the ORISO platform now lives in
**[ORISO-Helm](https://github.com/OpenResilienceInitiative/ORISO-Helm)**
(umbrella Helm chart, single source of truth). This repository is archived and
read-only. Do **not** open PRs or issues here — use ORISO-Helm.
```

## Step 5 — Disable CI

- [ ] Disable/delete the workflows under `.github/workflows/` (or set them to
      `on: workflow_dispatch` only) so the repo stops producing green checks
      that make it look alive.

## Step 6 — Update cross-references

- [ ] Search all org repos + ORISO-Docs for links to `ORISO-Kubernetes` and
      repoint them to ORISO-Helm (`gh search code`, or grep local clones).
- [ ] Update any org README / project board columns.

## Step 7 — Archive

- [ ] Settings → General → Danger Zone → **Archive this repository**.
- [ ] Confirm the repo now shows the "Public archive" banner and rejects new PRs.

---

## Abort / rollback

- Steps 1–6 are reversible. If step 2 uncovers a live PreDev dependency, pause
  the whole runbook until the chart absorbs it.
- Archiving is reversible (unarchive) but you cannot receive PRs while archived;
  only unarchive if migration to ORISO-Helm turns out incomplete.
