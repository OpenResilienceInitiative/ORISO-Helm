{{/*
Moderator removal from a running group call (ORISO-Admin#1518): UserService asks
the policy gateway to remove a banned person's LiveKit participants. Missing
configuration is disabled; an explicit non-boolean fails.
*/}}
{{- define "oriso.matrixrtcEviction.enabled" -}}
{{- $cfg := .Values.matrixrtcEviction | default dict -}}
{{- $enabled := false -}}
{{- if hasKey $cfg "enabled" -}}
{{- $enabled = $cfg.enabled -}}
{{- end -}}
{{- if not (kindIs "bool" $enabled) -}}
{{- fail "matrixrtcEviction.enabled must be a boolean" -}}
{{- end -}}
{{- $enabled | toString -}}
{{- end -}}
