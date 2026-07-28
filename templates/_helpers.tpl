{{/*
Render one complete OCI image reference and reject mutable tags or malformed
digests. Zero digests remain valid placeholders in values.yaml.default so the
chart can be linted without inventing release artifacts; the coordinated
release preflight rejects those placeholders before deployment.
*/}}
{{- define "oriso.immutableImage" -}}
{{- $valueName := index . 0 -}}
{{- $image := required (printf "%s must be an immutable image digest" $valueName) (index . 1) -}}
{{- if not (regexMatch "^[^@[:space:]]+@sha256:[a-f0-9]{64}$" $image) -}}
{{- fail (printf "%s must use repository@sha256:<64 lowercase hex characters>" $valueName) -}}
{{- end -}}
{{- $image -}}
{{- end -}}

{{/*
Runtime metadata consumed by HealthDashboard and rollout tooling.
Use annotations because source branches can contain characters such as `/`.
*/}}
{{- define "oriso.runtimeAnnotations" -}}
{{- with .Values.global.sourceBranch }}
app.kubernetes.io/source-branch: {{ . | quote }}
oriso.org/source-branch: {{ . | quote }}
{{- end }}
{{- with .Values.global.platformRelease }}
app.kubernetes.io/platform-release: {{ . | quote }}
oriso.org/platform-release: {{ . | quote }}
{{- end }}
{{- end -}}
