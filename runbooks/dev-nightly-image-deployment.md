# Nightly Dev image deployment

The Dev master schedules `/usr/local/sbin/oriso-dev-deploy-all` at 01:00 UTC. The [versioned launcher](../scripts/oriso-dev-deploy-all) uses the existing per-service deploy scripts. Review this release step with the Dev operator before applying it.

TenantService search now assumes the UserService batch endpoint is available. The launcher starts TenantService only after the UserService child succeeds. That child waits for Deployment readiness and verifies the installed image digest. If UserService is blocked, fails or times out, the launcher withholds TenantService. The other backend jobs still run in parallel, and the frontend wave starts after the backend wave finishes. [Directory rollout issue](https://github.com/OpenResilienceInitiative/ORISO-TenantService/issues/390)

## First installation from the reviewed Git revision

The example expects the unmanaged launcher read from Dev on 21 November 2026. Compare and review the installed script first if its digest has changed. Check that the UserService child still waits for rollout readiness and verifies the digest.

Run the following from the reviewed checkout on the Dev master during the approved window.

```bash
(
  set -euo pipefail
  installed=/usr/local/sbin/oriso-dev-deploy-all
  exec 9>/var/lock/oriso-dev-image-deploy.lock
  flock -n 9
  printf '%s  %s\n' '8ea0945b70531710b2c3079440bff3d4e40af6e1566ff102485d28ac4ded4600' "$installed" |
    sha256sum --check --status

  previous=$(mktemp -d /root/oriso-dev-deploy-all.XXXXXX)
  cp -p "$installed" "$previous/"
  staged=$(mktemp /usr/local/sbin/.oriso-dev-deploy-all.XXXXXX)
  trap 'rm -f "$staged"' EXIT
  install -m 0755 scripts/oriso-dev-deploy-all "$staged"
  bash -n "$staged"
  mv -T "$staged" "$installed"
  printf 'Previous launcher saved in %s\n' "$previous"
)
```

Run `/usr/local/sbin/oriso-dev-deploy-all --dry-run` after releasing the lock. The output should list UserService before TenantService. Keep the previous launcher until the nightly log confirms UserService succeeded before TenantService started. Verify the synthetic directory lookup after the successful rollout.

## Roll back the launcher

Use the same reviewed checkout. Set `PREVIOUS_LAUNCHER` to the saved file from the first installation. This check refuses to replace an installed launcher that has changed since the review.

```bash
(
  set -euo pipefail
  : "${PREVIOUS_LAUNCHER:?Set the saved launcher path}"
  installed=/usr/local/sbin/oriso-dev-deploy-all
  exec 9>/var/lock/oriso-dev-image-deploy.lock
  flock -n 9
  if ! cmp -s scripts/oriso-dev-deploy-all "$installed"; then
    printf 'The installed launcher differs from the reviewed checkout.\n' >&2
    exit 1
  fi

  staged=$(mktemp /usr/local/sbin/.oriso-dev-deploy-all.XXXXXX)
  trap 'rm -f "$staged"' EXIT
  install -m 0755 "$PREVIOUS_LAUNCHER" "$staged"
  bash -n "$staged"
  mv -T "$staged" "$installed"
)
```

The offline review fixtures use local synthetic subprocesses. Run them with `python3 -m unittest discover -s tests -p 'test_dev_nightly_deploy.py'`.
