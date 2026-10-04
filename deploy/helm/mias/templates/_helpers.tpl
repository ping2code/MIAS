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

{{- /* The OpenTelemetry Collector configuration: the otel-collector-config ConfigMap's only data key, and the input to
the collector Deployment's checksum/config (so only a real configuration change rolls the collector). */ -}}
{{- define "mias.collectorConfig" -}}
extensions:
  health_check:
    endpoint: 0.0.0.0:13133
receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318
processors:
  memory_limiter:
    check_interval: 5s
    limit_percentage: 80
    spike_limit_percentage: 25
  batch: {}
exporters:
  prometheus:
    endpoint: 0.0.0.0:8889
  debug:
    verbosity: {{ .Values.observability.collector.traceDebugVerbosity }}
    sampling_initial: 2
    sampling_thereafter: 500
service:
  extensions:
    - health_check
  telemetry:
    logs:
      level: info
      encoding: json
    metrics:
      level: none
  pipelines:
    traces:
      receivers:
        - otlp
      processors:
        - memory_limiter
        - batch
      exporters:
        - debug
    metrics:
      receivers:
        - otlp
      processors:
        - memory_limiter
        - batch
      exporters:
        - prometheus
{{- end -}}

{{- define "mias.syntheticActive" -}}
{{- if and .Values.syntheticMonitoring.enabled .Values.ui.enabled .Values.ui.route.enabled -}}true{{- end -}}
{{- end -}}

{{- define "mias.blackboxImage" -}}
{{- if not (regexMatch "^sha256:[0-9a-f]{64}$" .Values.syntheticMonitoring.image.digest) -}}
{{- fail "syntheticMonitoring.image.digest must be sha256:<64 hex>" -}}
{{- end -}}
{{ .Values.syntheticMonitoring.image.repository }}@{{ .Values.syntheticMonitoring.image.digest }}
{{- end -}}

{{- define "mias.syntheticTarget" -}}
https://{{ .Values.ui.route.host }}/healthz
{{- end -}}

{{- /* The blackbox exporter configuration (its ConfigMap's only data key and the input to its checksum). */ -}}
{{- define "mias.blackboxConfig" -}}
modules:
  http_2xx_mias:
    prober: http
    timeout: {{ .Values.syntheticMonitoring.probeTimeout }}
    http:
      method: GET
      valid_status_codes:
        - 200
      valid_http_versions:
        - HTTP/1.1
        - HTTP/2.0
      preferred_ip_protocol: ip4
      ip_protocol_fallback: false
      follow_redirects: false
      fail_if_not_ssl: true
      fail_if_body_not_matches_regexp:
        - "^ok"
      tls_config:
        insecure_skip_verify: {{ .Values.syntheticMonitoring.tls.insecureSkipVerify }}
{{- end -}}
