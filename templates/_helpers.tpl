{{/*
Render one complete OCI image reference. Release overlays can enable the strict
digest-only gate while local development keeps accepting mutable tags.
*/}}
{{- define "oriso.immutableImage" -}}
{{- $valueName := index . 0 -}}
{{- $value := required (printf "%s must be set" $valueName) (index . 1) -}}
{{- $requireImmutable := false -}}
{{- if ge (len .) 3 -}}
{{- $requireImmutable = index . 2 -}}
{{- end -}}
{{- if and $requireImmutable (not (regexMatch "^[^@[:space:]]+@sha256:[a-f0-9]{64}$" $value)) -}}
{{- fail (printf "%s must use repository@sha256:<64 lowercase hex>" $valueName) -}}
{{- end -}}
{{- $value -}}
{{- end -}}

{{/*
Default image pull policy for chart-managed workloads. Keep this value
environment-overridable from values.yaml/secrets.yaml instead of hardcoding it
in templates.
*/}}
{{- define "oriso.imagePullPolicy" -}}
{{- default "Always" .Values.global.imagePullPolicy -}}
{{- end -}}

{{/*
Resolve whether ORISO services should export OTLP telemetry.
Enabling the bundled SigNoz dependency turns this on automatically unless
global.observability.autoEnableWithSignoz is explicitly false.
*/}}
{{- define "oriso.observabilityEnabled" -}}
{{- $autoEnableWithSignoz := true -}}
{{- if hasKey .Values.global.observability "autoEnableWithSignoz" -}}
{{- $autoEnableWithSignoz = .Values.global.observability.autoEnableWithSignoz -}}
{{- end -}}
{{- $signoz := get .Values "signoz" | default dict -}}
{{- $signozEnabled := get $signoz "enabled" | default false -}}
{{- if or .Values.global.observability.otlpEnabled (and $signozEnabled $autoEnableWithSignoz) -}}true{{- else -}}false{{- end -}}
{{- end -}}

{{/*
Resolve the OTLP HTTP collector host. A manually supplied collector wins; when
the bundled SigNoz chart is enabled, use its in-cluster collector service.
*/}}
{{- define "oriso.otlpCollectorHost" -}}
{{- $signoz := get .Values "signoz" | default dict -}}
{{- $signozEnabled := get $signoz "enabled" | default false -}}
{{- if .Values.global.observability.otlpCollectorHost -}}
{{- .Values.global.observability.otlpCollectorHost -}}
{{- else if $signozEnabled -}}
{{- printf "%s.%s:%v" (include "oriso.signozOtelCollectorServiceName" .) .Release.Namespace (get $signoz "orisoOtelCollectorHttpPort" | default 4318) -}}
{{- end -}}
{{- end -}}

{{- define "oriso.signozServiceName" -}}
{{- $signoz := get .Values "signoz" | default dict -}}
{{- default (printf "%s-signoz" .Release.Name) (get $signoz "orisoServiceNameOverride" | default "") -}}
{{- end -}}

{{- define "oriso.signozOtelCollectorServiceName" -}}
{{- $signoz := get .Values "signoz" | default dict -}}
{{- default (printf "%s-signoz-otel-collector" .Release.Name) (get $signoz "orisoOtelCollectorServiceNameOverride" | default "") -}}
{{- end -}}

{{- define "oriso.signozExternalUrl" -}}
{{- $signoz := get .Values "signoz" | default dict -}}
{{- default (printf "https://%s/signoz" .Values.global.domainName) (get $signoz "externalUrl" | default "") -}}
{{- end -}}

{{/*
Mirror the vendored ClickHouse chart's public naming contract so the parent
chart can bind cluster-scoped discovery permissions to the exact operator
service account. The render contract compares this result with the rendered
operator Deployment and will fail if an upstream chart update changes it.
*/}}
{{- define "oriso.signozClickhouseFullname" -}}
{{- $signoz := get .Values "signoz" | default dict -}}
{{- $clickhouse := get $signoz "clickhouse" | default dict -}}
{{- $fullnameOverride := get $clickhouse "fullnameOverride" | default "" -}}
{{- if $fullnameOverride -}}
{{- $fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := get $clickhouse "nameOverride" | default "clickhouse" -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "oriso.signozClickhouseOperatorFullname" -}}
{{- $signoz := get .Values "signoz" | default dict -}}
{{- $clickhouse := get $signoz "clickhouse" | default dict -}}
{{- $operator := get $clickhouse "clickhouseOperator" | default dict -}}
{{- printf "%s-%s" (include "oriso.signozClickhouseFullname" .) (get $operator "name" | default "operator") | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "oriso.signozClickhouseOperatorServiceAccountName" -}}
{{- $signoz := get .Values "signoz" | default dict -}}
{{- $clickhouse := get $signoz "clickhouse" | default dict -}}
{{- $operator := get $clickhouse "clickhouseOperator" | default dict -}}
{{- $serviceAccount := get $operator "serviceAccount" | default dict -}}
{{- $create := true -}}
{{- if hasKey $serviceAccount "create" -}}
{{- $create = get $serviceAccount "create" -}}
{{- end -}}
{{- if $create -}}
{{- get $serviceAccount "name" | default (include "oriso.signozClickhouseOperatorFullname" .) -}}
{{- else -}}
{{- get $serviceAccount "name" | default "default" -}}
{{- end -}}
{{- end -}}

{{- define "oriso.signozClickhouseNamespace" -}}
{{- $signoz := get .Values "signoz" | default dict -}}
{{- $clickhouse := get $signoz "clickhouse" | default dict -}}
{{- get $clickhouse "namespace" | default .Release.Namespace -}}
{{- end -}}
