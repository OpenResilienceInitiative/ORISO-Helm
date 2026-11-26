# Pre-Dev lane

Pre-Dev (predev.oriso.org) is the experimental environment (ORISO-Helm#679). Work lands on the
`pre-dev` branches directly and is deployed from here without the full CI path; what proves
itself is ported to `dev` in larger PRs every few days.

The scripts run **on the Pre-Dev server as root**, started from GitHub Actions:
ORISO-Frontend → *ORISO Frontend Deploy* → ref `pre-dev`, environment `pre-dev`,
component `predev-lane`, `lane_action` = a script name below. The runner fetches this folder
from the `pre-dev` branch of ORISO-Helm first, so a pushed change is live on the next run.

| Script | Does |
|---|---|
| `status` | Read only: release, workloads not ready, restarting pods, add-ons, lane extras (names) |
| `helm-check` | Server-side dry run of the pre-dev chart; prints Helm's errors or the images it would roll out |
| `helm-upgrade` | Upgrades release `caritas` to the pre-dev chart (`--wait-for-jobs`, 20 min) |
| `addonservice-on` | Generates the AddonService DB password and token key, switches it on |

Values: `/root/ORISO-Helm/values.yaml` → `values-dev.yaml` → `values-pre-dev.yaml` →
`/root/ORISO-Helm/secrets.yaml` → lane extras (`/root/predev-lane/extra/`, one file per key,
mode 600). The old checkout `/root/ORISO-Helm` is only read.

Run logs are public: scripts print names, versions, images and Helm errors, never values,
secrets or rendered manifests.
