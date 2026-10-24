{{/*
SPDX-License-Identifier: Apache-2.0
C5 机房版 · djb-node 公共模板片段（名字 / 标签 / 镜像 / ServiceAccount）。

只做字符串拼接，不读凭据、不渲染密钥。真正的安全断言在 values.yaml +
templates/deployment.yaml（securityContext / 无入站端口 / 无 hostNetwork）。
*/}}

{{/* Chart 名（可被 nameOverride 覆盖），DNM 只做截断 */}}
{{- define "djb-node.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* 全名：release-name + chart-name（release 名里已含 chart 名时不重复） */}}
{{- define "djb-node.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* helm.sh/chart 标签值 */}}
{{- define "djb-node.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* 选择器标签：一旦上线不可再改（会破坏 Deployment 的不可变 selector） */}}
{{- define "djb-node.selectorLabels" -}}
app.kubernetes.io/name: {{ include "djb-node.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* 完整标签（含版本 / 管理方 / 组件归属），用于展示与检索，不进 selector */}}
{{- define "djb-node.labels" -}}
helm.sh/chart: {{ include "djb-node.chart" . }}
{{ include "djb-node.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: djb-node
app.kubernetes.io/component: node-client
{{- end -}}

{{/* ServiceAccount 名 */}}
{{- define "djb-node.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "djb-node.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* 镜像引用：<repo>:<tag|appVersion>，永不使用 latest（tag 为空时回落 appVersion） */}}
{{- define "djb-node.image" -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) -}}
{{- end -}}
