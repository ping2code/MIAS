{{- define "mias.image" -}}
{{- if not (regexMatch "^sha256:[0-9a-f]{64}$" .Values.image.digest) -}}
{{- fail "image.digest must be sha256:<64 hex> (digest pinning is mandatory)" -}}
{{- end -}}
{{ .Values.image.repository }}@{{ .Values.image.digest }}
{{- end -}}

{{- define "mias.labels" -}}
app.kubernetes.io/part-of: mias
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}

{{- define "mias.podSecurityContext" -}}
runAsNonRoot: true
{{- if .Values.podSecurity.fsGroupChangePolicy }}
fsGroupChangePolicy: {{ .Values.podSecurity.fsGroupChangePolicy }}
{{- end }}
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "mias.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
runAsNonRoot: true
capabilities:
  drop:
    - ALL
seccompProfile:
  type: RuntimeDefault
{{- end -}}


{{- define "mias.collectorImage" -}}
{{- if not (regexMatch "^sha256:[0-9a-f]{64}$" .Values.observability.collector.image.digest) -}}
{{- fail "observability.collector.image.digest must be sha256:<64 hex>" -}}
{{- end -}}
{{ .Values.observability.collector.image.repository }}@{{ .Values.observability.collector.image.digest }}
{{- end -}}

{{- define "mias.otlpEndpoint" -}}
{{- if .Values.observability.otlp.endpoint -}}
{{ .Values.observability.otlp.endpoint }}
{{- else -}}
http://otel-collector.{{ .Release.Namespace }}.svc:4318
{{- end -}}
{{- end -}}

{{- define "mias.collectorActive" -}}
{{- if and .Values.observability.enabled .Values.observability.collector.enabled -}}true{{- end -}}
{{- end -}}

{{- define "mias.uiImage" -}}
{{- if not (regexMatch "^sha256:[0-9a-f]{64}$" .Values.ui.image.digest) -}}
{{- fail "ui.image.digest must be sha256:<64 hex> when ui.enabled (digest pinning is mandatory)" -}}
{{- end -}}
{{ .Values.ui.image.repository }}@{{ .Values.ui.image.digest }}
{{- end -}}

{{- define "mias.uiUpstream" -}}
{{- $upstream := .Values.ui.api.upstream | default (printf "mias-api.%s.svc.%s:8080" .Release.Namespace .Values.ui.api.clusterDomain) -}}
{{- if not (regexMatch "^[a-z0-9]([a-z0-9.-]*[a-z0-9])?:[0-9]{1,5}$" $upstream) -}}
{{- fail "ui.api.upstream must be host:port (a fully qualified lowercase DNS name; no scheme or path)" -}}
{{- end -}}
{{ $upstream }}
{{- end -}}
