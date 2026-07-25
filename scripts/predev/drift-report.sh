#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RENDER_DIR="${1:?usage: drift-report.sh RENDER_DIR OUTPUT_DIR}"
OUTPUT_DIR="${2:?usage: drift-report.sh RENDER_DIR OUTPUT_DIR}"
EXPECTED="$RENDER_DIR/manifest.yaml"

if [[ ! -r "$EXPECTED" ]]; then
  echo "Rendered manifest is not readable: $EXPECTED" >&2
  exit 2
fi

mkdir -p "$OUTPUT_DIR"
PREVIOUS="$OUTPUT_DIR/previous-manifest.yaml"
LIVE="$OUTPUT_DIR/live-manifest.yaml"

if ! helm get manifest oriso-platform --namespace caritas >"$PREVIOUS" 2>"$OUTPUT_DIR/helm-get.err"; then
  : >"$PREVIOUS"
fi

python3 "$SCRIPT_DIR/collect_live.py" \
  --expected "$EXPECTED" \
  --previous "$PREVIOUS" \
  --output "$LIVE"

python3 "$SCRIPT_DIR/manifest_drift.py" \
  --expected "$EXPECTED" \
  --live "$LIVE" \
  --previous "$PREVIOUS" \
  --output "$OUTPUT_DIR/drift.json"
