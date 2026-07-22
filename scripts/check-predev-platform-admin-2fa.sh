#!/usr/bin/env bash
set -euo pipefail

KUBECTL_BIN="${KUBECTL_BIN:-kubectl}"
NAMESPACE="${NAMESPACE:-caritas}"
DEPLOYMENT="${DEPLOYMENT:-oriso-platform-userservice}"
CONFIGMAP="${CONFIGMAP:-userservice-configmap-env}"
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-120s}"
POLICY_KEY="IDENTITY_OTP_ALLOWED_FOR_TENANT_SUPER_ADMINS"

fail() {
  printf 'platform-admin 2FA runtime gate: FAIL — %s\n' "$*" >&2
  exit 1
}

config_value="$($KUBECTL_BIN -n "$NAMESPACE" get configmap "$CONFIGMAP" \
  -o "jsonpath={.data.${POLICY_KEY}}")" || fail "could not read ${CONFIGMAP}.${POLICY_KEY}"

if [[ "$config_value" != "true" ]]; then
  fail "${CONFIGMAP}.${POLICY_KEY} is '${config_value:-<empty>}', expected 'true'"
fi

deployment_env="$($KUBECTL_BIN -n "$NAMESPACE" get deployment "$DEPLOYMENT" \
  -o 'jsonpath={range .spec.template.spec.containers[0].env[*]}{.name}={.valueFrom.configMapKeyRef.name}:{.valueFrom.configMapKeyRef.key}{"\n"}{end}')" \
  || fail "could not read ${DEPLOYMENT} environment references"
expected_reference="${POLICY_KEY}=${CONFIGMAP}:${POLICY_KEY}"

if ! grep -Fqx "$expected_reference" <<<"$deployment_env"; then
  fail "${DEPLOYMENT} does not import ${POLICY_KEY} from ${CONFIGMAP}:${POLICY_KEY}"
fi

$KUBECTL_BIN -n "$NAMESPACE" rollout status "deployment/${DEPLOYMENT}" \
  --timeout="$ROLLOUT_TIMEOUT" >/dev/null \
  || fail "${DEPLOYMENT} is not ready"

effective_value="$($KUBECTL_BIN -n "$NAMESPACE" exec "deployment/${DEPLOYMENT}" -- \
  printenv "$POLICY_KEY")" || fail "could not read the effective pod value for ${POLICY_KEY}"

if [[ "$effective_value" != "true" ]]; then
  fail "effective pod value is '${effective_value:-<empty>}' for ${POLICY_KEY}, expected 'true'"
fi

revision="$($KUBECTL_BIN -n "$NAMESPACE" get deployment "$DEPLOYMENT" \
  -o 'jsonpath={.metadata.annotations.deployment\.kubernetes\.io/revision}')" \
  || fail "could not read ${DEPLOYMENT} revision"
image="$($KUBECTL_BIN -n "$NAMESPACE" get deployment "$DEPLOYMENT" \
  -o 'jsonpath={.spec.template.spec.containers[0].image}')" \
  || fail "could not read ${DEPLOYMENT} image"

if [[ "$image" != *@sha256:* ]]; then
  fail "${DEPLOYMENT} image is not digest-pinned: ${image}"
fi

printf 'platform-admin 2FA runtime gate: PASS namespace=%s revision=%s image=%s\n' \
  "$NAMESPACE" "$revision" "$image"
