{{- define "oriso.keycloakSmtpReconcilePod" -}}
{{- $mode := .mode -}}
{{- with .root -}}
{{- $image := required "keycloakSmtpReconcile.image is required and must use an immutable digest" .Values.keycloakSmtpReconcile.image -}}
{{- if not (regexMatch "^[^[:space:]]+@sha256:[a-f0-9]{64}$" $image) -}}
{{- fail "keycloakSmtpReconcile.image must use an immutable sha256 digest" -}}
{{- end -}}
restartPolicy: {{ if eq $mode "serve" }}Always{{ else }}Never{{ end }}
{{- if eq $mode "serve" }}
terminationGracePeriodSeconds: 60
{{- end }}
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
      {{- include "oriso.taskBindingEnv" (dict "keys" (list "SMTP_SYNC")) | nindent 6 }}
      {{- include "oriso.taskSecretEnv" (dict "root" . "keys" (list "SMTP_SYNC")) | nindent 6 }}
      {{- if eq $mode "trigger" }}
      - name: SMTP_RECONCILE_URL
        value: {{ printf "http://keycloak-reconcile-smtp.%s:8080/smtp/reconcile" .Release.Namespace | quote }}
      {{- end }}
    command: ["python3", "-B", "/scripts/keycloak-reconcile-smtp.py", {{ printf "--%s" $mode | quote }}]
    {{- if eq $mode "serve" }}
    ports:
      - name: http
        containerPort: 8080
    readinessProbe:
      httpGet:
        path: /health
        port: http
      periodSeconds: 5
      timeoutSeconds: 2
    livenessProbe:
      httpGet:
        path: /health
        port: http
      periodSeconds: 10
      timeoutSeconds: 2
    {{- end }}
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
{{- end -}}
