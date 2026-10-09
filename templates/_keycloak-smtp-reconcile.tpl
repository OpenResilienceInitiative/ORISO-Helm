{{- /* Short-lived SMTP sync pod: reads Admin Settings with the technical client,
writes the realm with the smtp-sync realm client. No master admin credential. */ -}}
{{- define "oriso.keycloakSmtpReconcilePod" -}}
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
        value: "smtp-sync"
      - name: SMTP_SYNC_CLIENT_SECRET
        valueFrom:
          secretKeyRef:
            name: keycloak-smtp-sync-client
            key: KEYCLOAK_SMTP_SYNC_CLIENT_SECRET
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
