{{/*
Render an image reference from repository, tag, and optional digest.
Pre-Dev supplies digests for ORISO-owned images; other environments can
continue to use an explicit tag.
*/}}
{{- define "oriso.imageRef" -}}
{{- if .digest -}}
{{ printf "%s@%s" .repository .digest }}
{{- else -}}
{{ printf "%s:%s" .repository .tag }}
{{- end -}}
{{- end -}}
