{{/* One explicit model. Legacy emits no grouped identity configuration. */}}
{{- define "oriso.scenarioC.enabled" -}}
{{- $settings := default dict .Values.global.taskIdentities -}}
{{- $model := default "LEGACY" $settings.model -}}
{{- if not (has $model (list "LEGACY" "FIVE_ACTORS")) -}}
{{- fail "global.taskIdentities.model must be LEGACY or FIVE_ACTORS" -}}
{{- end -}}
{{- eq $model "FIVE_ACTORS" -}}
{{- end -}}

{{- define "oriso.scenarioC.secretRef" -}}
{{- $ref := . -}}
{{- if not (kindIs "map" $ref) -}}
{{- fail "global.taskIdentities requires an existing Secret reference" -}}
{{- end -}}
{{- if or (ne (len $ref) 2) (not (regexMatch "^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$" (default "" $ref.name))) (not (regexMatch "^[a-zA-Z0-9._-]+$" (default "" $ref.key))) -}}
{{- fail "global.taskIdentities Secret references require only valid name and key" -}}
{{- end -}}
{{- end -}}

{{- define "oriso.scenarioC.validate" -}}
{{- if eq (include "oriso.scenarioC.enabled" .) "true" -}}
{{- $s := .Values.global.taskIdentities -}}
{{- if or (not (kindIs "slice" $s.humanClientIds)) (empty $s.humanClientIds) -}}
{{- fail "global.taskIdentities requires reviewed humanClientIds" -}}
{{- end -}}
{{- $humanClients := list -}}
{{- range $client := $s.humanClientIds -}}
{{- if or (not (kindIs "string" $client)) (not (regexMatch "^[a-zA-Z0-9_-]{1,255}$" $client)) (has $client $humanClients) (has $client (list "admin-cli" "account" "broker" "security-admin-console")) (hasPrefix "backend-" $client) -}}
{{- fail "global.taskIdentities humanClientIds must be finite distinct reviewed interactive clients" -}}
{{- end -}}
{{- $humanClients = append $humanClients $client -}}
{{- end -}}
{{- if or (not (kindIs "string" $s.realmId)) (not (regexMatch "^[a-zA-Z0-9._-]{1,255}$" $s.realmId)) (not (regexMatch "^[1-9][0-9]{0,8}$" (toString (default "" $s.contextKeyVersion)))) -}}
{{- fail "global.taskIdentities requires the actual realmId and positive contextKeyVersion" -}}
{{- end -}}
{{- include "oriso.scenarioC.secretRef" $s.contextKeySecretRef -}}
{{- $actors := default dict $s.actors -}}
{{- if or (not (kindIs "map" $actors)) (ne (len $actors) 5) -}}
{{- fail "global.taskIdentities requires exactly five actor bindings" -}}
{{- end -}}
{{- $clients := list -}}{{- $subjects := list -}}{{- $refs := list (toJson $s.contextKeySecretRef) -}}
{{- range $actor := list "SETUP" "ACCOUNT_CREATE" "ACCOUNT_ADMIN" "MAIL" "RUNTIME" -}}
{{- $binding := default dict (get $actors $actor) -}}
{{- $expected := printf "backend-%s" ($actor | lower | replace "_" "-") -}}
{{- if or (not (kindIs "map" $binding)) (ne (len $binding) 3) (ne (default "" $binding.clientId) $expected) (has $binding.clientId $clients) (has $binding.clientId (list $.Values.global.keycloak.serviceAppClientId $.Values.global.keycloak.backendTechnicalClientId $.Values.global.keycloak.serviceAdminClientId)) -}}
{{- fail "global.taskIdentities requires five distinct canonical backend clients, separate from legacy and human clients" -}}
{{- end -}}
{{- $subject := default "" $binding.serviceSubject -}}
{{- if or (not (regexMatch "^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$" $subject)) (has $subject $subjects) (eq $subject $.Values.global.keycloak.serviceAdminSubject) -}}
{{- fail "global.taskIdentities requires distinct actual native serviceSubject UUIDs" -}}
{{- end -}}
{{- include "oriso.scenarioC.secretRef" $binding.secretRef -}}
{{- $ref := toJson $binding.secretRef -}}
{{- if has $ref $refs -}}
{{- fail "global.taskIdentities actor credentials and context key must use distinct Secret references" -}}
{{- end -}}
{{- $clients = append $clients $binding.clientId -}}{{- $subjects = append $subjects $subject -}}{{- $refs = append $refs $ref -}}
{{- end -}}
{{- if or (not (kindIs "map" $s.ordinaryGroups)) (empty $s.ordinaryGroups) -}}
{{- fail "global.taskIdentities requires reviewed ordinaryGroups" -}}
{{- end -}}
{{- range $path, $roles := $s.ordinaryGroups -}}
{{- if or (not (regexMatch "^/human-roles/[a-z0-9-]{1,80}$" $path)) (not (kindIs "slice" $roles)) (empty $roles) -}}
{{- fail "global.taskIdentities ordinaryGroups must use reviewed human-role paths and role lists" -}}
{{- end -}}
{{- $seen := list -}}
{{- range $role := $roles -}}
{{- if or (has $role $seen) (not (has $role (list "user" "consultant" "group-chat-consultant" "restricted-agency-admin" "user-admin"))) -}}
{{- fail "global.taskIdentities ordinaryGroups contain duplicate or protected/technical roles" -}}
{{- end -}}
{{- $seen = append $seen $role -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* Called inside the actual container env list; no Secret value is rendered. */}}
{{- define "oriso.scenarioC.env" -}}
{{- $root := .root -}}{{- $component := .component -}}
{{- include "oriso.scenarioC.validate" $root -}}
{{- if eq (include "oriso.scenarioC.enabled" $root) "true" -}}
{{- $s := $root.Values.global.taskIdentities -}}
- name: TASK_IDENTITY_MODEL
  value: "FIVE_ACTORS"
- name: KEYCLOAK_REALM_INTERNAL_ID
  value: {{ $s.realmId | quote }}
- name: ORISO_TASK_CONTEXT_KEY_VERSION
  value: {{ $s.contextKeyVersion | toString | quote }}
- name: ORISO_IDENTITY_HUMAN_CLIENT_IDS
  value: {{ $s.humanClientIds | toJson | quote }}
{{- range $actor := list "SETUP" "ACCOUNT_CREATE" "ACCOUNT_ADMIN" "MAIL" "RUNTIME" }}
{{- $binding := get $s.actors $actor }}
- name: IDENTITY_{{ $actor }}_CLIENT_ID
  value: {{ $binding.clientId | quote }}
- name: IDENTITY_{{ $actor }}_SERVICE_SUBJECT
  value: {{ $binding.serviceSubject | quote }}
{{- if eq $component "userservice" }}
- name: IDENTITY_{{ $actor }}_CLIENT_SECRET
  valueFrom:
    secretKeyRef: {{ $binding.secretRef | toJson }}
{{- end }}
{{- end }}
{{- if ne $component "agencyservice" }}
- name: ORISO_TASK_CONTEXT_KEY
  valueFrom:
    secretKeyRef: {{ $s.contextKeySecretRef | toJson }}
{{- end }}
{{- if has $component (list "tenantservice" "agencyservice" "consultingtypeservice") }}
- name: TASK_IDENTITY_AUDIENCE
  value: {{ $component | quote }}
- name: TASK_IDENTITY_REQUIRED_TASKS
  value: "CONFIG_WIZARD,INVITE_RESERVATIONS,NOTIFICATION_DISPATCH,SYSTEM_EMAIL_DELIVERY,MATRIX_AGENCY,SMTP_SYNC,RUNTIME_POLICY{{ if eq $component "tenantservice" }},CONSULTANT_IMPORT{{ end }}"
{{- end }}
{{- if or (eq $component "keycloak") (eq $component "userservice") }}
- name: ORISO_ACCOUNT_LIFECYCLE_ENABLED
  value: "true"
- name: ORISO_ACCOUNT_LIFECYCLE_REALM
  value: {{ $root.Values.global.keycloak.realm | quote }}
- name: ORISO_ACCOUNT_LIFECYCLE_GROUPS
  value: {{ $s.ordinaryGroups | toJson | quote }}
{{- end }}
{{- if eq $component "keycloak" }}
- name: ORISO_SMTP_CONTROL_ENABLED
  value: "true"
- name: ORISO_SMTP_CONTROL_REALM
  value: {{ $root.Values.global.keycloak.realm | quote }}
{{- end }}
{{- end -}}
{{- end -}}
