{{/*
Expand the name of the chart.
*/}}
{{- define "azimuth-apps-operator.name" -}}
{{- .Chart.Name | lower | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified name for a chart-level resource.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "azimuth-apps-operator.fullname" -}}
{{- if contains .Chart.Name .Release.Name }}
{{- .Release.Name | lower | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name .Chart.Name | lower | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
Selector labels for a chart-level resource.
*/}}
{{- define "azimuth-apps-operator.selectorLabels" -}}
app.kubernetes.io/name: {{ include "azimuth-apps-operator.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Labels for a chart-level resource.
*/}}
{{- define "azimuth-apps-operator.labels" -}}
helm.sh/chart: {{
  printf "%s-%s" .Chart.Name .Chart.Version |
    replace "+" "_" |
    lower |
    trunc 63 |
    trimSuffix "-" |
    trimSuffix "_" |
    trimSuffix "."
}}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
{{ include "azimuth-apps-operator.selectorLabels" . }}
{{- end }}
