{{/*
Expand the name of the chart.
*/}}
{{- define "paas-app-chart.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "paas-app-chart.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "paas-app-chart.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "paas-app-chart.labels" -}}
helm.sh/chart: {{ include "paas-app-chart.chart" . }}
{{ include "paas-app-chart.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "paas-app-chart.selectorLabels" -}}
app.kubernetes.io/name: {{ include "paas-app-chart.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "paas-app-chart.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "paas-app-chart.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/* ===== Parcial 2: ayudantes para base de datos y TLS ===== */}}
{{- define "paas-app-chart.dbName" -}}
{{ include "paas-app-chart.fullname" . }}-db
{{- end }}

{{- define "paas-app-chart.dbPort" -}}
{{- if eq .Values.database.engine "mysql" }}3306{{ else }}5432{{ end }}
{{- end }}

{{- define "paas-app-chart.dbImage" -}}
{{- if eq .Values.database.engine "mysql" -}}
mysql:{{ .Values.database.version | default "8.4" }}
{{- else -}}
postgres:{{ .Values.database.version | default "16-alpine" }}
{{- end }}
{{- end }}

{{- define "paas-app-chart.dbSecret" -}}
{{ include "paas-app-chart.fullname" . }}-db-credentials
{{- end }}

{{- define "paas-app-chart.dbLabels" -}}
app.kubernetes.io/name: {{ include "paas-app-chart.name" . }}-db
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: database
{{- end }}

{{- define "paas-app-chart.tlsSecret" -}}
{{- .Values.tls.secretName | default (printf "%s-tls" .Release.Name) }}
{{- end }}

{{- define "paas-app-chart.dbAdminHost" -}}
{{- .Values.dbAdmin.host | default (printf "db.%s" .Values.domain) }}
{{- end }}
