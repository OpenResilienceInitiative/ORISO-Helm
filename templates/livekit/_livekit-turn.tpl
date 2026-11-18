{{/*
Embedded LiveKit TURN server (ORISO-Frontend#1828). Renders nothing while
livekit.turn.enabled is false, so existing environments keep their config.
*/}}
{{- define "oriso.livekitTurnEnabled" -}}
{{- $turn := .Values.livekit.turn | default dict -}}
{{- if not (kindIs "invalid" $turn.enabled) -}}
{{- if not (kindIs "bool" $turn.enabled) -}}
{{- fail "livekit.turn.enabled must be a boolean" -}}
{{- end -}}
{{- end -}}
{{- if $turn.enabled -}}
{{- if eq (include "oriso.matrixrtcLifecycle.enabled" .) "true" -}}
{{- fail "livekit.turn is rendered into the chart's LiveKit config; with matrixrtcLifecycle enabled the config comes from the runtime Secret, so configure TURN there and expose its ports" -}}
{{- end -}}
true
{{- end -}}
{{- end -}}

{{- define "oriso.livekitTurnCertSecret" -}}
{{- if eq (include "oriso.livekitTurnEnabled" .) "true" -}}
{{- $turn := .Values.livekit.turn -}}
{{- if not (kindIs "invalid" $turn.externalTls) -}}
{{- if not (kindIs "bool" $turn.externalTls) -}}
{{- fail "livekit.turn.externalTls must be a boolean" -}}
{{- end -}}
{{- end -}}
{{- if and (gt (int $turn.tlsPort) 0) (not $turn.externalTls) -}}
{{- required "livekit.turn.tlsSecretName is required for TURN/TLS unless livekit.turn.externalTls is true or livekit.turn.tlsPort is 0" $turn.tlsSecretName -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "oriso.livekitTurnConfig" -}}
{{- if eq (include "oriso.livekitTurnEnabled" .) "true" -}}
{{- $turn := .Values.livekit.turn -}}
{{- $tlsPort := int $turn.tlsPort -}}
{{- $udpPort := int $turn.udpPort -}}
{{- if and (eq $tlsPort 0) (eq $udpPort 0) -}}
{{- fail "livekit.turn needs tlsPort or udpPort above 0" -}}
{{- end -}}
{{- if or (lt $tlsPort 0) (gt $tlsPort 65535) (lt $udpPort 0) (gt $udpPort 65535) -}}
{{- fail "livekit.turn ports must be between 0 and 65535" -}}
{{- end -}}
{{- $cert := include "oriso.livekitTurnCertSecret" . }}
turn:
  enabled: true
  domain: {{ required "livekit.turn.domain is required when livekit.turn.enabled is true" $turn.domain | quote }}
  {{- if gt $tlsPort 0 }}
  tls_port: {{ $tlsPort }}
  {{- end }}
  {{- if gt $udpPort 0 }}
  udp_port: {{ $udpPort }}
  {{- end }}
  external_tls: {{ $turn.externalTls | default false }}
  {{- if $cert }}
  cert_file: /etc/livekit-turn/tls.crt
  key_file: /etc/livekit-turn/tls.key
  {{- end }}
{{- end -}}
{{- end -}}
