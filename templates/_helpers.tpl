{{/*
Render one complete OCI image reference. Tags and digests are both accepted so
development environments can deploy mutable dev tags when release digests are
not available.
*/}}
{{- define "oriso.immutableImage" -}}
{{- $valueName := index . 0 -}}
{{- required (printf "%s must be set" $valueName) (index . 1) -}}
{{- end -}}
