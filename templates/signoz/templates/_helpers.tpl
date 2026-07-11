{{/*
Expand the name of the chart.
*/}}
{{- define "signoz.name" -}}
{{- default "signoz" .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "signoz.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default "signoz" .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "signoz.labels" -}}
helm.sh/chart: {{ include "signoz.name" . }}-{{ .Chart.Version }}
{{ include "signoz.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "signoz.selectorLabels" -}}
app.kubernetes.io/name: {{ include "signoz.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
ClickHouse workload/service name. MUST stay "<release>-clickhouse": the
Pre-Dev cluster already runs StatefulSet/Service "oriso-platform-clickhouse"
(helm-owned by release "oriso-platform"), and keeping the name lets a helm
upgrade adopt the running instance instead of duplicating it.
*/}}
{{- define "signoz.clickhouse.fullname" -}}
{{- printf "%s-clickhouse" .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Resolved ClickHouse host: global override > signoz.clickhouse.host > the
in-chart ClickHouse service.
*/}}
{{- define "signoz.clickhouse.host" -}}
{{- $gSvc := default dict (.Values.global | default dict).services -}}
{{- $gCh := default dict $gSvc.clickhouse -}}
{{- $host := default .Values.signoz.clickhouse.host (default "" $gCh.host) -}}
{{- if $host -}}
{{- $host -}}
{{- else -}}
{{- include "signoz.clickhouse.fullname" . -}}
{{- end -}}
{{- end }}

{{/*
OTel gateway collector name. MUST stay "<release>-otel-collector" so the helm
upgrade adopts the running Deployment/Service/ConfigMap on Pre-Dev.
*/}}
{{- define "signoz.otelCollector.fullname" -}}
{{- printf "%s-otel-collector" .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
OTel log-collection agent (DaemonSet) name.
*/}}
{{- define "signoz.otelAgent.fullname" -}}
{{- printf "%s-otel-agent" .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
NOTE: the signoz/signoz single binary has NO OTLP listener (it serves
8080/4320/6060/9090 only). Anything that needs to ingest telemetry must go
through the gateway collector (signoz-otel-collector); do not add helpers
that point OTLP traffic at the signoz service.
*/}}

{{/*
Env for the signoz-otel-collector "migrate ..." subcommands (schema-migrator
Job and the gateway's sync-check initContainer). Mirrors upstream
snippet.telemetryStoreMigrator-env (chart signoz-0.132.2): ClickHouse
credentials plus the SIGNOZ_OTEL_COLLECTOR_* migration settings.
LC-M03: the password stays a secretKeyRef.
*/}}
{{- define "signoz.migratorEnv" -}}
- name: CLICKHOUSE_HOST
  value: {{ include "signoz.clickhouse.host" . | quote }}
- name: CLICKHOUSE_PORT
  value: {{ .Values.signoz.clickhouse.port | quote }}
- name: CLICKHOUSE_CLUSTER
  value: {{ .Values.signoz.clickhouse.cluster | quote }}
- name: CLICKHOUSE_USER
  value: {{ .Values.signoz.clickhouse.user | quote }}
- name: CLICKHOUSE_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.signoz.clickhouse.secret.name }}
      key: {{ .Values.signoz.clickhouse.secret.key }}
- name: SIGNOZ_OTEL_COLLECTOR_CLICKHOUSE_DSN
  value: "tcp://$(CLICKHOUSE_USER):$(CLICKHOUSE_PASSWORD)@$(CLICKHOUSE_HOST):$(CLICKHOUSE_PORT)"
- name: SIGNOZ_OTEL_COLLECTOR_CLICKHOUSE_CLUSTER
  value: "$(CLICKHOUSE_CLUSTER)"
- name: SIGNOZ_OTEL_COLLECTOR_TIMEOUT
  value: {{ .Values.signoz.schemaMigrator.timeout | default "10m" | quote }}
- name: SIGNOZ_OTEL_COLLECTOR_CLICKHOUSE_REPLICATION
  value: {{ .Values.signoz.schemaMigrator.enableReplication | quote }}
{{- end }}
