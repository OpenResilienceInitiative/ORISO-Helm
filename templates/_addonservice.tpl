{{- define "oriso.addonService.image" -}}
{{- $image := dig "image" (dict) .Values.addonService -}}
{{- $repository := required "addonService.image.repository must be set when addonService.enabled is true" (get $image "repository" | default "") -}}
{{- $tag := required "addonService.image.tag must be set when addonService.enabled is true" (get $image "tag" | default "") -}}
{{- if hasPrefix "sha256:" $tag -}}
{{- printf "%s@%s" $repository $tag -}}
{{- else -}}
{{- printf "%s:%s" $repository $tag -}}
{{- end -}}
{{- end -}}

{{- define "oriso.addonService.dbUsername" -}}
{{- dig "secrets" "addonServiceDbUsername" "addonservice" .Values.global -}}
{{- end -}}

{{- define "oriso.addonService.dbPassword" -}}
{{- required "global.secrets.addonServiceDbPassword is required when addonService.enabled is true" (dig "secrets" "addonServiceDbPassword" "" .Values.global) -}}
{{- end -}}

{{- define "oriso.addonService.tokenPrivateKeyPem" -}}
{{- $pem := required "addonService.tokenPrivateKeyPem is required when addonService.enabled is true (PKCS#8 RSA key, see README)" (dig "tokenPrivateKeyPem" "" .Values.addonService) -}}
{{- if not (contains "-----BEGIN PRIVATE KEY-----" $pem) -}}
{{- fail "addonService.tokenPrivateKeyPem must be a PKCS#8 PEM (-----BEGIN PRIVATE KEY-----)" -}}
{{- end -}}
{{- $pem -}}
{{- end -}}

{{- define "oriso.addonService.discoveryNamespace" -}}
{{- dig "discovery" "namespace" "" .Values.addonService | default "addons" -}}
{{- end -}}

{{- define "oriso.addonService.kubernetesDiscovery" -}}
{{- dig "discovery" "kubernetes" true .Values.addonService | toString -}}
{{- end -}}

{{- define "oriso.addonService.dbJobPodTemplate" -}}
{{- $mariadb := index .Values "online-counseling-mariadb" -}}
metadata:
  labels:
    app: addonservice-db-init
  annotations:
    checksum/db-username: {{ include "oriso.addonService.dbUsername" . | sha256sum | quote }}
spec:
  restartPolicy: OnFailure
  automountServiceAccountToken: false
  securityContext:
    {{- include "oriso.podSecurityContext" (dict "user" 999) | nindent 4 }}
  containers:
    - name: addonservice-db-init
      securityContext:
        {{- include "oriso.containerSecurityContext" (dict) | nindent 8 }}
      image: {{ printf "%s:%s" $mariadb.imageName $mariadb.imageVersion | quote }}
      imagePullPolicy: {{ include "oriso.imagePullPolicy" . | quote }}
      env:
        - name: HOME
          value: /tmp
        - name: MARIADB_ROOT_PASSWORD
          valueFrom:
            secretKeyRef:
              name: mariadb-secret
              key: MYSQL_ROOT_PASSWORD
        - name: DB_USERNAME
          valueFrom:
            secretKeyRef:
              name: addonservice-secret
              key: SPRING_DATASOURCE_USERNAME
        - name: DB_PASSWORD
          valueFrom:
            secretKeyRef:
              name: addonservice-secret
              key: SPRING_DATASOURCE_PASSWORD
      command:
        - /bin/sh
        - -ec
        - |
          until mariadb-admin -h mariadb -uroot --password="$MARIADB_ROOT_PASSWORD" ping >/dev/null 2>&1; do
            echo "waiting for MariaDB"
            sleep 5
          done
          mariadb -h mariadb -uroot --password="$MARIADB_ROOT_PASSWORD" <<SQL
          CREATE DATABASE IF NOT EXISTS addonservice CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
          CREATE USER IF NOT EXISTS '${DB_USERNAME}'@'%' IDENTIFIED BY '${DB_PASSWORD}';
          ALTER USER '${DB_USERNAME}'@'%' IDENTIFIED BY '${DB_PASSWORD}';
          GRANT ALTER, CREATE, CREATE VIEW, DELETE, DROP, INDEX, INSERT, REFERENCES, SELECT, SHOW VIEW, TRIGGER, UPDATE, ALTER ROUTINE, EXECUTE ON addonservice.* TO '${DB_USERNAME}'@'%';
          FLUSH PRIVILEGES;
          SQL
          echo "addonservice database ready"
{{- end -}}
