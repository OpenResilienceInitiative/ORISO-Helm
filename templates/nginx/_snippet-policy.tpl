{{- define "oriso.nginx.hasSnippetAnnotations" -}}
{{- range $name, $_ := . -}}
  {{- if and (hasPrefix "nginx.ingress.kubernetes.io/" $name) (hasSuffix "-snippet" $name) -}}
    {{- $name -}}
  {{- end -}}
{{- end -}}
{{- end -}}

{{- define "oriso.nginx.checkRenderedSnippetPolicy" -}}
{{- /* Keep this inventory in sync with the render check for snippet-producing templates. */ -}}
{{- $templates := list
  "media-scanner/media-scanner-direct-media-block-ingress.yaml"
  "nginx/account-inactivity-api-ingress.yaml"
  "nginx/element-call-ingress.yaml"
  "nginx/keycloak-token-rate-limit-ingress.yaml"
  "nginx/main-ingress.yaml"
  "nginx/matrix-client-ingress.yaml"
  "nginx/matrix-login-rate-limit-ingress.yaml"
  "nginx/userservice-public-entry-ingress.yaml"
  "signoz/signoz-ingress.yaml"
  "storybook/storybook-ingress.yaml"
-}}
{{- $pending := list -}}
{{- range $file := $templates -}}
  {{- $content := include (print $.Template.BasePath "/" $file) $ -}}
  {{- range $doc := regexSplit "(?m)^---[ \t]*$" $content -1 -}}
    {{- if trim $doc -}}
      {{- $ingress := fromYaml $doc -}}
      {{- if and (eq (get $ingress "kind") "Ingress") (include "oriso.nginx.hasSnippetAnnotations" (dig "metadata" "annotations" (dict) $ingress)) -}}
        {{- $pending = append $pending (dig "metadata" "name" $file $ingress) -}}
      {{- end -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- if $pending -}}
  {{- fail (printf "cannot disable ingress snippets: migrate the rendered Ingresses first (%s)" (join ", " (sortAlpha (uniq $pending)))) -}}
{{- end -}}
{{- end -}}

{{- define "oriso.nginx.checkLiveSnippetPolicy" -}}
{{- /* Include every namespace while an old cluster-wide pod still exists. */ -}}
{{- $watchNamespace := "" -}}
{{- if .Values.nginxController.scopeToReleaseNamespace -}}
  {{- $watchNamespace = .Release.Namespace -}}
  {{- $pods := lookup "v1" "Pod" .Release.Namespace "" -}}
  {{- range $pod := (get $pods "items" | default (list)) -}}
    {{- $labels := dig "metadata" "labels" (dict) $pod -}}
    {{- if and (eq (get $labels "app.kubernetes.io/instance") "ingress-nginx") (eq (get $labels "app.kubernetes.io/component") "controller") -}}
      {{- range $container := $pod.spec.containers -}}
        {{- if and (eq $container.name "controller") (not (has "--watch-namespace=$(POD_NAMESPACE)" ($container.args | default (list)))) -}}
          {{- $watchNamespace = "" -}}
        {{- end -}}
      {{- end -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- $classes := dict "nginx" true -}}
{{- $liveClasses := lookup "networking.k8s.io/v1" "IngressClass" "" "" -}}
{{- range $class := (get $liveClasses "items" | default (list)) -}}
  {{- if eq $class.spec.controller "k8s.io/ingress-nginx" -}}
    {{- $_ := set $classes $class.metadata.name true -}}
  {{- end -}}
{{- end -}}
{{- $ingresses := lookup "networking.k8s.io/v1" "Ingress" $watchNamespace "" -}}
{{- $pending := list -}}
{{- range $ingress := (get $ingresses "items" | default (list)) -}}
  {{- $class := dig "spec" "ingressClassName" "" $ingress -}}
  {{- $annotations := dig "metadata" "annotations" (dict) $ingress -}}
  {{- $served := ternary (hasKey $classes $class) (eq (get $annotations "kubernetes.io/ingress.class") "nginx") (ne $class "") -}}
  {{- if and $served (include "oriso.nginx.hasSnippetAnnotations" $annotations) -}}
    {{- $pending = append $pending (printf "%s/%s" $ingress.metadata.namespace $ingress.metadata.name) -}}
  {{- end -}}
{{- end -}}
{{- if $pending -}}
  {{- fail (printf "cannot disable ingress snippets: migrate the live Ingresses first (%s)" (join ", " (sortAlpha $pending))) -}}
{{- end -}}
{{- end -}}
