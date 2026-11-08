{{- define "otelCollector.validatePrivateOtlp" -}}
{{- $collector := .Values.otelCollector -}}
{{- $private := $collector.privateOtlp -}}
{{- if ne (dig "observability" "deploymentEnvironment" "" (.Values.global | default dict)) "production" -}}
{{- fail "otelCollector.privateOtlp requires the production observability environment" -}}
{{- end -}}
{{- if ne $collector.service.type "ClusterIP" -}}
{{- fail "otelCollector.privateOtlp requires a ClusterIP Service" -}}
{{- end -}}
{{- if $collector.ingress.enabled -}}
{{- fail "otelCollector.privateOtlp requires the vendored collector ingress disabled" -}}
{{- end -}}
{{- if not $collector.configMap.create -}}
{{- fail "otelCollector.privateOtlp requires configMap.create=true" -}}
{{- end -}}
{{- if or (ne ($collector.command.name | default "/signoz-otel-collector") "/signoz-otel-collector") $collector.command.extraArgs $collector.extraVolumeMounts $collector.extraVolumes -}}
{{- fail "otelCollector.privateOtlp requires the chart command and rejects custom arguments or mounts" -}}
{{- end -}}
{{- if hasKey ($collector.podLabels | default dict) "oriso.org/private-otlp" -}}
{{- fail "otelCollector.privateOtlp reserves the oriso.org/private-otlp Pod label" -}}
{{- end -}}
{{- if or (not (regexMatch "^[1-9][0-9]*$" (toString $private.maxRequestBytes))) (le (int $private.maxRequestBytes) 0) (gt (int $private.maxRequestBytes) 16777216) -}}
{{- fail "otelCollector.privateOtlp.maxRequestBytes must be explicitly set to 1 through 16777216" -}}
{{- end -}}
{{- if or (not (kindIs "slice" $private.producerMatchLabels)) (not $private.producerMatchLabels) -}}
{{- fail "otelCollector.privateOtlp.producerMatchLabels must contain approved same-namespace producer labels" -}}
{{- end -}}
{{- range $labels := $private.producerMatchLabels -}}
{{- if or (not (kindIs "map" $labels)) (not $labels) -}}
{{- fail "otelCollector.privateOtlp.producerMatchLabels requires nonempty matchLabels entries" -}}
{{- end -}}
{{- range $key, $value := $labels -}}
{{- if or (not $key) (not (kindIs "string" $value)) (not $value) -}}
{{- fail "otelCollector.privateOtlp.producerMatchLabels requires nonempty string keys and values" -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if not (kindIs "map" $private.policy) -}}
{{- fail "otelCollector.privateOtlp.policy must define both filter and transform" -}}
{{- end -}}
{{- range $name := list "filter" "transform" -}}
{{- $component := get $private.policy $name -}}
{{- if not (kindIs "map" $component) -}}
{{- fail (printf "otelCollector.privateOtlp.policy.%s must be an explicit OTTL processor" $name) -}}
{{- end -}}
{{- if and (hasKey $component "error_mode") (ne (toString (get $component "error_mode")) "propagate") -}}
{{- fail (printf "otelCollector.privateOtlp.policy.%s must propagate OTTL errors" $name) -}}
{{- end -}}
{{- end -}}
{{- range $signal := list "traces" "metrics" -}}
{{- $filter := get $private.policy.filter $signal -}}
{{- if or (not (kindIs "map" $filter)) (not $filter) -}}
{{- fail (printf "otelCollector.privateOtlp.policy.filter.%s requires reviewed filter rules" $signal) -}}
{{- end -}}
{{- end -}}
{{- range $name := list "trace_statements" "metric_statements" -}}
{{- $statements := get $private.policy.transform $name -}}
{{- if or (not (kindIs "slice" $statements)) (not $statements) -}}
{{- fail (printf "otelCollector.privateOtlp.policy.transform.%s requires reviewed statements" $name) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "otelCollector.privateConfig" -}}
{{- $private := .Values.otelCollector.privateOtlp -}}
{{- $existing := .Values.otelCollector.config -}}
{{- $filter := pick $private.policy.filter "traces" "metrics" -}}
{{- $transform := pick $private.policy.transform "trace_statements" "metric_statements" -}}
{{- $_ := set $filter "error_mode" "propagate" -}}
{{- $_ := set $transform "error_mode" "propagate" -}}
{{- $processors := pick $existing.processors "batch" "batch/meter" "signozspanmetrics/delta" -}}
{{- $_ := set $processors "memory_limiter" (dict "check_interval" "1s" "limit_percentage" 80 "spike_limit_percentage" 20) -}}
{{- $_ := set $processors "filter/oriso-private" $filter -}}
{{- $_ := set $processors "transform/oriso-private" $transform -}}
receivers:
  otlp:
    protocols:
      http:
        endpoint: 127.0.0.1:4318
        max_request_body_size: {{ int $private.maxRequestBytes }}
        compression_algorithms: [gzip]
processors:
  {{- toYaml $processors | nindent 2 }}
connectors:
  {{- pick $existing.connectors "signozmeter" | toYaml | nindent 2 }}
exporters:
  {{- pick $existing.exporters "clickhousetraces" "signozclickhousemetrics" "metadataexporter" "signozclickhousemeter" | toYaml | nindent 2 }}
extensions:
  health_check:
    endpoint: 0.0.0.0:13133
service:
  extensions: [health_check]
  telemetry:
    metrics:
      level: none
    logs:
      level: warn
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, filter/oriso-private, transform/oriso-private, signozspanmetrics/delta, batch]
      exporters: [clickhousetraces, metadataexporter, signozmeter]
    metrics:
      receivers: [otlp]
      processors: [memory_limiter, filter/oriso-private, transform/oriso-private, batch]
      exporters: [metadataexporter, signozclickhousemetrics, signozmeter]
    metrics/meter:
      receivers: [signozmeter]
      processors: [batch/meter]
      exporters: [signozclickhousemeter]
{{- end -}}

{{- define "otelCollector.privateProxyConfig" -}}
worker_processes 1;
error_log /dev/null emerg;
pid /tmp/nginx.pid;
events { worker_connections 128; }
http {
    access_log off;
    server_tokens off;
    client_body_temp_path /tmp/client;
    proxy_temp_path /tmp/proxy;
    fastcgi_temp_path /tmp/fastcgi;
    uwsgi_temp_path /tmp/uwsgi;
    scgi_temp_path /tmp/scgi;

    map $content_type $otlp_protobuf {
        default 0;
        application/x-protobuf 1;
    }
    map $http_content_encoding $otlp_encoding {
        default unsupported;
        "" "";
        identity "";
        gzip gzip;
    }
    map $upstream_http_retry_after $otlp_retry_after {
        default "";
        "~^[0-9]{1,3}$" $upstream_http_retry_after;
    }

    server {
        listen 4322;
        client_max_body_size {{ int .Values.otelCollector.privateOtlp.maxRequestBytes }};
        # nginx reserves 499 for a disconnected client.
        error_page {{ concat (untilStep 300 499 1) (untilStep 500 600 1) | join " " }} /_oriso_otlp_error;

        location = /_oriso_otlp_error {
            internal;
            alias /conf/private-otlp-error.pb;
            default_type application/x-protobuf;
            add_header Cache-Control "no-store" always;
            add_header Retry-After $otlp_retry_after always;
        }

        location = /healthz { return 204; }

        location ~ ^/v1/(traces|metrics)$ {
            if ($request_uri !~ "^/v1/(traces|metrics)$") { return 404; }
            if ($request_method != POST) { return 405; }
            if ($otlp_protobuf = 0) { return 415; }
            if ($otlp_encoding = unsupported) { return 415; }

            proxy_http_version 1.1;
            proxy_request_buffering off;
            proxy_next_upstream off;
            proxy_pass_request_headers off;
            proxy_set_header Content-Type application/x-protobuf;
            proxy_set_header Content-Encoding $otlp_encoding;
            proxy_set_header Connection "";
            proxy_intercept_errors on;
            proxy_hide_header Retry-After;
            proxy_hide_header WWW-Authenticate;
            proxy_hide_header Location;
            proxy_hide_header Set-Cookie;
            proxy_pass http://127.0.0.1:4318;
        }

        location / { return 404; }
    }
}
{{- end -}}
