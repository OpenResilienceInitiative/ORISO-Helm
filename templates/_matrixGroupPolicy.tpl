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
{{- $identitySuggestions := .Values.userService.identitySuggestions | default dict -}}
{{- $mediaScanner := .Values.mediaScanner | default dict -}}
{{- $reserved := list "matrix-secrets" "userservice-secret" "keycloak-secret" "redis-secret" "keycloak-backend-client-secrets" (dig "existingSecret" "name" "" (.Values.matrixrtcAuth | default dict)) (dig "existingSecret" "name" "" (.Values.matrixrtcLifecycle | default dict)) (dig "matrixAdminTokenSecret" "name" "" $identitySuggestions) (dig "requestSecret" "existingSecret" "" $mediaScanner) (dig "aiCheck" "existingSecret" "" $mediaScanner) -}}
{{- if has $secret $reserved -}}
{{- fail "AVV group policy requires a separate dedicated Secret resource" -}}
{{- end -}}
{{- $approvedDigest := "sha256:5f868df1f5772907c6dbe973a9b69ab530a5d6bb317c011a3788f7ad78eb1292" -}}
{{- if not (regexMatch (printf "^.+/synapse@%s$" $approvedDigest) .Values.matrix.image) -}}
{{- fail "AVV group policy requires the reviewed immutable Synapse 1.158.0 image digest" -}}
{{- end -}}
{{- if ne (int .Values.matrix.replicas) 1 -}}
{{- fail "AVV group policy currently supports one Synapse main process" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "oriso.matrixGroupPolicy.synapseChecksum" -}}
{{- if eq (include "oriso.matrixGroupPolicy.enabled" .) "true" -}}
{{- printf "%s/%s" (.Values.matrix.groupPolicy | toJson) (.Files.Get "files/matrix/oriso_group_policy.py") | sha256sum -}}
{{- else -}}
disabled
{{- end -}}
{{- end -}}

{{- define "oriso.matrixGroupPolicy.userServiceChecksum" -}}
{{- if eq (include "oriso.matrixGroupPolicy.enabled" .) "true" -}}
{{- .Values.matrix.groupPolicy | toJson | sha256sum -}}
{{- else -}}
disabled
{{- end -}}
{{- end -}}
