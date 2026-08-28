#!/usr/bin/env bash
#
# Pre-Dev drift check — is every ORISO deployment running its :pre-dev image?
#
# The previous version carried a hardcoded map of deployment names
# (oriso-platform-admin, oriso-platform-agencyservice, …). Those objects were
# renamed to bare names (admin, agencyservice, …), so every lookup came back
# empty and the script reported "DRIFT -> redeploy" for all twelve services,
# including the ones that were perfectly current. A checker that always cries
# wolf gets ignored, which is how three deployments sat on a demo session's
# local images for two days without anyone noticing.
#
# This version derives the service list from the cluster itself, so a rename or
# a new deployment cannot silently fall out of the report.
#
# Read-only: it inspects the cluster and the registry, and changes nothing.
#
# Usage: predev-drift-check.sh [namespace]        (default namespace: caritas)

set -uo pipefail

NS="${1:-${NS:-caritas}}"
REGISTRY="ghcr.io/openresilienceinitiative"

short() { # print the first 12 hex chars of a sha256 reference
    printf '%s' "${1#sha256:}" | cut -c1-12
}

printf '%-34s %-22s %-14s %-14s %-13s %s\n' DEPLOYMENT CONTAINER RUNNING PRE-DEV PULL STATUS

drift=0
frozen=0

while read -r dep container image pull; do
    [ -z "${dep:-}" ] && continue

    # Bare image name without registry, tag or digest — e.g. "oriso-admin".
    name="${image##*/}"
    name="${name%%@*}"
    name="${name%%:*}"

    # Only our own images. Third-party ones (redis, rabbitmq, livekit, synapse,
    # clickhouse-operator, signoz …) are not built from our branches.
    case "$image" in
        "$REGISTRY"/*) ;;
        *) case "$name" in oriso-*|element-call|health-dashboard|matrixrtc-*) ;; *) continue ;; esac ;;
    esac

    running_digest="$(printf '%s' "$image" | grep -o 'sha256:[0-9a-f]*' || true)"

    target="$(docker buildx imagetools inspect "$REGISTRY/$name:pre-dev" \
        --format '{{json .Manifest.Digest}}' 2>/dev/null | tr -d '"')"

    if [ -z "$target" ]; then
        status='?? no :pre-dev tag in registry'
    elif [ -z "$running_digest" ]; then
        # Running a tag instead of a digest. Two very different cases:
        # a deliberate release pin from our own registry (keycloak:2.0.2), or a
        # hand-built image imported straight into the node — the latter is what
        # froze Pre-Dev for two days and is real drift.
        case "$image" in
            "$REGISTRY"/*)
                status="PINNED tag :${image##*:}"
                ;;
            *)
                status="DRIFT local image (${image})"
                drift=$((drift + 1))
                ;;
        esac
    elif [ "$running_digest" = "$target" ]; then
        status='OK current'
    else
        status='DRIFT -> redeploy'
        drift=$((drift + 1))
    fi

    # imagePullPolicy: Never freezes the deployment: kubectl set image still
    # succeeds, but the kubelet never fetches the new layers, so regular deploys
    # appear to work and change nothing.
    if [ "$pull" = "Never" ]; then
        status="$status  [FROZEN: imagePullPolicy=Never]"
        frozen=$((frozen + 1))
    fi

    printf '%-34s %-22s %-14s %-14s %-13s %s\n' \
        "$dep" "$container" "$(short "$running_digest")" "$(short "$target")" "$pull" "$status"
done < <(kubectl get deploy -n "$NS" \
    -o custom-columns=NAME:.metadata.name,CONTAINER:.spec.template.spec.containers[0].name,IMAGE:.spec.template.spec.containers[0].image,PULL:.spec.template.spec.containers[0].imagePullPolicy \
    --no-headers 2>/dev/null)

echo
if [ "$drift" -eq 0 ] && [ "$frozen" -eq 0 ]; then
    echo "All ORISO deployments match their :pre-dev digest."
else
    echo "${drift} deployment(s) drifted, ${frozen} frozen on imagePullPolicy=Never."
    echo "Restore one with:"
    echo "  kubectl -n ${NS} set image deployment/<dep> <container>=${REGISTRY}/<image>@<digest>"
    echo "  kubectl -n ${NS} patch deployment/<dep> --type=json \\"
    echo "    -p='[{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/imagePullPolicy\",\"value\":\"Always\"}]'"
fi

exit 0
