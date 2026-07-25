#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
OUTPUT_DIR="${1:?usage: render-release.sh OUTPUT_DIR}"
SECRETS_FILE="${PREDEV_SECRETS_FILE:-$REPO_ROOT/secrets.yaml.default}"

if [[ ! -r "$SECRETS_FILE" ]]; then
  echo "PreDev secret values file is not readable: $SECRETS_FILE" >&2
  exit 1
fi

mkdir -p "$OUTPUT_DIR"

VALUES=(
  "$REPO_ROOT/values.yaml.default"
  "$REPO_ROOT/values-pre-dev.yaml"
  "$REPO_ROOT/deploy/predev/images.lock.yaml"
  "$SECRETS_FILE"
)

HELM_ARGS=()
for value_file in "${VALUES[@]}"; do
  HELM_ARGS+=(-f "$value_file")
done

helm template oriso-platform "$REPO_ROOT" \
  --namespace caritas \
  "${HELM_ARGS[@]}" \
  >"$OUTPUT_DIR/manifest.yaml"

if grep -En 'image:[[:space:]]*["'\'']?[^[:space:]"'\'']*:latest(["'\'']?[[:space:]]*)?$' \
  "$OUTPUT_DIR/manifest.yaml"; then
  echo "PreDev render contains a floating latest image." >&2
  exit 1
fi

python3 "$SCRIPT_DIR/redact_manifest.py" \
  "$OUTPUT_DIR/manifest.yaml" \
  "$OUTPUT_DIR/manifest.redacted.yaml"

PROVENANCE_ARGS=()
for value_file in "${VALUES[@]:0:3}"; do
  PROVENANCE_ARGS+=(--values "$value_file")
done
python3 "$SCRIPT_DIR/provenance.py" \
  --manifest "$OUTPUT_DIR/manifest.yaml" \
  --redacted-manifest "$OUTPUT_DIR/manifest.redacted.yaml" \
  --output "$OUTPUT_DIR/provenance.json" \
  "${PROVENANCE_ARGS[@]}"

echo "Rendered PreDev release to $OUTPUT_DIR"
