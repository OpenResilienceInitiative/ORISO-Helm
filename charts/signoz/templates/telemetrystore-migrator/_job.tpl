{{- define "telemetryStoreMigrator.podTemplate" -}}
{{- $root := .root -}}
{{- $values := $root.Values.telemetryStoreMigrator -}}
{{- $env := include "snippet.telemetryStoreMigrator-env" $root | fromYamlArray -}}
{{- if .passwordRef -}}
  {{- range $item := $env -}}
    {{- if eq $item.name "CLICKHOUSE_PASSWORD" -}}
      {{- $_ := unset $item "value" -}}
      {{- $_ := set $item "valueFrom" (dict "secretKeyRef" $.passwordRef) -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- $container := dict
  "image" (include "otelCollector.image" $root)
  "imagePullPolicy" $root.Values.otelCollector.image.pullPolicy
  "securityContext" $values.securityContext
  "env" $env -}}
{{- with $values.resources -}}
  {{- $_ := set $container "resources" . -}}
{{- end -}}
metadata:
  labels: {{- include "telemetryStoreMigrator.selectorLabels" $root | nindent 4 }}
spec:
  {{- with $values.affinity }}
  affinity: {{- toYaml . | nindent 4 }}
  {{- end }}
  {{- with $values.tolerations }}
  tolerations: {{- toYaml . | nindent 4 }}
  {{- end }}
  {{- with $values.nodeSelector }}
  nodeSelector: {{- toYaml . | nindent 4 }}
  {{- end }}
  {{- with $values.topologySpreadConstraints }}
  topologySpreadConstraints: {{- toYaml . | nindent 4 }}
  {{- end }}
  serviceAccountName: {{ include "telemetryStoreMigrator.serviceAccountName" $root }}
  automountServiceAccountToken: false
  securityContext: {{- toYaml $values.podSecurityContext | nindent 4 }}
  initContainers:
    {{- range $phase := list (list "ready") (list "bootstrap") (list "sync" "up") }}
    - {{- merge (dict "name" (first $phase) "args" (prepend $phase "migrate")) $container | toYaml | nindent 6 }}
    {{- end }}
  containers:
    - {{- merge (dict "name" "async" "args" (list "migrate" "async" "up")) $container | toYaml | nindent 6 }}
  restartPolicy: OnFailure
{{- end -}}

{{- define "telemetryStoreMigrator.checkReplacement" -}}
{{- if .root.Release.IsUpgrade -}}
  {{- $existing := lookup "batch/v1" "Job" .root.Release.Namespace "" -}}
  {{- range $job := $existing.items -}}
    {{- $labels := $job.metadata.labels | default dict -}}
    {{- if and (eq (get $labels "app.kubernetes.io/instance") $.root.Release.Name) (or $.hook (ne $job.metadata.name $.jobName)) -}}
      {{- $migrator := hasKey $labels "oriso.org/signoz-migrator" -}}
      {{- range $c := $job.spec.template.spec.containers -}}
        {{- if and (eq $c.name "async") (deepEqual $c.args (list "migrate" "async" "up")) -}}
          {{- $migrator = true -}}
        {{- end -}}
      {{- end -}}
      {{- if $migrator -}}
        {{- $terminal := false -}}
        {{- range $condition := dig "status" "conditions" (list) $job -}}
          {{- if and (eq $condition.status "True") (has $condition.type (list "Complete" "Failed")) -}}
            {{- $terminal = true -}}
          {{- end -}}
        {{- end -}}
        {{- if not $terminal -}}
          {{- fail (printf "SigNoz migration Job %s is still running. Wait for it to finish before replacing it." $job.metadata.name) -}}
        {{- end -}}
      {{- end -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- end -}}
