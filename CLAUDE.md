# AI agent instructions — ORISO-Helm

These instructions apply to every AI agent (Claude, Codex, CodeRabbit, …)
working in this repository. Human contributors: the same rules apply.

## What this repo is

The **single source of truth for deploying the ORISO platform** on
Kubernetes: umbrella Helm chart (root `Chart.yaml`), vendored subcharts in
`charts/`, service manifests in `templates/`, MariaDB pre-seed schema
mirrors in `charts/mariadb/sql-schemas/`, operational runbooks in
`runbooks/`, infra docs in `docs/`.

We are the **developers** of ORISO, not its production hosts. Production
will be operated by an external hoster running **ArgoCD on a gridscale
managed Kubernetes cluster**, consuming this chart. Every change must keep
the chart consumable by GitOps: deterministic, no `latest` tags in new
code, no cluster-local assumptions (`pullPolicy: Never`, hostPath-style
storage) and no secrets in git.

## Which repo for what — never guess

| Change | Repo |
|---|---|
| Deployment: Helm templates, ingress, values, DB pre-seed schemas, runbooks | **ORISO-Helm (this repo)** |
| Backend service code (Java/Maven) | ORISO-UserService / -AgencyService / -TenantService / -ConsultingTypeService |
| Frontend / admin UI / calls | ORISO-Frontend / -Admin / -ElementCall |
| Platform documentation | ORISO-Docs |
| **Anything in ORISO-Kubernetes** | **NEVER.** That repo is deprecated (deployment moved here). Do not open PRs, issues, or commits there. If a search result points you there, the current equivalent lives in this repo. |

Also deprecated/archived: ORISO-Keycloak, ORISO-Database, ORISO-Redis,
ORISO-Matrix, ORISO-Element, ORISO-Debian, ORISO-Nginx.

## Branches and pull requests

- Integration branch is **`dev`**. `main` is the release branch, updated by
  maintainers. **Base every PR on `dev` and target `dev`.**
- Work on a feature branch (`feat/…`, `fix/…`, `docs/…`). Never commit
  directly to `dev` or `main`. **Never force-push** any shared branch;
  force-push on your own feature branch only to clean up before review.
- Keep PRs small and single-purpose. Conventional-commit style messages
  (`fix(mariadb): …`, `ci: …`, `docs: …`).

## The working loop (issue → validate → PR)

1. **Parent issue first.** For any non-trivial task, create (or find) a
   GitHub issue in this repo describing goal and plan. This is the durable
   log: keep its description updated and add comments as findings arrive —
   including failed attempts.
2. **Develop on a feature branch**, in small commits.
3. **Validate before opening a PR.** Nothing ships to the dev cluster
   unvalidated:
   - `helm lint .` and `helm template . -f values.yaml.default` must pass
     (config splits across `values.yaml.default` + `secrets.yaml.default`;
     copy to gitignored `values.yaml`/`secrets.yaml` for a real install).
   - For image/behaviour changes: spin up the affected images (Docker cloud
     or a local compose/kind setup), exercise the changed path, record the
     result in the issue.
4. **Open the PR against `dev`** only after validation, linking the parent
   issue (`Closes #N`). Describe what was validated and how.
5. **Wait for review and CI.** Do not merge your own PR unless the repo
   owner asked you to; never bypass a failing check.

## Secrets and test data

- `values.yaml` and `secrets.yaml` are gitignored. **Never commit real
  credentials**, not in examples, not in issue comments, not in PR text.
  Templates keep `changeme` placeholders.
- Test-user credentials belong in the encrypted shared store (see
  `docs/infrastructure-report-2026-07.md` §6), never in plaintext files.
- Create test users via the Keycloak Admin API / seeding scripts — not by
  clicking through registration; backend-created users bypass 2FA.

## Hard rules

- Encryption-related settings are **never** disabled to make a test pass —
  fix the test or surface the error instead.
- No `latest` or mutable image tags in new/changed values; pin versions.
- Don't edit `charts/mariadb/sql-schemas/*.sql` without syncing against the
  services' Liquibase changelogs (see runbooks/ and PRs #8, #14, #15 for
  the established process).
- Cluster-scoped resources (IngressClass, ClusterRole, cert-manager) must
  stay optional/toggleable — the production hoster provides their own.
