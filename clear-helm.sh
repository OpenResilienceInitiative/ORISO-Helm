#!/bin/bash
# Removes the Helm release "caritas" and its volumes.
# SigNoz, ClickHouse and ZooKeeper data plus the signoz.db backup are kept by
# default, so a reinstall reuses them. Run with --dry-run first.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: clear-helm.sh [--dry-run] [--include-observability]

  --dry-run                 print what would be deleted, delete nothing
  --include-observability   also delete the SigNoz, ClickHouse, ZooKeeper and
                            backup volumes (all dashboards, users, API keys,
                            alert rules and telemetry are lost)
EOF
}

RELEASE=caritas
NAMESPACE=caritas
DRY_RUN=false
INCLUDE_OBSERVABILITY=false

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    --include-observability) INCLUDE_OBSERVABILITY=true ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

# Claims the observability stack reuses on reinstall (see
# templates/signoz/signoz-data-protection.yaml), as patterns so every
# replica, shard and ZooKeeper member is covered, not only the first.
OBSERVABILITY_CLAIMS=(
  "signoz-db-${RELEASE}-signoz-[0-9]*"
  "data-volumeclaim-template-chi-${RELEASE}-clickhouse-*"
  "data-${RELEASE}-zookeeper-[0-9]*"
  "${RELEASE}-signoz-data-protection-backup"
)

is_observability_claim() {
  local pattern
  for pattern in "${OBSERVABILITY_CLAIMS[@]}"; do
    # shellcheck disable=SC2053 # the pattern is meant to match as a glob
    [[ "$1" == $pattern ]] && return 0
  done
  return 1
}

is_kept() {
  local claim="$1" keep_annotation="$2"
  if [ "$INCLUDE_OBSERVABILITY" = true ]; then
    return 1
  fi
  [ "$keep_annotation" = "keep" ] && return 0
  is_observability_claim "$claim"
}

run() {
  if [ "$DRY_RUN" = true ]; then
    echo "[dry-run] $*"
  else
    "$@"
  fi
}

# 1. Remove only the Helm release
echo "Uninstalling Helm release '${RELEASE}'..."
if helm status "$RELEASE" -n "$NAMESPACE" >/dev/null 2>&1; then
  run helm uninstall "$RELEASE" -n "$NAMESPACE"
else
  echo "Helm release '${RELEASE}' not found in namespace '${NAMESPACE}'."
fi

# 2. Delete the PVCs in the namespace, except the kept ones
echo "Deleting PVCs in namespace ${NAMESPACE}..."
kept_claims=()
deleted_claims=()
claims=$(kubectl get pvc -n "$NAMESPACE" \
  -o jsonpath='{range .items[*]}{.metadata.name}{"|"}{.metadata.annotations.helm\.sh/resource-policy}{"\n"}{end}')
while IFS='|' read -r claim keep_annotation; do
  [ -n "$claim" ] || continue
  if is_kept "$claim" "$keep_annotation"; then
    echo "Keeping PVC ${claim}"
    kept_claims+=("$claim")
  else
    run kubectl delete pvc "$claim" -n "$NAMESPACE"
    deleted_claims+=("$claim")
  fi
done <<<"$claims"

# 3. Delete unbound PVs that belonged to this namespace, except kept claims'.
# A PV still bound to a claim deleted above counts as unbound, so the dry run
# lists the volumes the real run frees and deletes.
echo "Deleting unattached PersistentVolumes of namespace ${NAMESPACE}..."
volumes=$(kubectl get pv \
  -o jsonpath='{range .items[*]}{.metadata.name}{"|"}{.status.phase}{"|"}{.spec.claimRef.namespace}{"|"}{.spec.claimRef.name}{"\n"}{end}')
while IFS='|' read -r volume phase claim_namespace claim; do
  [ -n "$volume" ] || continue
  [ "$claim_namespace" = "$NAMESPACE" ] || continue
  if [ "$phase" = "Bound" ]; then
    freed=false
    for deleted in ${deleted_claims[@]+"${deleted_claims[@]}"}; do
      [ "$claim" = "$deleted" ] && freed=true
    done
    [ "$freed" = true ] || continue
  fi
  keep=false
  if [ "$INCLUDE_OBSERVABILITY" != true ]; then
    is_observability_claim "$claim" && keep=true
    for kept in ${kept_claims[@]+"${kept_claims[@]}"}; do
      [ "$claim" = "$kept" ] && keep=true
    done
  fi
  if [ "$keep" = true ]; then
    echo "Keeping PV ${volume} (claim ${claim})"
  else
    # The provisioner may remove a freed volume itself in the meantime.
    run kubectl delete pv "$volume" --ignore-not-found
  fi
done <<<"$volumes"

if [ "$DRY_RUN" = true ]; then
  echo "Dry run: nothing was deleted."
elif [ "$INCLUDE_OBSERVABILITY" = true ]; then
  echo "Helm release '${RELEASE}', all its PVCs and its unattached PVs have been deleted."
else
  echo "Helm release '${RELEASE}' and its PVCs have been deleted; observability volumes were kept."
fi
