#!/usr/bin/env bash
# Self-tests for tests/check-no-bare-ip-servername.sh (the ADR-005 / DB-M04 static
# guardrail). A guardrail that cannot fail is worthless, so we prove the scanner:
#   1. passes on the clean tree,
#   2. FAILS (exit 1) when a bare IPv4 `server_name` is injected into a template,
#   3. FAILS (exit 1) when a bare IPv4 `"m.server"` is injected,
#   4. exits LOUDLY (exit > 1) when a declared scan target is missing, instead of
#      silently reporting "no offenders".
#
# Every case runs against a throwaway copy of the chart sources, so the real repo
# is never mutated. A trap removes the temp dir on any exit.
set -euo pipefail

CHART_DIR="$(cd "$(dirname "$0")/.." && pwd)"
GUARD_REL="tests/check-no-bare-ip-servername.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fail=0
note() { printf '%s\n' "$*"; }

# Lay down a fresh, isolated copy of exactly what the guardrail scans.
seed() {
  local dst="$1"
  rm -rf "$dst"
  mkdir -p "$dst/tests"
  cp -R "$CHART_DIR/templates" "$dst/templates"
  cp "$CHART_DIR/values.yaml.default" "$dst/values.yaml.default"
  cp "$CHART_DIR/$GUARD_REL" "$dst/tests/"
  # Mirror any committed overlays so the copy matches the real scan surface.
  for f in "$CHART_DIR"/values-*.yaml; do
    [ -e "$f" ] && cp "$f" "$dst/"
  done
  return 0
}

# Run the guardrail copy inside a seeded tree; echo only its exit code.
run_guard() {
  local dst="$1"
  set +e
  bash "$dst/tests/check-no-bare-ip-servername.sh" >"$dst/out.log" 2>&1
  local rc=$?
  set -e
  echo "$rc"
}

# expect <label> <actual_rc> <test-op> <expected_rc>
expect() {
  local label="$1" actual="$2" op="$3" expected="$4"
  if [ "$actual" "$op" "$expected" ]; then
    note "PASS: $label (exit $actual)"
  else
    note "FAIL: $label (exit $actual, expected $op $expected)"
    fail=1
  fi
}

# 1) Clean tree passes.
C="$WORK/clean"; seed "$C"
rc="$(run_guard "$C")"
expect "clean tree passes" "$rc" -eq 0

# 2) Bare-IP server_name is rejected (exit 1).
S="$WORK/bareip-servername"; seed "$S"
cat > "$S/templates/matrix/adr005-selftest-fixture.yaml" <<'EOF'
# regression fixture: a bare IPv4 server_name must be rejected by the guardrail.
apiVersion: v1
kind: ConfigMap
data:
  homeserver.yaml: |
    server_name: 91.99.219.182
EOF
rc="$(run_guard "$S")"
expect "bare-IP server_name is rejected" "$rc" -eq 1

# 3) Bare-IP "m.server" is rejected (exit 1).
Mv="$WORK/bareip-mserver"; seed "$Mv"
cat > "$Mv/templates/matrix/adr005-selftest-fixture.yaml" <<'EOF'
# regression fixture: a bare IPv4 well-known m.server must be rejected.
apiVersion: v1
kind: ConfigMap
data:
  caritas.local: |
    { "m.server": "91.99.219.182:8448" }
EOF
rc="$(run_guard "$Mv")"
expect "bare-IP m.server is rejected" "$rc" -eq 1

# 4) A missing scan target exits loudly (> 1), never a silent pass.
T="$WORK/missing-target"; seed "$T"
rm -f "$T/values.yaml.default"   # a target the guardrail lists explicitly
rc="$(run_guard "$T")"
expect "missing scan target exits loudly" "$rc" -gt 1

note ""
if [ "$fail" -ne 0 ]; then
  note "guardrail self-tests FAILED"
  exit 1
fi
note "guardrail self-tests passed"
