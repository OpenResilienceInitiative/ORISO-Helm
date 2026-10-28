{{/* This opt-in pair shares only a separately provisioned AVV credential. */}}
{{- define "oriso.matrixGroupPolicy.enabled" -}}
{{- $cfg := .Values.matrix.groupPolicy | default dict -}}
{{- $enabled := dig "enabled" false $cfg -}}
{{- if not (kindIs "bool" $enabled) -}}
{{- fail "matrix.groupPolicy.enabled must be a boolean" -}}
{{- end -}}
{{- $enabled | toString -}}
{{- end -}}

{{- define "oriso.matrixGroupPolicy.validate" -}}
{{- if eq (include "oriso.matrixGroupPolicy.enabled" .) "true" -}}
{{- $cfg := .Values.matrix.groupPolicy -}}
{{- $secret := required "matrix.groupPolicy.existingSecret.name is required" $cfg.existingSecret.name -}}
{{- $_ := required "matrix.groupPolicy.existingSecret.tokenKey is required" $cfg.existingSecret.tokenKey -}}
{{- $_ := required "matrix.groupPolicy.tokenRevision is required; change on rotation" $cfg.tokenRevision -}}
{{- if has $secret (list "matrix-secrets" "userservice-secret" "keycloak-secret" .Values.matrixrtcAuth.existingSecret.name .Values.matrixrtcLifecycle.existingSecret.name) -}}
{{- fail "AVV group policy requires a separate dedicated Secret resource" -}}
{{- end -}}
{{- if not (regexMatch "^(docker.io/)?matrixdotorg/synapse:v1\\.158\\.0(@sha256:[a-f0-9]{64})?$" .Values.matrix.image) -}}
{{- fail "AVV group policy requires the reviewed Synapse1.158.0 image" -}}
{{- end -}}
{{- if ne (int .Values.matrix.replicas) 1 -}}
{{- fail "AVV group policy currently supports one Synapse main process" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "oriso.matrixGroupPolicy.checksum" -}}
{{- printf "%s/%s" (.Values.matrix.groupPolicy | toJson) (.Files.Get "files/matrix/oriso_group_policy.py") | sha256sum -}}
{{- end -}}
