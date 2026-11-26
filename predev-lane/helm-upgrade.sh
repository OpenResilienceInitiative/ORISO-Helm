#!/bin/bash
# Upgrades the caritas release on Pre-Dev to the pre-dev chart.
source "$(dirname "$0")/lib.sh"
update_chart
cd "$CHART"
helm upgrade "$RELEASE" . -n "$NS" $(values_args) --wait-for-jobs --timeout 20m 2>&1 \
	| grep -vE '^coalesce.go' | sed -E 's/"[^"]*"/"…"/g' | tail -25 || echo "upgrade: FAILED (exit ${PIPESTATUS[0]})"
bash "$(dirname "$0")/status.sh"
