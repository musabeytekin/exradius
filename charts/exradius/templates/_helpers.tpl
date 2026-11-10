{{/* Labels shared by every object of the release. */}}
{{- define "exradius.labels" -}}
app.kubernetes.io/name: exradius
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/* Selector labels of one component: (list . "api") */}}
{{- define "exradius.selector" -}}
{{- $root := index . 0 -}}
app.kubernetes.io/name: exradius
app.kubernetes.io/instance: {{ $root.Release.Name }}
app.kubernetes.io/component: {{ index . 1 }}
{{- end -}}

{{- define "exradius.componentLabels" -}}
{{ include "exradius.labels" (index . 0) }}
app.kubernetes.io/component: {{ index . 1 }}
{{- end -}}

{{/* An Exradius image: (list . "exradius-backend") -> <registry>/<name>:<tag> */}}
{{- define "exradius.image" -}}
{{- $root := index . 0 -}}
{{- $tag := $root.Values.images.tag | default $root.Chart.AppVersion -}}
{{- if $root.Values.images.registry -}}{{ $root.Values.images.registry }}/{{- end -}}{{ index . 1 }}:{{ $tag }}
{{- end -}}

{{- define "exradius.runtimeImage" -}}
{{- .Values.runtime.claudeCode.image | default (include "exradius.image" (list . "exradius-runtime-claude-code")) -}}
{{- end -}}

{{- define "exradius.builderImage" -}}
{{- .Values.builds.builderImage | default (include "exradius.image" (list . .Values.images.builder)) -}}
{{- end -}}

{{- define "exradius.projectDefaultImage" -}}
{{- .Values.projectDefaultImage | default (include "exradius.image" (list . "exradius-project-default")) -}}
{{- end -}}

{{/* <name>.<namespace>.svc.<cluster domain> */}}
{{- define "exradius.serviceHost" -}}
{{- $root := index . 0 -}}
{{- printf "%s.%s.svc.%s" (index . 1) $root.Release.Namespace $root.Values.clusterDomain -}}
{{- end -}}

{{- define "exradius.internalApiUrl" -}}
http://{{ include "exradius.serviceHost" (list . "exradius-api-internal") }}:8001
{{- end -}}

{{/* The egress proxy's forward and reverse addresses, as run and build pods reach them. */}}
{{- define "exradius.egressProxy" -}}
http://{{ include "exradius.serviceHost" (list . "exradius-egress") }}:3128
{{- end -}}

{{- define "exradius.egressUrl" -}}
http://{{ include "exradius.serviceHost" (list . "exradius-egress") }}:3129
{{- end -}}

{{/* Pod and container security contexts for Pod Security "restricted": (list uid gid) */}}
{{- define "exradius.podSecurity" -}}
runAsNonRoot: true
runAsUser: {{ index . 0 }}
runAsGroup: {{ index . 1 }}
fsGroup: {{ index . 1 }}
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{/* (true|false): readOnlyRootFilesystem */}}
{{- define "exradius.containerSecurity" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: {{ . }}
capabilities:
  drop: [ALL]
{{- end -}}

{{/* The database URL Secret and key. */}}
{{- define "exradius.postgresSecret" -}}
{{- if .Values.postgres.existingSecret -}}
{{ .Values.postgres.existingSecret }}
{{- else if .Values.postgres.inCluster.cloudnativepg -}}
{{ .Values.postgres.inCluster.name }}-app
{{- else -}}
{{ fail "set postgres.existingSecret, or postgres.inCluster.cloudnativepg" }}
{{- end -}}
{{- end -}}

{{- define "exradius.brokerSecret" -}}
{{- if .Values.broker.existingSecret -}}
{{ .Values.broker.existingSecret }}
{{- else if .Values.rabbitmq.inCluster.operator -}}
{{ .Values.rabbitmq.inCluster.name }}-default-user
{{- end -}}
{{- end -}}

{{- define "exradius.brokerUrl" -}}
{{- if .Values.broker.url -}}
{{ .Values.broker.url }}
{{- else if .Values.rabbitmq.inCluster.operator -}}
amqp://{{ include "exradius.serviceHost" (list . .Values.rabbitmq.inCluster.name) }}:5672/
{{- else -}}
{{ fail "set broker.url, or rabbitmq.inCluster.operator" }}
{{- end -}}
{{- end -}}

{{- define "exradius.s3Endpoint" -}}
{{- if .Values.objectStore.s3.endpoint -}}
{{ .Values.objectStore.s3.endpoint }}
{{- else if .Values.objectStore.inCluster.seaweedfs -}}
http://{{ include "exradius.serviceHost" (list . .Values.objectStore.inCluster.name) }}:8333
{{- end -}}
{{- end -}}

{{/* Environment of the api and the worker (backend/exradius/config.py). */}}
{{- define "exradius.backendEnv" -}}
- name: EXRADIUS_DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ include "exradius.postgresSecret" . }}
      key: {{ .Values.postgres.uriKey }}
- name: EXRADIUS_BROKER_URL
  value: {{ include "exradius.brokerUrl" . | quote }}
{{- with include "exradius.brokerSecret" . }}
- name: EXRADIUS_BROKER_USERNAME
  valueFrom:
    secretKeyRef:
      name: {{ . }}
      key: {{ $.Values.broker.usernameKey }}
- name: EXRADIUS_BROKER_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ . }}
      key: {{ $.Values.broker.passwordKey }}
{{- end }}
- name: EXRADIUS_OBJECT_STORE
  value: {{ .Values.objectStore.kind | quote }}
- name: EXRADIUS_BUCKET
  value: {{ .Values.objectStore.bucket | quote }}
{{- if eq .Values.objectStore.kind "s3" }}
{{- with include "exradius.s3Endpoint" . }}
- name: EXRADIUS_S3_ENDPOINT_URL
  value: {{ . | quote }}
{{- end }}
{{- with .Values.objectStore.s3.existingSecret }}
- name: EXRADIUS_S3_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ . }}
      key: {{ $.Values.objectStore.s3.accessKeyKey }}
- name: EXRADIUS_S3_SECRET_KEY
  valueFrom:
    secretKeyRef:
      name: {{ . }}
      key: {{ $.Values.objectStore.s3.secretKeyKey }}
{{- end }}
- name: EXRADIUS_S3_REGION
  value: {{ .Values.objectStore.region | quote }}
{{- else if eq .Values.objectStore.kind "gcs" }}
{{- with .Values.objectStore.gcs.endpoint }}
- name: EXRADIUS_GCS_ENDPOINT
  value: {{ . | quote }}
{{- end }}
{{- else if eq .Values.objectStore.kind "azure" }}
- name: EXRADIUS_AZURE_ACCOUNT
  value: {{ required "objectStore.azure.account names the storage account" .Values.objectStore.azure.account | quote }}
{{- with .Values.objectStore.azure.endpoint }}
- name: EXRADIUS_AZURE_ENDPOINT
  value: {{ . | quote }}
{{- end }}
{{- with .Values.objectStore.azure.existingSecret }}
- name: EXRADIUS_AZURE_ACCOUNT_KEY
  valueFrom:
    secretKeyRef:
      name: {{ . }}
      key: {{ $.Values.objectStore.azure.keyKey }}
{{- end }}
{{- else }}
{{- fail "objectStore.kind is s3, gcs or azure" }}
{{- end }}
- name: EXRADIUS_SECRET_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.secretKey.existingSecret }}
      key: {{ .Values.secretKey.key }}
- name: EXRADIUS_PUBLIC_API_URL
  value: {{ .Values.publicApiUrl | quote }}
- name: EXRADIUS_PUBLIC_BROKER_HOST
  value: {{ .Values.broker.publicHost | quote }}
- name: EXRADIUS_PUBLIC_BROKER_PORT
  value: {{ .Values.broker.publicPort | quote }}
- name: EXRADIUS_CACHE_DIR
  value: /var/cache/exradius
- name: EXRADIUS_EGRESS_URL
  value: {{ include "exradius.egressUrl" . | quote }}
- name: HOME
  value: /tmp
{{- end -}}

{{/* A backend process, under Tilt's restart wrapper in development: (list . "exradius-api") */}}
{{- define "exradius.backendCommand" -}}
{{- $root := index . 0 -}}
{{- if $root.Values.dev.restartWrapper -}}
["/tilt-restart-wrapper", "--watch_file=/opt/exradius/.restart-proc", {{ index . 1 | quote }}]
{{- else -}}
[{{ index . 1 | quote }}]
{{- end -}}
{{- end -}}

{{/* The run identity files: identity_files() in backend/exradius/worker/prepare.py. */}}
{{- define "exradius.passwd" -}}
{{- $uid := int .Values.runs.user.uid -}}
{{- $gid := int .Values.runs.user.gid -}}
{{- if eq $uid 0 -}}
root:x:0:{{ $gid }}:root:/home/runner:/bin/bash
{{ else -}}
root:x:0:0:root:/root:/bin/sh
runner:x:{{ $uid }}:{{ $gid }}::/home/runner:/bin/bash
{{ end -}}
{{- end -}}

{{- define "exradius.group" -}}
{{- $gid := int .Values.runs.user.gid -}}
root:x:0:
{{ if ne $gid 0 -}}
runner:x:{{ $gid }}:
{{ end -}}
{{- end -}}

{{/* NetworkPolicy peers (templates/networkpolicies.yaml): the cluster DNS, and a component of
this release by its selector: (list . "egress") */}}
{{- define "exradius.dnsPeer" -}}
- to:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: {{ .Values.networkPolicy.dns.namespace }}
      podSelector:
        matchLabels:
          {{- toYaml .Values.networkPolicy.dns.podLabels | nindent 10 }}
  ports:
    - {protocol: UDP, port: 53}
    - {protocol: TCP, port: 53}
{{- end -}}
{{- define "exradius.peer" -}}
{{- $root := index . 0 -}}
- namespaceSelector:
    matchLabels:
      kubernetes.io/metadata.name: {{ $root.Release.Namespace }}
  podSelector:
    matchLabels:
      {{- include "exradius.selector" (list $root (index . 1)) | nindent 6 }}
{{- end -}}

{{/* Where the stack's pods run (values stack): a pod spec's nodeSelector and tolerations. */}}
{{- define "exradius.stackPlacement" -}}
{{- with .Values.stack.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.stack.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}
