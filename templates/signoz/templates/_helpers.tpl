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
