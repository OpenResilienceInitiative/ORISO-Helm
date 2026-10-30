{{- /* Must match the smtp-sync client in charts/keycloak/keycloak-resources/realm.json. */ -}}
{{- define "oriso.smtpSyncClientId" -}}smtp-sync{{- end -}}

{{- /* Short-lived SMTP sync pod. C uses the sole SETUP actor and bounded resource;
the default legacy path keeps its established credentials. */ -}}
{{- define "oriso.keycloakSmtpReconcilePod" -}}
{{- include "oriso.scenarioC.validate" . -}}
{{- $image := required "keycloakSmtpReconcile.image is required and must use an immutable digest" .Values.keycloakSmtpReconcile.image -}}
{{- if not (regexMatch "^[^[:space:]]+@sha256:[a-f0-9]{64}$" $image) -}}
{{- fail "keycloakSmtpReconcile.image must use an immutable sha256 digest" -}}
{{- end -}}
{{- if eq .Values.global.keycloak.realm "master" -}}
{{- fail "global.keycloak.realm must not be master: the SMTP sync client lives in the ORISO realm" -}}
{{- end -}}
restartPolicy: Never
automountServiceAccountToken: false
securityContext:
  runAsNonRoot: true
  runAsUser: 1000
  runAsGroup: 1000
  seccompProfile:
    type: RuntimeDefault
containers:
  - name: reconcile-smtp
    image: {{ $image | quote }}
    imagePullPolicy: {{ include "oriso.imagePullPolicy" . | quote }}
    securityContext:
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: true
      capabilities:
        drop: ["ALL"]
    resources:
      requests:
        cpu: 25m
        memory: 32Mi
      limits:
        cpu: 250m
        memory: 64Mi
    env:
      - name: POD_NAMESPACE
        valueFrom:
          fieldRef:
            fieldPath: metadata.namespace
      - name: KEYCLOAK_URL
        value: {{ include "oriso.keycloakAdminUrl" . | quote }}
      - name: KEYCLOAK_REALM
        value: {{ .Values.global.keycloak.realm | quote }}
      - name: CONSULTING_TYPE_SERVICE_URL
        valueFrom:
          configMapKeyRef:
            name: userservice-configmap-env
            key: CONSULTING_TYPE_SERVICE_API_URL
      {{- if eq (include "oriso.scenarioC.enabled" .) "true" }}
      - name: TASK_IDENTITY_MODEL
        value: FIVE_ACTORS
      - name: TASK_IDENTITY_ISSUER
        value: {{ printf "%s/realms/%s" (trimSuffix "/" .Values.global.keycloak.authServerUrl) .Values.global.keycloak.realm | quote }}
      - name: KEYCLOAK_REALM_INTERNAL_ID
        value: {{ .Values.global.taskIdentities.realmId | quote }}
      - name: IDENTITY_SETUP_CLIENT_ID
        value: {{ .Values.global.taskIdentities.actors.SETUP.clientId | quote }}
      - name: IDENTITY_SETUP_SERVICE_SUBJECT
        value: {{ .Values.global.taskIdentities.actors.SETUP.serviceSubject | quote }}
      - name: IDENTITY_SETUP_CLIENT_SECRET
        valueFrom:
          secretKeyRef:
            name: {{ .Values.global.taskIdentities.actors.SETUP.secretRef.name | quote }}
            key: {{ .Values.global.taskIdentities.actors.SETUP.secretRef.key | quote }}
      - name: ORISO_TASK_CONTEXT_KEY
        valueFrom:
          secretKeyRef:
            name: {{ .Values.global.taskIdentities.contextKeySecretRef.name | quote }}
            key: {{ .Values.global.taskIdentities.contextKeySecretRef.key | quote }}
      {{- else }}
      - name: TECHNICAL_CLIENT_ID
        valueFrom:
          configMapKeyRef:
            name: userservice-configmap-env
            key: IDENTITY_TECHNICAL_CLIENT_ID
      - name: TECHNICAL_SERVICE_SUBJECT
        valueFrom:
          configMapKeyRef:
            name: tenantservice-configmap-env
            key: TECHNICAL_SERVICE_SUBJECT
      - name: TECHNICAL_CLIENT_SECRET
        valueFrom:
          secretKeyRef:
            name: keycloak-backend-client-secrets
            key: KEYCLOAK_BACKEND_TECHNICAL_CLIENT_SECRET
      - name: SMTP_SYNC_CLIENT_ID
        value: {{ include "oriso.smtpSyncClientId" . | quote }}
      - name: SMTP_SYNC_CLIENT_SECRET
        valueFrom:
          secretKeyRef:
            name: keycloak-smtp-sync-client
            key: KEYCLOAK_SMTP_SYNC_CLIENT_SECRET
      {{- end }}
    command: ["python3", "-B", "/scripts/keycloak-reconcile-smtp.py"]
    volumeMounts:
      - name: reconcile-script
        mountPath: /scripts
        readOnly: true
volumes:
  - name: reconcile-script
    configMap:
      name: keycloak-reconcile-smtp-script
      defaultMode: 0444
{{- end -}}
