# PreDev deployment authority

## Decision

`OpenResilienceInitiative/ORISO-Helm` on branch `pre-dev` is the only source
that may define or deploy the ORISO PreDev platform.

- Helm release: `oriso-platform`
- Kubernetes namespace: `caritas`
- Deployment workflow: `.github/workflows/predev-deploy.yml`
- Non-secret environment values: `values-pre-dev.yaml`
- Image lock: `deploy/predev/images.lock.yaml`
- Protected secrets: GitHub environment `predev`

`OpenResilienceInitiative/ORISO-Kubernetes` is archived and read-only. Its
workflows and the scripts still present under `/root/deploy-script` on the host
are historical evidence, not deployment authorities. They must not be copied,
edited, or run as an alternative PreDev path.

## Why this is reproducible

Every candidate is rendered from the same four layers, in this order:

1. `values.yaml.default`
2. `values-pre-dev.yaml`
3. `deploy/predev/images.lock.yaml`
4. the protected `PREDEV_VALUES_B64` secret overlay

The renderer records the exact Git commit, hashes of the three non-secret
layers, a redacted manifest hash, and the complete rendered image inventory.
Secret values never enter uploaded artifacts.

## Human and machine gates

1. A pull request to `pre-dev` must pass `PreDev release plan`.
2. Hassan reviews deployment-affecting changes and the rendered, redacted
   evidence.
3. The GitHub environment `predev` must require Hassan as a reviewer. This is
   a repository-settings handoff; workflow YAML cannot configure its own
   environment reviewers.
4. A maintainer dispatches `PreDev plan or deploy` from `pre-dev` with the full
   40-character commit SHA.
5. The workflow rejects any branch/SHA mismatch.
6. `plan` reads live state and reports `missing`, `extra`, and `changed`
   resources without mutating the cluster.
7. `deploy` uses `helm upgrade --install --atomic --wait`, then fails unless
   the live resources have zero remaining drift from the approved render.

The workflow does not merge a pull request and a green run is not deployment
approval. Human review remains mandatory.

## Drift semantics

- `missing`: rendered resource absent from the cluster
- `extra`: resource owned by the previous release but absent from the render
- `changed`: a rendered field has a different live value

API-server default fields that the chart does not control are ignored. Secret
values are redacted and only the set of Secret keys is compared.

## One-time repository settings handoff

Before enabling `deploy` mode:

- create the GitHub environment `predev`;
- require Hassan's approval;
- add `PREDEV_KUBECONFIG_B64` and `PREDEV_VALUES_B64` as environment secrets;
- confirm the credentials are limited to the `caritas` deployment scope;
- require `Reproducible PreDev render` on branch `pre-dev`.

After the first approved deployment, archive the host-side deploy scripts in
place and remove their execute permission only through a separately reviewed
operations change. Do not delete them during this code change.
