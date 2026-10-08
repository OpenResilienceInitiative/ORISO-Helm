{{/* One exact registry shared by first import, reconciliation and env wiring. */}}
{{- define "oriso.taskSubject" -}}
{{- $root := .root -}}{{- $task := .task -}}
{{- $override := index $root.Values.global.taskIdentities.subjects $task.key | default "" -}}
{{- if $override -}}{{- $override -}}{{- else -}}
{{- $hash := printf "oriso-task:%s:%s" $root.Values.global.keycloak.realm $task.clientId | sha256sum -}}
{{- printf "%s-%s-%s-%s-%s" (substr 0 8 $hash) (substr 8 12 $hash) (substr 12 16 $hash) (substr 16 20 $hash) (substr 20 32 $hash) -}}
{{- end -}}
{{- end -}}

{{- define "oriso.taskDefinitions" -}}
{{- if and .Values.global.taskIdentities.retireLegacy .Values.global.taskIdentities.legacyOtpCompatibility -}}
{{- fail "legacy OTP compatibility must be disabled before legacy retirement" -}}
{{- end -}}
{{- $root := . -}}{{- $result := list -}}{{- $seen := dict -}}
{{- range $legacy := list .Values.global.secrets.keycloakBackendTechnicalClientSecret .Values.global.secrets.keycloakBackendAdminClientSecret -}}
{{- if $legacy -}}{{- $_ := set $seen $legacy true -}}{{- end -}}
{{- end -}}
{{- range $task := .Files.Get "files/task-identities.json" | fromJsonArray -}}
{{- $secret := index $root.Values.global.taskIdentitySecrets $task.key | default "" | toString -}}
{{- if or (eq (trim $secret) "") (eq $secret "changeme") (hasKey $seen $secret) -}}
{{- fail (printf "taskIdentitySecrets.%s requires a distinct persistent secret; empty/example/reused credentials are rejected" $task.key) -}}
{{- end -}}{{- $_ := set $seen $secret true -}}
{{- $subject := include "oriso.taskSubject" (dict "root" $root "task" $task) -}}
{{- if not (regexMatch "^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$" $subject) -}}
{{- fail (printf "taskIdentities.subjects.%s must be the actual service-account UUID" $task.key) -}}
{{- end -}}
{{- $_ := set $task "subject" $subject -}}{{- $_ := set $task "secret" $secret -}}
{{- $result = append $result $task -}}
{{- end -}}
{{- range $purpose := list "provisioning" "maintenance" "tenantCreation" "wizardPolicy" -}}
{{- $key := index $root.Values.global.commandOriginKeys $purpose | default "" | toString -}}
{{- if or (lt (len ($key | b64dec)) 32) (ne (($key | b64dec) | b64enc) $key) (hasKey $seen $key) -}}
{{- fail (printf "commandOriginKeys.%s requires an independent Base64 key of at least 32 bytes" $purpose) -}}
{{- end -}}{{- $_ := set $seen $key true -}}
{{- end -}}
{{- toJson $result -}}
{{- end -}}

{{- define "oriso.taskBindingData" -}}
{{- $root := . -}}
{{- range $task := include "oriso.taskDefinitions" . | fromJsonArray }}
  IDENTITY_{{ $task.key }}_CLIENT_ID: {{ $task.clientId | quote }}
  IDENTITY_{{ $task.key }}_SERVICE_SUBJECT: {{ $task.subject | quote }}
{{- end -}}
{{- end -}}

{{- define "oriso.taskSecretEnv" -}}
{{- $root := .root -}}{{- $keys := .keys -}}
{{- range $task := include "oriso.taskDefinitions" $root | fromJsonArray -}}
{{- if has $task.key $keys }}
- name: KEYCLOAK_{{ $task.key }}_CLIENT_SECRET
  valueFrom:
    secretKeyRef:
      name: oriso-task-identity-credentials
      key: KEYCLOAK_{{ $task.key }}_CLIENT_SECRET
{{- end -}}{{- end -}}
{{- end -}}

{{- define "oriso.taskBindingEnv" -}}
{{- range $key := .keys -}}{{- range $suffix := list "CLIENT_ID" "SERVICE_SUBJECT" }}
- name: IDENTITY_{{ $key }}_{{ $suffix }}
  valueFrom:
    configMapKeyRef:
      name: oriso-task-identity-bindings
      key: IDENTITY_{{ $key }}_{{ $suffix }}
{{- end -}}{{- end -}}
{{- end -}}

{{- define "oriso.legacyRetirement" -}}
{{- $actors := list -}}
{{- if .Values.global.taskIdentities.retireLegacy -}}
{{- $subjects := .Values.global.taskIdentities.legacySubjects -}}
{{- range $actor := list (dict "key" "TECHNICAL_PASSWORD" "username" "technical") (dict "key" "SERVICE_ADMIN_PASSWORD" "username" "svc-keycloak-admin") (dict "key" "TECHNICAL_CLIENT" "username" "service-account-backend-technical" "clientId" "backend-technical") (dict "key" "ADMIN_CLIENT" "username" "service-account-backend-admin" "clientId" "backend-admin") -}}
{{- $subject := index $subjects $actor.key | default "" -}}
{{- if not (regexMatch "^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$" $subject) -}}
{{- fail (printf "taskIdentities.legacySubjects.%s requires the verified live UUID before retiring legacy access" $actor.key) -}}
{{- end -}}
{{- $_ := set $actor "subject" $subject -}}{{- $_ := unset $actor "key" -}}
{{- $actors = append $actors $actor -}}
{{- end -}}{{- end -}}
{{- toJson $actors -}}
{{- end -}}

{{- define "oriso.taskBindingsChecksum" -}}
{{- dict "subjects" .Values.global.taskIdentities.subjects "realm" .Values.global.keycloak.realm | toJson | sha256sum -}}
{{- end -}}

{{- define "oriso.taskCredentialsChecksum" -}}
{{- $credentials := dict -}}
{{- range $key := .keys -}}
{{- $_ := set $credentials $key (index $.root.Values.global.taskIdentitySecrets $key) -}}
{{- end -}}
{{- $credentials | toJson | sha256sum -}}
{{- end -}}

{{- define "oriso.commandKeysChecksum" -}}
{{- $keys := dict -}}
{{- range $purpose := .keys -}}
{{- $_ := set $keys $purpose (index $.root.Values.global.commandOriginKeys $purpose) -}}
{{- end -}}
{{- $keys | toJson | sha256sum -}}
{{- end -}}
