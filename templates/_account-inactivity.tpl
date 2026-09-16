{{- define "oriso.accountInactivity.accessGateEnabled" -}}
{{- dig "accountInactivity" "accessGateEnabled" false .Values.global | toString -}}
{{- end -}}

{{- define "oriso.accountInactivity.authAnnotations" -}}
{{- if eq (include "oriso.accountInactivity.accessGateEnabled" .) "true" }}
nginx.ingress.kubernetes.io/auth-url: "http://userservice.{{ .Release.Namespace }}.svc.cluster.local:8080/users/account-inactivity/access"
nginx.ingress.kubernetes.io/auth-method: "GET"
nginx.ingress.kubernetes.io/auth-proxy-set-headers: "{{ .Release.Namespace }}/account-inactivity-auth-headers"
{{- end }}
{{- end -}}

{{- define "oriso.mainApiPaths" -}}
- backend:
    service:
      name: agencyservice
      port:
        number: 8080
  path: /service/agencies
  pathType: Prefix
- backend:
    service:
      name: agencyservice
      port:
        number: 8080
  path: /service/agencyadmin
  pathType: Prefix
- backend:
    service:
      name: consultingtypeservice
      port:
        number: 8080
  path: /service/consultingtypes
  pathType: Prefix
- backend:
    service:
      name: consultingtypeservice
      port:
        number: 8080
  path: /service/consultingtypeadmin
  pathType: Prefix
- backend:
    service:
      name: consultingtypeservice
      port:
        number: 8080
  path: /service/topic
  pathType: Prefix
- backend:
    service:
      name: consultingtypeservice
      port:
        number: 8080
  path: /service/topicadmin
  pathType: Prefix
- backend:
    service:
      name: consultingtypeservice
      port:
        number: 8080
  path: /service/topic-groups
  pathType: Prefix
- backend:
    service:
      name: consultingtypeservice
      port:
        number: 8080
  path: /service/settingsadmin
  pathType: Prefix
- backend:
    service:
      name: consultingtypeservice
      port:
        number: 8080
  path: /service/settings
  pathType: Prefix
- backend:
    service:
      name: userservice
      port:
        number: 8080
  path: /service/users
  pathType: Prefix
- backend:
    service:
      name: userservice
      port:
        number: 8080
  path: /service/conversations
  pathType: Prefix
- backend:
    service:
      name: userservice
      port:
        number: 8080
  path: /service/liveproxy
  pathType: Prefix
- backend:
    service:
      name: userservice
      port:
        number: 8080
  path: /service/useradmin
  pathType: Prefix
- backend:
    service:
      name: userservice
      port:
        number: 8080
  path: /service/appointments
  pathType: Prefix
{{- end -}}
