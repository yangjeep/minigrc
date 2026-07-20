{{- define "minigrc.name" -}}
minigrc
{{- end -}}

{{- define "minigrc.fullname" -}}
{{ .Release.Name }}-minigrc
{{- end -}}

{{- define "minigrc.labels" -}}
app.kubernetes.io/name: {{ include "minigrc.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "minigrc.selectorLabels" -}}
app.kubernetes.io/name: {{ include "minigrc.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "minigrc.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{ .Values.serviceAccount.name | default (include "minigrc.fullname" .) }}
{{- else -}}
{{ .Values.serviceAccount.name | default "default" }}
{{- end -}}
{{- end -}}
