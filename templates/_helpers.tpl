{{/*
Render one complete OCI image reference. Release overlays can enable the strict
digest-only gate while local development keeps accepting mutable tags.
*/}}
{{- define "oriso.immutableImage" -}}
{{- $valueName := index . 0 -}}
{{- required (printf "%s must be set" $valueName) (index . 1) -}}
{{- end -}}

{{/*
Public URLs (ORISO-Helm#366): every public URL is derived from
global.domainName or given explicitly, and both are validated here, so a
missing or placeholder value fails the install instead of shipping links to
another host. These helpers are shared with the subcharts.
*/}}
{{/*
Placeholder check, aligned with the UserService startup validator: template
markers anywhere in the value, and reserved example hosts (RFC 2606/6761:
example.com/.org/.net/.test and their subdomains, any *.invalid) as host.
Usage: include "oriso.rejectUrlPlaceholder" (list "name" $value $host)
*/}}
{{- define "oriso.rejectUrlPlaceholder" -}}
{{- $name := index . 0 -}}
{{- $value := index . 1 -}}
{{- $host := lower (regexReplaceAll ":[0-9]*$" (index . 2) "") -}}
{{- range $marker := list "your-domain" "changeme" "todo-set" -}}
{{- if contains $marker (lower $value) -}}
{{- fail (printf "%s is still a placeholder (%q contains %q). Set the real public value for this environment." $name $value $marker) -}}
{{- end -}}
{{- end -}}
{{- range $reserved := list "example.com" "example.org" "example.net" "example.test" -}}
{{- if or (eq $host $reserved) (hasSuffix (printf ".%s" $reserved) $host) -}}
{{- fail (printf "%s points at the reserved example host %q, which is a placeholder. Set the real public value for this environment." $name $host) -}}
{{- end -}}
{{- end -}}
{{- if or (eq $host "invalid") (hasSuffix ".invalid" $host) -}}
{{- fail (printf "%s points at the reserved .invalid host %q, which is a placeholder. Set the real public value for this environment." $name $host) -}}
{{- end -}}
{{/* Browsers normalize shortened, octal, hex and integer IPv4 forms. Accept only
     canonical dotted decimal here so aliases such as 127.1 cannot bypass the
     loopback check while ordinary public IPv4 addresses remain valid. */}}
{{- if regexMatch "(?i)^(0x[0-9a-f]+|[0-9]+)(\\.(0x[0-9a-f]+|[0-9]+))*$" $host -}}
{{- $parts := splitList "." $host -}}
{{- if ne (len $parts) 4 -}}
{{- fail (printf "%s has an ambiguous numeric host %q. Use a DNS name or canonical dotted-decimal IPv4 address." $name $host) -}}
{{- end -}}
{{- range $part := $parts -}}
{{- if or (not (regexMatch "^(0|[1-9][0-9]{0,2})$" $part)) (gt (atoi $part) 255) -}}
{{- fail (printf "%s has an ambiguous numeric host %q. Use a DNS name or canonical dotted-decimal IPv4 address." $name $host) -}}
{{- end -}}
{{- end -}}
{{- if eq (index $parts 0) "127" -}}
{{- fail (printf "%s points at the loopback host %q. Set a publicly reachable host for emailed links." $name $host) -}}
{{- end -}}
{{- end -}}
{{- if or (eq $host "localhost") (hasSuffix ".localhost" $host) (eq $host "0.0.0.0") -}}
{{- fail (printf "%s points at the loopback host %q. Set a publicly reachable host for emailed links." $name $host) -}}
{{- end -}}
{{- end -}}

{{/*
Shared host[:port] check for global.domainName and the host of explicit URLs:
DNS labels of 1-63 chars that neither start nor end with a hyphen, no empty
labels, optional port 1-65535.
Usage: include "oriso.validateHost" (list "global.domainName" $hostPort)
*/}}
{{- define "oriso.validateHost" -}}
{{- $name := index . 0 -}}
{{- $hostPort := index . 1 -}}
{{- $label := "[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?" -}}
{{- if not (regexMatch (printf "^%s(\\.%s)*(:[0-9]{1,5})?$" $label $label) $hostPort) -}}
{{- fail (printf "%s must contain a valid host name: DNS labels without empty or hyphen-bounded parts, no scheme, path, trailing slash or whitespace (got %q)" $name $hostPort) -}}
{{- end -}}
{{- if contains ":" $hostPort -}}
{{- $port := atoi (last (splitList ":" $hostPort)) -}}
{{- if or (lt $port 1) (gt $port 65535) -}}
{{- fail (printf "%s has a port outside 1-65535 (got %q)" $name $hostPort) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* The validated public host name, e.g. dev.example.org. */}}
{{- define "oriso.domainName" -}}
{{- $domain := toString (.Values.global.domainName | default "") -}}
{{- if eq (trim $domain) "" -}}
{{- fail "global.domainName is required: set the public host name of this installation (e.g. app.example.org, no scheme, no path). There is no default on purpose." -}}
{{- end -}}
{{- include "oriso.rejectUrlPlaceholder" (list "global.domainName" $domain $domain) -}}
{{- include "oriso.validateHost" (list "global.domainName" $domain) -}}
{{- if contains ":" $domain -}}
{{- fail (printf "global.domainName must not contain a port (got %q): it is used as Ingress host and TLS host. Use the explicit userService.*BaseUrl overrides for non-default ports." $domain) -}}
{{- end -}}
{{- $domain -}}
{{- end -}}

{{/* The public origin <scheme>://<domainName>; scheme is https, or http when global.enableTls is false. */}}
{{- define "oriso.publicOrigin" -}}
{{- if .Values.global.enableTls }}https{{ else }}http{{ end }}://{{ include "oriso.domainName" . -}}
{{- end -}}

{{/*
An explicitly set public URL wins but must be absolute http(s), without
trailing slash or placeholder; empty falls back to the derived URL.
Usage: include "oriso.publicUrl" (list "userService.x" .Values.userService.x $derived)
*/}}
{{- define "oriso.publicUrl" -}}
{{- $name := index . 0 -}}
{{- $value := toString (index . 1 | default "") -}}
{{- $derived := index . 2 -}}
{{- if eq (trim $value) "" -}}
{{- $derived -}}
{{- else -}}
{{- include "oriso.rejectUrlPlaceholder" (list $name $value (regexReplaceAll "^[a-zA-Z]+://([^/]*).*$" $value "${1}")) -}}
{{- if not (regexMatch "^https?://[^/\\s]+(/[^\\s]*[^/\\s])?$" $value) -}}
{{- fail (printf "%s must be an absolute http(s) URL without trailing slash (got %q). Leave it empty to derive it from global.domainName." $name $value) -}}
{{- end -}}
{{- include "oriso.validateHost" (list $name (regexReplaceAll "^https?://([^/]+).*$" $value "${1}")) -}}
{{- $value -}}
{{- end -}}
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
{{- /* The SigNoz ingress is TLS-only, so this fallback stays https regardless of global.enableTls. */ -}}
{{- $explicit := get $signoz "externalUrl" | default "" -}}
{{- if $explicit -}}
{{- $explicit -}}
{{- else -}}
{{- printf "https://%s/signoz" (include "oriso.domainName" .) -}}
{{- end -}}
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

{{/*
Matrix identity (ADR-005, ORISO-Helm#366): the server_name is baked into every
user and room ID, so an empty or placeholder value must stop the install
instead of creating users under "your-server.local".
Usage: include "oriso.matrixServerName" (list "matrix.matrixServerName" $value)
*/}}
{{- define "oriso.matrixServerName" -}}
{{- $name := index . 0 -}}
{{- $value := toString (index . 1 | default "") -}}
{{- if eq (trim $value) "" -}}
{{- fail (printf "%s is required: set the Matrix server name of this installation (a host name, fixed for its lifetime). There is no default on purpose." $name) -}}
{{- end -}}
{{- if contains "your-server" (lower $value) -}}
{{- fail (printf "%s is still a placeholder (got %q). Set the real Matrix server name for this environment." $name $value) -}}
{{- end -}}
{{- include "oriso.rejectUrlPlaceholder" (list $name $value $value) -}}
{{- include "oriso.validateHost" (list $name $value) -}}
{{- end -}}

{{/*
In-cluster Keycloak admin endpoint for the post-install Jobs. It lives entirely
in values: a default host here would be the address that actually ships, while
an operator looks for it in values. tpl resolves the namespace, which only the
release knows.
*/}}
{{- define "oriso.keycloakAdminUrl" -}}
{{- $verify := default (dict) .Values.global.keycloak.verifyTwoFactorContract -}}
{{- required "global.keycloak.verifyTwoFactorContract.adminUrl must be set - see values.yaml.default" (tpl ($verify.adminUrl | default "") .) -}}
{{- end -}}
