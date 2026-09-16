{{/* The media gate must not be enabled against legacy host-network runtime configuration. */}}
{{- define "oriso.matrixrtcLifecycle.validate" -}}
{{- $cfg := .Values.matrixrtcLifecycle -}}
{{- if not (kindIs "bool" $cfg.enabled) -}}
{{- fail "matrixrtcLifecycle.enabled must be a boolean" -}}
{{- end -}}
{{- if $cfg.enabled -}}
{{- $_ := required "matrixrtcLifecycle.tokenRevision is required and must change on token rotation" $cfg.tokenRevision -}}
{{- $_ := required "matrixrtcLifecycle.livekit.nodeHostname must identify the node owning nodeIp" $cfg.livekit.nodeHostname -}}
{{- $secret := required "matrixrtcLifecycle.existingSecret.name must name a separately provisioned dedicated secret" $cfg.existingSecret.name -}}
{{- $_ := required "matrixrtcLifecycle.existingSecret.tokenKey is required" $cfg.existingSecret.tokenKey -}}
{{- $config := required "matrixrtcLifecycle.livekit.existingConfigSecret.name must name a separately provisioned lifecycle config secret" $cfg.livekit.existingConfigSecret.name -}}
{{- $_ := required "matrixrtcLifecycle.livekit.existingConfigSecret.key is required" $cfg.livekit.existingConfigSecret.key -}}
{{- if eq $config .Values.livekit.existingConfigSecret.name -}}
{{- fail "matrixrtcLifecycle must use a separate LiveKit config secret, not the legacy host-network config" -}}
{{- end -}}
{{- if or (eq $secret .Values.matrixrtcAuth.existingSecret.name) (eq $secret $config) (eq $secret .Values.livekit.existingConfigSecret.name) -}}
{{- fail "matrixrtcLifecycle requires a dedicated shared-secret resource, separate from JWT/config credentials" -}}
{{- end -}}
{{- $ip := required "matrixrtcLifecycle.livekit.nodeIp must be the operator-verified public IPv4 address" $cfg.livekit.nodeIp -}}
{{- if not (regexMatch "^([0-9]{1,3}\\.){3}[0-9]{1,3}$" $ip) -}}
{{- fail "matrixrtcLifecycle.livekit.nodeIp must be an IPv4 address" -}}
{{- end -}}
{{- range (splitList "." $ip) -}}
{{- if or (gt (int .) 255) (and (gt (len .) 1) (hasPrefix "0" .)) -}}
{{- fail "matrixrtcLifecycle.livekit.nodeIp has an invalid IPv4 octet" -}}
{{- end -}}
{{- end -}}
{{- $octets := splitList "." $ip -}}
{{- $first := int (index $octets 0) -}}
{{- $second := int (index $octets 1) -}}
{{- if or (eq $first 0) (eq $first 10) (eq $first 127) (ge $first 224) (and (eq $first 169) (eq $second 254)) (and (eq $first 172) (ge $second 16) (le $second 31)) (and (eq $first 192) (eq $second 168)) -}}
{{- fail "matrixrtcLifecycle.livekit.nodeIp must be public, not a private, loopback or multicast address" -}}
{{- end -}}
{{- if ne (int .Values.livekit.replicas) 1 -}}
{{- fail "matrixrtcLifecycle currently requires livekit.replicas=1 for its single operator-verified nodeIp" -}}
{{- end -}}
{{- end -}}
{{- end -}}
