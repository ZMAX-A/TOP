<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowLeft,
  CircleCheck,
  DataAnalysis,
  Refresh,
  Setting,
  TrendCharts,
  WarningFilled,
} from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { ApiError, api } from '@/api/client'
import type {
  Baseline,
  Environment,
  FailureCluster,
  FlakyCase,
  Project,
  ProjectMember,
  QualityAlertState,
  QualityAnalytics,
  QualityAnalyticsQuery,
  QualityChangeAlertStatus,
  QualityChangeSignal,
  QualityPolicy,
  QualityTrendPoint,
  QualityWebhookConfig,
  QualityWebhookDelivery,
  Target,
} from '@/api/types'
import { auth } from '@/auth'
import { formatDate, formatUtcDate, shortDigest } from '@/presentation'

const route = useRoute()
const router = useRouter()
const projectId = computed(() => String(route.params.projectId))
const loading = ref(true)
const policyBusy = ref(false)
const webhookBusy = ref(false)
const webhookTestBusy = ref(false)
const webhookSilenceBusy = ref(false)
const webhookDeliveryBusy = ref(false)
const webhookStateBusy = ref(false)
const webhookReplayBusyId = ref<string | null>(null)
const acknowledgementBusyMetric = ref<QualityAlertState['metric'] | null>(null)
const currentTimeMs = ref(Date.now())
const project = ref<Project>()
const members = ref<ProjectMember[]>([])
const targets = ref<Target[]>([])
const environmentsByTarget = ref<Record<string, Environment[]>>({})
const baselines = ref<Baseline[]>([])
const policy = ref<QualityPolicy>()
const analytics = ref<QualityAnalytics>()
const webhookConfig = ref<QualityWebhookConfig>()
const webhookDeliveries = ref<QualityWebhookDelivery[]>([])
const webhookStates = ref<QualityAlertState[]>([])
const selectedWindowDays = ref(30)
const policyForm = reactive({
  target_pass_rate_percent: 95,
  window_days: 30,
  alert_warning_drop_percentage_points: 5,
  alert_critical_drop_percentage_points: 10,
})
const filterForm = reactive({ target_id: '', environment_id: '', baseline_id: '' })
const webhookForm = reactive({
  enabled: false,
  endpoint_url: '',
  minimum_alert_status: 'WARNING' as 'WARNING' | 'CRITICAL',
  cooldown_seconds: 3600,
  signing_secret_name: '',
  signing_secret_ref: '',
  clear_signing_secret: false,
})
const silenceForm = reactive({
  silenced_until: null as Date | null,
  reason: '',
})
let analyticsRequestSequence = 0
let silenceClock: number | undefined

const currentMember = computed(() =>
  members.value.find((item) => item.user_id === auth.state.user?.id),
)
const canManage = computed(
  () =>
    auth.state.user?.system_role === 'SYSTEM_ADMIN' ||
    currentMember.value?.role === 'PROJECT_ADMIN',
)
const qualityRate = computed(() => analytics.value?.runs.pass_rate_percent)
const reliabilityRate = computed(() => analytics.value?.runs.execution_reliability_percent)
const caseRate = computed(() => analytics.value?.cases.pass_rate_percent)
const sloLabel = computed(() => {
  if (analytics.value?.slo_status === 'MET') return 'SLO 达标'
  if (analytics.value?.slo_status === 'BREACHED') return 'SLO 未达标'
  return '暂无结论数据'
})
const sloTagType = computed(() => {
  if (analytics.value?.slo_status === 'MET') return 'success'
  if (analytics.value?.slo_status === 'BREACHED') return 'danger'
  return 'info'
})
const comparisonLabel = computed(() => {
  if (analytics.value?.comparison.alert_status === 'CRITICAL') return '严重下降'
  if (analytics.value?.comparison.alert_status === 'WARNING') return '需要关注'
  if (analytics.value?.comparison.alert_status === 'STABLE') return '保持稳定'
  return '暂无可比数据'
})
const qualityAlertsSilenced = computed(() => {
  const until = webhookConfig.value?.silenced_until
  return Boolean(until && new Date(until).getTime() > currentTimeMs.value)
})
const maxDailyRuns = computed(() =>
  Math.max(1, ...(analytics.value?.trend.map((point) => point.total_terminal_runs) ?? [1])),
)
const availableEnvironments = computed(() => {
  if (filterForm.target_id) return environmentsByTarget.value[filterForm.target_id] ?? []
  return Object.values(environmentsByTarget.value).flat()
})
const hasDimensionFilters = computed(() =>
  Boolean(filterForm.target_id || filterForm.environment_id || filterForm.baseline_id),
)
const trendSegments = computed(() => {
  const trend = analytics.value?.trend ?? []
  const segments: string[][] = []
  let current: string[] = []
  trend.forEach((point, index) => {
    if (point.pass_rate_percent === null) {
      if (current.length) segments.push(current)
      current = []
      return
    }
    const x = trend.length === 1 ? 450 : 34 + (index * 832) / (trend.length - 1)
    const y = 190 - point.pass_rate_percent * 1.55
    current.push(`${x.toFixed(1)},${y.toFixed(1)}`)
  })
  if (current.length) segments.push(current)
  return segments
})

function report(error: unknown, fallback: string): void {
  ElMessage.error(error instanceof ApiError ? error.message : fallback)
}

function percent(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : `${value.toFixed(2)}%`
}

function metricLabel(metric: QualityChangeSignal['metric']): string {
  if (metric === 'RUN_PASS_RATE') return 'Run 质量通过率'
  if (metric === 'CASE_PASS_RATE') return 'Case 通过率'
  return '执行可靠性'
}

function signalLabel(signal: QualityChangeSignal): string {
  return metricLabel(signal.metric)
}

function signalTagType(
  status: QualityChangeAlertStatus,
): 'success' | 'warning' | 'danger' | 'info' {
  if (status === 'CRITICAL') return 'danger'
  if (status === 'WARNING') return 'warning'
  if (status === 'STABLE') return 'success'
  return 'info'
}

function deltaPoints(value: number | null): string {
  if (value === null) return '无法比较'
  const sign = value > 0 ? '+' : ''
  return `${sign}${value.toFixed(2)} 个百分点`
}

function shortDay(value: string): string {
  return new Date(value).toISOString().slice(5, 10)
}

function barHeight(point: QualityTrendPoint): string {
  return `${Math.max(point.total_terminal_runs ? 8 : 0, (point.total_terminal_runs / maxDailyRuns.value) * 100)}%`
}

function clusterTagType(
  cluster: FailureCluster,
): 'danger' | 'warning' | 'info' {
  if (cluster.infra_error_occurrences > 0) return 'warning'
  if (cluster.timed_out_occurrences > 0) return 'info'
  return 'danger'
}

function flakyStatusType(item: FlakyCase): 'success' | 'danger' {
  return item.latest_status === 'PASSED' ? 'success' : 'danger'
}

function deliveryTagType(
  status: QualityWebhookDelivery['status'],
): 'success' | 'danger' | 'warning' {
  if (status === 'DELIVERED') return 'success'
  if (status === 'FAILED') return 'danger'
  return 'warning'
}

function deliveryStatusLabel(status: QualityWebhookDelivery['status']): string {
  if (status === 'DELIVERED') return '已送达'
  if (status === 'FAILED') return '失败'
  return '等待投递'
}

function deliveryEventLabel(eventType: string): string {
  if (eventType === 'quality.alert.triggered') return '告警触发'
  if (eventType === 'quality.alert.escalated') return '告警升级'
  if (eventType === 'quality.alert.deescalated') return '告警降级'
  if (eventType === 'quality.alert.recovered') return '质量恢复'
  if (eventType === 'quality.alert.test') return '测试事件'
  return eventType
}

function deliveryHasReplay(deliveryId: string): boolean {
  return webhookDeliveries.value.some((delivery) => delivery.replay_of_id === deliveryId)
}

function shortDeliveryId(deliveryId: string): string {
  return deliveryId.slice(0, 8)
}

function analyticsQuery(windowDays?: number): QualityAnalyticsQuery {
  return {
    windowDays,
    targetId: filterForm.target_id || undefined,
    environmentId: filterForm.environment_id || undefined,
    baselineId: filterForm.baseline_id || undefined,
  }
}

function targetName(targetId: string): string {
  return targets.value.find((item) => item.id === targetId)?.name ?? targetId
}

async function load(windowDays?: number): Promise<void> {
  const requestSequence = ++analyticsRequestSequence
  loading.value = true
  try {
    const loadedAnalytics = await api.qualityAnalytics(
      projectId.value,
      analyticsQuery(windowDays),
    )
    if (requestSequence !== analyticsRequestSequence) return
    analytics.value = loadedAnalytics
    selectedWindowDays.value = loadedAnalytics.window_days
    Object.assign(filterForm, {
      target_id: loadedAnalytics.filters.target_id ?? '',
      environment_id: loadedAnalytics.filters.environment_id ?? '',
      baseline_id: loadedAnalytics.filters.baseline_id ?? '',
    })
  } catch (error) {
    if (requestSequence === analyticsRequestSequence) {
      report(error, '质量分析加载失败')
    }
  } finally {
    if (requestSequence === analyticsRequestSequence) loading.value = false
  }
}

async function initialize(): Promise<void> {
  loading.value = true
  try {
    const [
      projectList,
      memberList,
      loadedPolicy,
      targetList,
      baselineList,
      loadedAnalytics,
      loadedWebhookConfig,
      loadedWebhookDeliveries,
      loadedWebhookStates,
    ] = await Promise.all([
        api.projects(),
        api.projectMembers(projectId.value),
        api.qualityPolicy(projectId.value),
        api.targets(projectId.value),
        api.baselines(projectId.value),
        api.qualityAnalytics(projectId.value),
        api.qualityWebhook(projectId.value),
        api.qualityWebhookDeliveries(projectId.value),
        api.qualityWebhookStates(projectId.value),
      ])
    const environmentLists = await Promise.all(
      targetList.map(async (target) => [
        target.id,
        await api.environments(projectId.value, target.id),
      ] as const),
    )
    project.value = projectList.find((item) => item.id === projectId.value)
    members.value = memberList
    targets.value = targetList
    environmentsByTarget.value = Object.fromEntries(environmentLists)
    baselines.value = baselineList
    policy.value = loadedPolicy
    analytics.value = loadedAnalytics
    webhookConfig.value = loadedWebhookConfig
    webhookDeliveries.value = loadedWebhookDeliveries
    webhookStates.value = loadedWebhookStates
    selectedWindowDays.value = loadedAnalytics.window_days
    Object.assign(policyForm, {
      target_pass_rate_percent: loadedPolicy.target_pass_rate_percent,
      window_days: loadedPolicy.window_days,
      alert_warning_drop_percentage_points:
        loadedPolicy.alert_warning_drop_percentage_points,
      alert_critical_drop_percentage_points:
        loadedPolicy.alert_critical_drop_percentage_points,
    })
    Object.assign(webhookForm, {
      enabled: loadedWebhookConfig.enabled,
      endpoint_url: '',
      minimum_alert_status: loadedWebhookConfig.minimum_alert_status,
      cooldown_seconds: loadedWebhookConfig.cooldown_seconds,
      signing_secret_name: '',
      signing_secret_ref: '',
      clear_signing_secret: false,
    })
  } catch (error) {
    report(error, '质量分析加载失败')
  } finally {
    loading.value = false
  }
}

async function changeWindow(days: number): Promise<void> {
  selectedWindowDays.value = days
  await load(days)
}

async function changeTarget(targetId: string): Promise<void> {
  const environment = availableEnvironments.value.find(
    (item) => item.id === filterForm.environment_id,
  )
  if (filterForm.environment_id && (!environment || environment.target_id !== targetId)) {
    filterForm.environment_id = ''
  }
  await load(selectedWindowDays.value)
}

async function changeDimension(): Promise<void> {
  await load(selectedWindowDays.value)
}

async function resetDimensions(): Promise<void> {
  Object.assign(filterForm, { target_id: '', environment_id: '', baseline_id: '' })
  await load(selectedWindowDays.value)
}

async function savePolicy(): Promise<void> {
  policyBusy.value = true
  try {
    const saved = await api.updateQualityPolicy(projectId.value, {
      target_pass_rate_percent: policyForm.target_pass_rate_percent,
      window_days: policyForm.window_days,
      alert_warning_drop_percentage_points:
        policyForm.alert_warning_drop_percentage_points,
      alert_critical_drop_percentage_points:
        policyForm.alert_critical_drop_percentage_points,
    })
    policy.value = saved
    Object.assign(policyForm, {
      target_pass_rate_percent: saved.target_pass_rate_percent,
      window_days: saved.window_days,
      alert_warning_drop_percentage_points: saved.alert_warning_drop_percentage_points,
      alert_critical_drop_percentage_points: saved.alert_critical_drop_percentage_points,
    })
    selectedWindowDays.value = saved.window_days
    await load(saved.window_days)
    ElMessage.success('质量 SLO 策略已更新')
  } catch (error) {
    report(error, '质量 SLO 策略更新失败')
  } finally {
    policyBusy.value = false
  }
}

async function refreshWebhookDeliveries(): Promise<void> {
  webhookDeliveryBusy.value = true
  webhookStateBusy.value = true
  try {
    const [deliveries, states, config] = await Promise.all([
      api.qualityWebhookDeliveries(projectId.value),
      api.qualityWebhookStates(projectId.value),
      api.qualityWebhook(projectId.value),
    ])
    webhookDeliveries.value = deliveries
    webhookStates.value = states
    webhookConfig.value = config
  } catch (error) {
    report(error, 'Webhook 自动评估状态加载失败')
  } finally {
    webhookDeliveryBusy.value = false
    webhookStateBusy.value = false
  }
}

async function saveWebhook(): Promise<void> {
  const endpoint = webhookForm.endpoint_url.trim()
  const secretName = webhookForm.signing_secret_name.trim()
  const secretRef = webhookForm.signing_secret_ref.trim()
  if (!webhookConfig.value?.endpoint_configured && !endpoint) {
    ElMessage.error('首次配置必须填写 Webhook HTTPS 地址')
    return
  }
  if (Boolean(secretName) !== Boolean(secretRef)) {
    ElMessage.error('签名密钥名称和 secret:// 引用必须同时填写')
    return
  }
  webhookBusy.value = true
  try {
    const payload: Record<string, unknown> = {
      enabled: webhookForm.enabled,
      minimum_alert_status: webhookForm.minimum_alert_status,
      cooldown_seconds: webhookForm.cooldown_seconds,
    }
    if (endpoint) payload.endpoint_url = endpoint
    if (webhookForm.clear_signing_secret) {
      payload.clear_signing_secret = true
    } else if (secretName && secretRef) {
      payload.signing_secret_name = secretName
      payload.signing_secret_ref = secretRef
    }
    const saved = await api.updateQualityWebhook(projectId.value, payload)
    webhookConfig.value = saved
    Object.assign(webhookForm, {
      enabled: saved.enabled,
      endpoint_url: '',
      minimum_alert_status: saved.minimum_alert_status,
      cooldown_seconds: saved.cooldown_seconds,
      signing_secret_name: '',
      signing_secret_ref: '',
      clear_signing_secret: false,
    })
    webhookStates.value = await api.qualityWebhookStates(projectId.value)
    ElMessage.success('质量告警 Webhook 配置已更新')
  } catch (error) {
    report(error, '质量告警 Webhook 配置更新失败')
  } finally {
    webhookBusy.value = false
  }
}

async function setQualityAlertSilence(): Promise<void> {
  const silencedUntil = silenceForm.silenced_until
  const reason = silenceForm.reason.trim()
  if (!silencedUntil || Number.isNaN(silencedUntil.getTime())) {
    ElMessage.error('请选择有效的静默截止时间')
    return
  }
  const duration = silencedUntil.getTime() - Date.now()
  if (duration <= 0) {
    ElMessage.error('静默截止时间必须晚于当前时间')
    return
  }
  if (duration > 30 * 24 * 60 * 60 * 1000) {
    ElMessage.error('单次静默最长为 30 天')
    return
  }
  if (!reason) {
    ElMessage.error('请填写静默原因')
    return
  }
  webhookSilenceBusy.value = true
  try {
    webhookConfig.value = await api.silenceQualityAlerts(projectId.value, {
      silenced_until: silencedUntil.toISOString(),
      reason,
    })
    Object.assign(silenceForm, { silenced_until: null, reason: '' })
    ElMessage.success('质量告警静默已生效，评估仍会继续运行')
  } catch (error) {
    report(error, '质量告警静默设置失败')
  } finally {
    webhookSilenceBusy.value = false
  }
}

async function clearQualityAlertSilence(): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '解除后，下次评估会补发静默期间仍待通知的升级或恢复事件。',
      '解除质量告警静默',
      { type: 'warning', confirmButtonText: '确认解除', cancelButtonText: '返回' },
    )
  } catch {
    return
  }
  webhookSilenceBusy.value = true
  try {
    webhookConfig.value = await api.clearQualityAlertSilence(projectId.value)
    ElMessage.success('质量告警静默已解除')
  } catch (error) {
    report(error, '质量告警静默解除失败')
  } finally {
    webhookSilenceBusy.value = false
  }
}

function replaceWebhookState(updated: QualityAlertState): void {
  webhookStates.value = webhookStates.value.map((state) =>
    state.metric === updated.metric ? updated : state,
  )
}

async function acknowledgeQualityAlert(state: QualityAlertState): Promise<void> {
  let note = ''
  try {
    const result = await ElMessageBox.prompt(
      '确认只记录人工处理状态，不会停止 evaluator 或自动恢复通知。',
      `确认 ${metricLabel(state.metric)}`,
      {
        confirmButtonText: '确认告警',
        cancelButtonText: '返回',
        inputPlaceholder: '填写处理说明（最多 500 字）',
        inputValue: state.acknowledgement_note ?? '',
        inputValidator: (value: string) => {
          const normalized = value.trim()
          if (!normalized) return '处理说明不能为空'
          if (normalized.length > 500) return '处理说明不能超过 500 字'
          return true
        },
      },
    )
    note = result.value.trim()
  } catch {
    return
  }
  acknowledgementBusyMetric.value = state.metric
  try {
    replaceWebhookState(
      await api.acknowledgeQualityAlert(projectId.value, state.metric, note),
    )
    ElMessage.success('质量告警已确认')
  } catch (error) {
    report(error, '质量告警确认失败')
  } finally {
    acknowledgementBusyMetric.value = null
  }
}

async function clearQualityAlertAcknowledgement(state: QualityAlertState): Promise<void> {
  acknowledgementBusyMetric.value = state.metric
  try {
    replaceWebhookState(
      await api.clearQualityAlertAcknowledgement(projectId.value, state.metric),
    )
    ElMessage.success('质量告警确认已取消')
  } catch (error) {
    report(error, '取消质量告警确认失败')
  } finally {
    acknowledgementBusyMetric.value = null
  }
}

async function sendWebhookTest(): Promise<void> {
  webhookTestBusy.value = true
  try {
    const queued = await api.testQualityWebhook(projectId.value)
    webhookDeliveries.value = [
      queued,
      ...webhookDeliveries.value.filter((item) => item.id !== queued.id),
    ].slice(0, 20)
    ElMessage.success('测试事件已进入持久化投递队列')
  } catch (error) {
    report(error, '测试 Webhook 入队失败')
  } finally {
    webhookTestBusy.value = false
  }
}

async function replayQualityWebhookDelivery(deliveryId: string): Promise<void> {
  let reason = ''
  try {
    const result = await ElMessageBox.prompt(
      '系统会使用当前已启用的 Webhook 配置创建新投递，原失败记录保持不变。',
      `重放投递 ${shortDeliveryId(deliveryId)}`,
      {
        confirmButtonText: '确认重放',
        cancelButtonText: '返回',
        inputPlaceholder: '填写重放原因（最多 500 字）',
        inputValidator: (value: string) => {
          const normalized = value.trim()
          if (!normalized) return '重放原因不能为空'
          if (normalized.length > 500) return '重放原因不能超过 500 字'
          return true
        },
      },
    )
    reason = result.value.trim()
  } catch {
    return
  }

  webhookReplayBusyId.value = deliveryId
  try {
    const replay = await api.replayQualityWebhookDelivery(
      projectId.value,
      deliveryId,
      reason,
    )
    webhookDeliveries.value = [
      replay,
      ...webhookDeliveries.value.filter((item) => item.id !== replay.id),
    ].slice(0, 20)
    ElMessage.success('失败投递已重新进入持久化队列')
  } catch (error) {
    report(error, 'Webhook 投递重放失败')
  } finally {
    webhookReplayBusyId.value = null
  }
}

onMounted(() => {
  void initialize()
  silenceClock = window.setInterval(() => {
    currentTimeMs.value = Date.now()
  }, 30_000)
})
onUnmounted(() => {
  if (silenceClock !== undefined) window.clearInterval(silenceClock)
})
</script>

<template>
  <div class="page-container" v-loading="loading">
    <el-button
      text
      class="back-link"
      @click="router.push({ name: 'project', params: { projectId } })"
    >
      <el-icon><ArrowLeft /></el-icon>
      返回项目
    </el-button>

    <header class="page-heading quality-heading">
      <div>
        <div class="project-key">{{ project?.key }}</div>
        <h1>{{ project?.name || '项目' }} · 质量分析</h1>
        <p>区分产品质量与执行可靠性，所有时间窗口和日趋势均按 UTC 统计。</p>
      </div>
      <div class="heading-actions">
        <el-select
          :model-value="selectedWindowDays"
          class="window-select"
          @change="changeWindow"
        >
          <el-option :value="7" label="最近 7 天" />
          <el-option :value="14" label="最近 14 天" />
          <el-option :value="30" label="最近 30 天" />
          <el-option :value="60" label="最近 60 天" />
          <el-option :value="90" label="最近 90 天" />
        </el-select>
        <el-button :icon="Refresh" @click="load(selectedWindowDays)">刷新</el-button>
      </div>
    </header>

    <section class="dimension-filters">
      <div class="dimension-filter-heading">
        <div><strong>质量维度</strong><span>按目标、环境和已发布基线收敛同一组质量事实</span></div>
        <el-button v-if="hasDimensionFilters" text type="primary" @click="resetDimensions">
          清除筛选
        </el-button>
      </div>
      <div class="dimension-filter-controls">
        <el-select
          v-model="filterForm.target_id"
          clearable
          placeholder="全部目标"
          @change="changeTarget"
        >
          <el-option
            v-for="target in targets"
            :key="target.id"
            :label="target.name"
            :value="target.id"
          />
        </el-select>
        <el-select
          v-model="filterForm.environment_id"
          clearable
          placeholder="全部环境"
          @change="changeDimension"
        >
          <el-option
            v-for="environment in availableEnvironments"
            :key="environment.id"
            :label="filterForm.target_id ? environment.name : `${targetName(environment.target_id)} / ${environment.name}`"
            :value="environment.id"
          />
        </el-select>
        <el-select
          v-model="filterForm.baseline_id"
          clearable
          placeholder="全部基线"
          @change="changeDimension"
        >
          <el-option
            v-for="baseline in baselines"
            :key="baseline.baseline_id"
            :label="baseline.version"
            :value="baseline.baseline_id"
          />
        </el-select>
      </div>
    </section>

    <el-alert
      v-if="analytics"
      :type="analytics.slo_status === 'BREACHED' ? 'error' : analytics.slo_status === 'MET' ? 'success' : 'info'"
      :closable="false"
      show-icon
      class="slo-alert"
    >
      <template #title>
        {{ sloLabel }} · 目标通过率 {{ analytics.target_pass_rate_percent }}%
      </template>
      质量通过率仅统计 PASSED/FAILED；超时和基础设施错误计入执行可靠性，不污染产品质量结论。
    </el-alert>

    <section v-if="analytics" class="metric-grid">
      <article class="metric-card" :class="{ danger: analytics.slo_status === 'BREACHED' }">
        <el-icon><CircleCheck /></el-icon>
        <div><strong>{{ percent(qualityRate) }}</strong><span>Run 质量通过率</span></div>
        <small>{{ analytics.runs.passed_runs }} 通过 / {{ analytics.runs.conclusive_runs }} 有结论</small>
      </article>
      <article class="metric-card">
        <el-icon><TrendCharts /></el-icon>
        <div><strong>{{ percent(caseRate) }}</strong><span>Case 通过率</span></div>
        <small>{{ analytics.cases.passed_cases }} 通过 / {{ analytics.cases.conclusive_cases }} 有结论</small>
      </article>
      <article class="metric-card" :class="{ danger: (reliabilityRate ?? 100) < 95 }">
        <el-icon><DataAnalysis /></el-icon>
        <div><strong>{{ percent(reliabilityRate) }}</strong><span>执行可靠性</span></div>
        <small>{{ analytics.runs.timed_out_runs }} 超时 · {{ analytics.runs.infra_error_runs }} 基础设施错误</small>
      </article>
      <article class="metric-card">
        <el-icon><WarningFilled /></el-icon>
        <div><strong>{{ analytics.failure_clusters.length }}</strong><span>失败聚类</span></div>
        <small>{{ analytics.runs.failed_runs }} 个失败 Run · {{ analytics.cases.failed_cases }} 个失败 Case</small>
      </article>
    </section>

    <section v-if="analytics" class="surface comparison-surface">
      <div class="section-heading">
        <div>
          <strong>相邻 UTC 窗口对比</strong>
          <span>
            上一窗口 {{ formatUtcDate(analytics.comparison.previous_window_started_at) }} —
            {{ formatUtcDate(analytics.comparison.previous_window_ended_at) }}
          </span>
        </div>
        <el-tag :type="signalTagType(analytics.comparison.alert_status)">
          {{ comparisonLabel }}
        </el-tag>
      </div>
      <div class="comparison-grid">
        <article
          v-for="signal in analytics.comparison.signals"
          :key="signal.metric"
          class="comparison-card"
          :class="signal.alert_status.toLowerCase()"
        >
          <div>
            <strong>{{ signalLabel(signal) }}</strong>
            <el-tag size="small" :type="signalTagType(signal.alert_status)">
              {{ signal.alert_status }}
            </el-tag>
          </div>
          <p>
            {{ percent(signal.previous_percent) }} <span>→</span>
            {{ percent(signal.current_percent) }}
          </p>
          <small>{{ deltaPoints(signal.delta_percentage_points) }}</small>
        </article>
      </div>
      <p class="comparison-note">
        下降达到 {{ analytics.comparison.warning_drop_percentage_points }} 个百分点提示警告，达到
        {{ analytics.comparison.critical_drop_percentage_points }} 个百分点提示严重；筛选条件在两个窗口完全一致。
      </p>
    </section>

    <section v-if="analytics" class="surface trend-surface">
      <div class="section-heading">
        <div>
          <strong>UTC 日质量趋势</strong>
          <span>{{ formatUtcDate(analytics.window_started_at) }} — {{ formatUtcDate(analytics.window_ended_at) }}</span>
        </div>
        <el-tag :type="sloTagType">{{ sloLabel }}</el-tag>
      </div>
      <div class="trend-chart">
        <svg viewBox="0 0 900 220" preserveAspectRatio="none" aria-label="每日 Run 通过率趋势">
          <line v-for="level in [0, 25, 50, 75, 100]" :key="level" x1="34" x2="866" :y1="190 - level * 1.55" :y2="190 - level * 1.55" />
          <text v-for="level in [0, 25, 50, 75, 100]" :key="`label-${level}`" x="2" :y="194 - level * 1.55">{{ level }}%</text>
          <line
            x1="34"
            x2="866"
            :y1="190 - analytics.target_pass_rate_percent * 1.55"
            :y2="190 - analytics.target_pass_rate_percent * 1.55"
            class="target-line"
          />
          <polyline
            v-for="(segment, index) in trendSegments"
            :key="index"
            :points="segment.join(' ')"
            class="pass-line"
          />
        </svg>
      </div>
      <div class="daily-bars">
        <div
          v-for="point in analytics.trend"
          :key="point.bucket_started_at"
          class="daily-column"
          :title="`${shortDay(point.bucket_started_at)} · ${point.total_terminal_runs} 个终态 Run · 通过率 ${percent(point.pass_rate_percent)}`"
        >
          <div class="bar-track"><span :style="{ height: barHeight(point) }" /></div>
          <b>{{ point.total_terminal_runs || '·' }}</b>
          <small>{{ shortDay(point.bucket_started_at) }}</small>
        </div>
      </div>
      <div class="legend">
        <span><i class="line-dot" />有结论 Run 通过率</span>
        <span><i class="target-dot" />质量 SLO 目标</span>
        <span><i class="bar-dot" />终态 Run 数</span>
      </div>
    </section>

    <section v-if="analytics" class="surface flaky-surface">
      <div class="section-heading">
        <div>
          <strong>Flaky Case</strong>
          <span>
            至少 {{ analytics.flaky.minimum_conclusive_executions }} 次有结论执行，且 PASSED/FAILED
            切换不少于 {{ analytics.flaky.minimum_status_transitions }} 次
          </span>
        </div>
        <div class="flaky-heading-tags">
          <el-tag v-if="analytics.flaky.data_truncated" type="warning">样本已截断</el-tag>
          <el-tag :type="analytics.flaky.detected_cases ? 'warning' : 'success'">
            {{ analytics.flaky.detected_cases }} 个
          </el-tag>
        </div>
      </div>
      <el-table :data="analytics.flaky.cases" empty-text="当前窗口没有符合规则的 Flaky Case">
        <el-table-column label="Case" min-width="220">
          <template #default="scope">
            <div class="flaky-case-title">
              <strong>{{ scope.row.case_code }}</strong>
              <code>{{ shortDigest(scope.row.case_id) }}</code>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="结果分布" min-width="165">
          <template #default="scope">
            <strong>{{ scope.row.passed_executions }}</strong> 通过 ·
            <strong>{{ scope.row.failed_executions }}</strong> 失败
            <div class="flaky-subline">通过率 {{ percent(scope.row.pass_rate_percent) }}</div>
          </template>
        </el-table-column>
        <el-table-column label="状态切换" min-width="155">
          <template #default="scope">
            <strong>{{ scope.row.status_transitions }}</strong> / {{ scope.row.conclusive_executions - 1 }} 次
            <div class="flaky-subline">切换率 {{ percent(scope.row.transition_rate_percent) }}</div>
          </template>
        </el-table-column>
        <el-table-column label="最新结果" width="190">
          <template #default="scope">
            <el-tag :type="flakyStatusType(scope.row as FlakyCase)" effect="light">
              {{ scope.row.latest_status }}
            </el-tag>
            <div class="flaky-latest-at">{{ formatUtcDate(scope.row.latest_completed_at) }}</div>
          </template>
        </el-table-column>
      </el-table>
      <p class="flaky-note">
        已分析 {{ analytics.flaky.analyzed_executions }} 条 PASSED/FAILED Case 结果；一次性的状态变化不会被标记为 Flaky。
      </p>
    </section>

    <div v-if="analytics" class="lower-grid">
      <section class="surface failure-surface">
        <div class="section-heading">
          <div>
            <strong>失败聚类</strong>
            <span>错误消息已归一化和脱敏，按稳定指纹聚合</span>
          </div>
          <el-tag v-if="analytics.failure_data_truncated" type="warning">仅分析最近 5000 条</el-tag>
        </div>
        <el-table :data="analytics.failure_clusters" empty-text="当前窗口没有失败或基础设施错误">
          <el-table-column label="聚类" min-width="330">
            <template #default="scope">
              <div class="cluster-title">
                <el-tag :type="clusterTagType(scope.row as FailureCluster)" effect="light">
                  {{ scope.row.failure_category }}
                </el-tag>
                <code>{{ shortDigest(scope.row.fingerprint) }}</code>
              </div>
              <p class="message-pattern">{{ scope.row.message_pattern }}</p>
              <div class="case-codes">{{ scope.row.case_codes.join(' · ') || '无 Case Code' }}</div>
            </template>
          </el-table-column>
          <el-table-column label="出现" width="90">
            <template #default="scope"><strong>{{ scope.row.occurrences }}</strong> 次</template>
          </el-table-column>
          <el-table-column label="Run" width="85">
            <template #default="scope">{{ scope.row.affected_runs }} 个</template>
          </el-table-column>
          <el-table-column label="最近发生" width="175">
            <template #default="scope">{{ formatDate(scope.row.latest_at) }}</template>
          </el-table-column>
        </el-table>
      </section>

      <section class="surface policy-surface">
        <div class="section-heading">
          <div><strong>质量 SLO 策略</strong><span>项目级滚动窗口与目标通过率</span></div>
          <el-icon><Setting /></el-icon>
        </div>
        <el-form v-if="policy" label-position="top">
          <el-form-item label="目标 Run 通过率">
            <el-input-number
              v-model="policyForm.target_pass_rate_percent"
              :min="1"
              :max="100"
              :disabled="!canManage"
              controls-position="right"
            />
            <span class="unit">%</span>
          </el-form-item>
          <el-form-item label="默认滚动窗口">
            <el-select v-model="policyForm.window_days" :disabled="!canManage">
              <el-option :value="7" label="7 天" />
              <el-option :value="14" label="14 天" />
              <el-option :value="30" label="30 天" />
              <el-option :value="60" label="60 天" />
              <el-option :value="90" label="90 天" />
            </el-select>
          </el-form-item>
          <el-form-item label="下降警告阈值">
            <el-input-number
              v-model="policyForm.alert_warning_drop_percentage_points"
              :min="1"
              :max="99"
              :disabled="!canManage"
              controls-position="right"
            />
            <span class="unit points-unit">百分点</span>
          </el-form-item>
          <el-form-item label="下降严重阈值">
            <el-input-number
              v-model="policyForm.alert_critical_drop_percentage_points"
              :min="2"
              :max="100"
              :disabled="!canManage"
              controls-position="right"
            />
            <span class="unit points-unit">百分点</span>
          </el-form-item>
          <el-button
            v-if="canManage"
            type="primary"
            :loading="policyBusy"
            @click="savePolicy"
          >保存 SLO 策略</el-button>
          <p class="policy-note">最近更新 {{ formatDate(policy.updated_at) }}</p>
        </el-form>
      </section>
    </div>

    <section v-if="webhookConfig" class="surface webhook-surface">
      <div class="section-heading">
        <div>
          <strong>质量告警 Webhook</strong>
          <span>相邻 UTC 窗口自动评估、状态化通知和持久化可靠投递</span>
        </div>
        <div class="webhook-heading-tags">
          <el-tag
            v-if="webhookConfig.silenced_until"
            :type="qualityAlertsSilenced ? 'warning' : 'info'"
          >
            {{ qualityAlertsSilenced ? '静默中' : '静默已到期' }}
          </el-tag>
          <el-tag :type="webhookConfig.enabled ? 'success' : 'info'">
            {{ webhookConfig.enabled ? '已启用' : '未启用' }}
          </el-tag>
        </div>
      </div>
      <div class="webhook-grid">
        <div class="webhook-config-panel">
          <el-alert type="info" :closable="false" show-icon>
            地址只返回脱敏展示；真实签名密钥由 dispatcher 进程的 TESTOPS_SECRET_* 环境变量提供。
          </el-alert>
          <el-form label-position="top" class="webhook-form">
            <el-form-item label="启用投递">
              <el-switch v-model="webhookForm.enabled" :disabled="!canManage" />
            </el-form-item>
            <el-form-item label="Webhook HTTPS 地址">
              <el-input
                v-model="webhookForm.endpoint_url"
                type="password"
                show-password
                :disabled="!canManage"
                :placeholder="webhookConfig.endpoint_display || 'https://hooks.example.com/...'
                "
                autocomplete="new-password"
              />
              <span class="webhook-hint">留空将保留当前地址，仅允许 HTTPS 443。</span>
            </el-form-item>
            <el-form-item label="最低通知等级">
              <el-select v-model="webhookForm.minimum_alert_status" :disabled="!canManage">
                <el-option value="WARNING" label="WARNING 及以上" />
                <el-option value="CRITICAL" label="仅 CRITICAL" />
              </el-select>
            </el-form-item>
            <el-form-item label="重复告警冷却">
              <el-input-number
                v-model="webhookForm.cooldown_seconds"
                :min="60"
                :max="86400"
                :step="60"
                controls-position="right"
                :disabled="!canManage"
              />
              <span class="webhook-hint">恢复后的同等级重复告警在冷却期内不会重新入队，单位为秒。</span>
            </el-form-item>
            <div class="signing-heading">
              <strong>请求签名</strong>
              <el-tag size="small" :type="webhookConfig.signing_configured ? 'success' : 'info'">
                {{ webhookConfig.signing_configured ? '已配置' : '未配置' }}
              </el-tag>
            </div>
            <el-form-item label="环境变量名称后缀">
              <el-input
                v-model="webhookForm.signing_secret_name"
                :disabled="!canManage || webhookForm.clear_signing_secret"
                placeholder="QUALITY_WEBHOOK_PROJECT_KEY"
              />
            </el-form-item>
            <el-form-item label="密钥引用">
              <el-input
                v-model="webhookForm.signing_secret_ref"
                :disabled="!canManage || webhookForm.clear_signing_secret"
                placeholder="secret://quality/project/webhook-signing"
              />
            </el-form-item>
            <el-checkbox
              v-if="webhookConfig.signing_configured"
              v-model="webhookForm.clear_signing_secret"
              :disabled="!canManage"
            >清除现有签名配置</el-checkbox>
            <div class="signing-heading silence-heading">
              <strong>限时静默</strong>
              <el-tag
                size="small"
                :type="qualityAlertsSilenced ? 'warning' : 'info'"
              >
                {{ qualityAlertsSilenced ? '生效中' : '未生效' }}
              </el-tag>
            </div>
            <div v-if="webhookConfig.silenced_until" class="silence-status">
              <strong>
                {{ qualityAlertsSilenced ? '静默至' : '已于' }}
                {{ formatDate(webhookConfig.silenced_until) }}
              </strong>
              <span>
                {{ webhookConfig.silence_reason }} ·
                操作人 {{ webhookConfig.silenced_by_display_name || '未知用户' }}
              </span>
            </div>
            <template v-if="canManage">
              <el-form-item label="新的静默截止时间">
                <el-date-picker
                  v-model="silenceForm.silenced_until"
                  type="datetime"
                  placeholder="选择未来时间"
                  :disabled="!webhookConfig.enabled"
                />
                <span class="webhook-hint">最长 30 天；静默期间状态评估仍会继续。</span>
              </el-form-item>
              <el-form-item label="静默原因">
                <el-input
                  v-model="silenceForm.reason"
                  maxlength="500"
                  show-word-limit
                  :disabled="!webhookConfig.enabled"
                  placeholder="例如：发布维护窗口"
                />
              </el-form-item>
              <div class="webhook-actions silence-actions">
                <el-button
                  :loading="webhookSilenceBusy"
                  :disabled="!webhookConfig.enabled"
                  @click="setQualityAlertSilence"
                >设置静默</el-button>
                <el-button
                  v-if="webhookConfig.silenced_until"
                  type="warning"
                  plain
                  :loading="webhookSilenceBusy"
                  @click="clearQualityAlertSilence"
                >解除静默</el-button>
              </div>
            </template>
            <div v-if="canManage" class="webhook-actions">
              <el-button type="primary" :loading="webhookBusy" @click="saveWebhook">
                保存 Webhook
              </el-button>
              <el-button
                :loading="webhookTestBusy"
                :disabled="!webhookConfig.enabled"
                @click="sendWebhookTest"
              >发送测试事件</el-button>
            </div>
          </el-form>
          <p class="webhook-updated">
            当前目标 {{ webhookConfig.endpoint_display || '未配置' }} ·
            配置更新 {{ webhookConfig.updated_at ? formatDate(webhookConfig.updated_at) : '—' }} ·
            下次评估 {{ webhookConfig.next_evaluation_at ? formatDate(webhookConfig.next_evaluation_at) : '—' }}
          </p>
        </div>

        <div class="webhook-history-panel" v-loading="webhookDeliveryBusy">
          <div class="alert-state-section" v-loading="webhookStateBusy">
            <div class="webhook-history-heading">
              <div><strong>自动告警状态</strong><span>确认仅记录人工处置，不改变自动通知规则</span></div>
            </div>
            <div v-if="webhookStates.length" class="alert-state-grid">
              <article v-for="state in webhookStates" :key="state.metric" class="alert-state-card">
                <div>
                  <strong>{{ metricLabel(state.metric) }}</strong>
                  <el-tag size="small" :type="signalTagType(state.current_status)">
                    {{ state.current_status }}
                  </el-tag>
                </div>
                <p>{{ percent(state.previous_percent) }} → {{ percent(state.current_percent) }}</p>
                <span>{{ deltaPoints(state.delta_percentage_points) }}</span>
                <small>
                  最近评估 {{ formatDate(state.last_evaluated_at) }} ·
                  活跃通知 {{ state.active_notification_status || '无' }}
                </small>
                <small v-if="state.acknowledged_at" class="acknowledgement-note">
                  {{ state.acknowledged_by_display_name || '未知用户' }} 于
                  {{ formatDate(state.acknowledged_at) }} 确认 · {{ state.acknowledgement_note }}
                </small>
                <small v-else>人工确认 无</small>
                <div
                  v-if="canManage && ['WARNING', 'CRITICAL'].includes(state.current_status)"
                  class="alert-state-actions"
                >
                  <el-button
                    v-if="!state.acknowledged_at"
                    text
                    type="primary"
                    :loading="acknowledgementBusyMetric === state.metric"
                    @click="acknowledgeQualityAlert(state)"
                  >确认告警</el-button>
                  <el-button
                    v-else
                    text
                    :loading="acknowledgementBusyMetric === state.metric"
                    @click="clearQualityAlertAcknowledgement(state)"
                  >取消确认</el-button>
                </div>
              </article>
            </div>
            <div v-else class="alert-state-empty">
              启用后由 quality-alerts evaluator 生成项目级状态
            </div>
          </div>
          <div class="webhook-history-heading">
            <div><strong>最近投递</strong><span>响应正文不会保存</span></div>
            <el-button text type="primary" :icon="Refresh" @click="refreshWebhookDeliveries">
              刷新
            </el-button>
          </div>
          <el-table :data="webhookDeliveries" empty-text="暂无 Webhook 投递记录">
            <el-table-column label="状态" width="100">
              <template #default="scope">
                <el-tag size="small" :type="deliveryTagType(scope.row.status)">
                  {{ deliveryStatusLabel(scope.row.status) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="事件" min-width="145">
              <template #default="scope">{{ deliveryEventLabel(scope.row.event_type) }}</template>
            </el-table-column>
            <el-table-column label="来源" width="115">
              <template #default="scope">
                <span v-if="scope.row.replay_of_id" class="delivery-result">
                  重放 {{ shortDeliveryId(scope.row.replay_of_id) }}
                </span>
                <span v-else>原始</span>
              </template>
            </el-table-column>
            <el-table-column label="尝试" width="70" prop="attempts" />
            <el-table-column label="HTTP" width="70">
              <template #default="scope">{{ scope.row.response_status ?? '—' }}</template>
            </el-table-column>
            <el-table-column label="创建时间" width="165">
              <template #default="scope">{{ formatDate(scope.row.created_at) }}</template>
            </el-table-column>
            <el-table-column label="结果" min-width="170">
              <template #default="scope">
                <span class="delivery-result">
                  <template v-if="scope.row.replay_reason">
                    {{ scope.row.replayed_by_display_name || '未知用户' }} 重放：
                    {{ scope.row.replay_reason }} ·
                  </template>
                  {{ scope.row.last_error || (scope.row.delivered_at ? `送达 ${formatDate(scope.row.delivered_at)}` : '等待 dispatcher') }}
                </span>
              </template>
            </el-table-column>
            <el-table-column v-if="canManage" label="操作" width="90" fixed="right">
              <template #default="scope">
                <el-button
                  v-if="scope.row.status === 'FAILED' && !deliveryHasReplay(scope.row.id)"
                  text
                  type="danger"
                  :loading="webhookReplayBusyId === scope.row.id"
                  @click="replayQualityWebhookDelivery(scope.row.id)"
                >重放</el-button>
                <span v-else-if="scope.row.status === 'FAILED'" class="delivery-result">已重放</span>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.back-link { margin: -8px 0 10px -12px; color: #66788a; }
.quality-heading { align-items: center; }
.project-key { color: #168579; font: 700 11px "SFMono-Regular", Consolas, monospace; letter-spacing: .08em; text-transform: uppercase; }
.heading-actions { display: flex; gap: 8px; }
.window-select { width: 138px; }
.dimension-filters { display: grid; gap: 12px; margin-bottom: 18px; padding: 16px 18px; border: 1px solid #e5eaee; border-radius: 11px; background: #fff; }
.dimension-filter-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.dimension-filter-heading > div { display: grid; gap: 3px; }
.dimension-filter-heading strong { color: #17324d; font-size: 14px; }
.dimension-filter-heading span { color: #8190a0; font-size: 12px; }
.dimension-filter-controls { display: grid; grid-template-columns: repeat(3, minmax(160px, 1fr)); gap: 12px; }
.slo-alert { margin-bottom: 18px; }
.metric-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 18px; }
.metric-card { display: grid; grid-template-columns: 42px 1fr; gap: 5px 12px; align-items: center; padding: 18px; border: 1px solid #e5eaee; border-radius: 11px; background: #fff; }
.metric-card .el-icon { grid-row: 1 / span 2; width: 42px; height: 42px; border-radius: 10px; color: #167d73; background: #e9f6f3; font-size: 20px; }
.metric-card strong { color: #17324d; font-size: 23px; }
.metric-card span, .metric-card small { color: #718096; font-size: 12px; }
.metric-card small { grid-column: 2; }
.metric-card.danger .el-icon { color: #b74343; background: #fcecec; }
.comparison-surface, .trend-surface, .flaky-surface, .failure-surface, .policy-surface, .webhook-surface { padding: 20px; }
.comparison-surface, .trend-surface, .flaky-surface { margin-bottom: 18px; }
.comparison-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
.comparison-card { display: grid; gap: 8px; padding: 14px 16px; border: 1px solid #e5eaee; border-radius: 9px; background: #fbfcfd; }
.comparison-card > div { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.comparison-card strong { color: #29465f; font-size: 13px; }
.comparison-card p { margin: 0; color: #17324d; font-size: 20px; font-weight: 700; }
.comparison-card p span { margin: 0 6px; color: #91a0ae; font-weight: 400; }
.comparison-card small, .comparison-note { color: #7f8e9c; font-size: 11px; }
.comparison-card.warning { border-color: #e8c982; background: #fffaf0; }
.comparison-card.critical { border-color: #e5aaaa; background: #fff5f5; }
.comparison-note { margin: 12px 0 0; }
.section-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 16px; }
.section-heading > div { display: grid; gap: 4px; }
.section-heading strong { color: #17324d; font-size: 15px; }
.section-heading span { color: #8190a0; font-size: 12px; }
.trend-chart { height: 230px; }
.trend-chart svg { width: 100%; height: 100%; overflow: visible; }
.trend-chart line { stroke: #e9eef2; stroke-width: 1; }
.trend-chart text { fill: #91a0ae; font-size: 10px; }
.trend-chart .target-line { stroke: #d99b39; stroke-width: 1.5; stroke-dasharray: 6 5; }
.trend-chart .pass-line { fill: none; stroke: #17897c; stroke-width: 3; stroke-linecap: round; stroke-linejoin: round; }
.daily-bars { display: flex; gap: 6px; height: 105px; overflow-x: auto; padding: 0 3px 8px 34px; }
.daily-column { display: grid; flex: 1 0 28px; grid-template-rows: 58px 16px 16px; justify-items: center; gap: 2px; }
.bar-track { display: flex; align-items: flex-end; width: 11px; height: 58px; overflow: hidden; border-radius: 4px; background: #f1f4f6; }
.bar-track span { display: block; width: 100%; border-radius: 4px; background: #9bbfd0; }
.daily-column b { color: #657789; font-size: 10px; }
.daily-column small { color: #98a5b1; font-size: 9px; transform: rotate(-36deg); white-space: nowrap; }
.legend { display: flex; gap: 20px; margin-top: 10px; color: #7b8997; font-size: 11px; }
.legend span { display: flex; align-items: center; gap: 6px; }
.legend i { width: 16px; height: 3px; border-radius: 4px; background: #17897c; }
.legend .target-dot { background: #d99b39; }
.legend .bar-dot { background: #9bbfd0; }
.lower-grid { display: grid; grid-template-columns: minmax(0, 1fr) 300px; gap: 18px; }
.failure-surface { min-width: 0; overflow: hidden; }
.cluster-title { display: flex; align-items: center; gap: 8px; }
.cluster-title code { color: #778899; font-size: 10px; }
.message-pattern { margin: 7px 0 5px; color: #40566b; font: 12px/1.5 "SFMono-Regular", Consolas, monospace; overflow-wrap: anywhere; }
.case-codes { color: #8b99a6; font-size: 11px; }
.flaky-heading-tags { display: flex !important; grid-auto-flow: column; gap: 8px !important; }
.flaky-case-title { display: grid; gap: 5px; }
.flaky-case-title strong { color: #29465f; }
.flaky-case-title code { color: #8493a1; font-size: 10px; }
.flaky-subline, .flaky-latest-at, .flaky-note { color: #8493a1; font-size: 11px; }
.flaky-subline { margin-top: 5px; }
.flaky-latest-at { margin-top: 7px; }
.flaky-note { margin: 12px 0 0; }
.policy-surface :deep(.el-input-number), .policy-surface :deep(.el-select) { width: 100%; }
.policy-surface .unit { position: absolute; right: 40px; color: #7b8997; }
.policy-surface .points-unit { right: 35px; padding-left: 7px; background: #fff; }
.policy-note { margin: 12px 0 0; color: #8b99a6; font-size: 11px; }
.webhook-surface { margin-top: 18px; }
.webhook-heading-tags { display: flex; gap: 8px; }
.webhook-grid { display: grid; grid-template-columns: 360px minmax(0, 1fr); gap: 22px; }
.webhook-config-panel { min-width: 0; }
.webhook-form { margin-top: 16px; }
.webhook-form :deep(.el-select), .webhook-form :deep(.el-input-number), .webhook-form :deep(.el-date-editor) { width: 100%; }
.webhook-hint, .webhook-updated, .delivery-result { color: #7f8e9c; font-size: 11px; overflow-wrap: anywhere; }
.webhook-hint { margin-top: 5px; }
.signing-heading, .webhook-history-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.signing-heading { margin: 4px 0 14px; color: #29465f; font-size: 13px; }
.silence-heading { margin-top: 22px; }
.silence-status { display: grid; gap: 4px; margin: -4px 0 14px; padding: 10px 12px; border-radius: 8px; background: #fff7e6; }
.silence-status strong { color: #8a5a12; font-size: 12px; }
.silence-status span { color: #8b7355; font-size: 11px; overflow-wrap: anywhere; }
.webhook-actions { display: flex; gap: 8px; margin-top: 18px; }
.silence-actions { margin: 0 0 20px; }
.webhook-updated { margin: 14px 0 0; }
.webhook-history-panel { min-width: 0; }
.webhook-history-heading { margin-bottom: 12px; }
.webhook-history-heading > div { display: grid; gap: 3px; }
.webhook-history-heading strong { color: #29465f; font-size: 13px; }
.webhook-history-heading span { color: #8190a0; font-size: 11px; }
.alert-state-section { margin-bottom: 20px; }
.alert-state-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.alert-state-card { display: grid; gap: 6px; padding: 12px; border: 1px solid #e5eaee; border-radius: 9px; background: #fbfcfd; }
.alert-state-card > div { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.alert-state-card strong { color: #29465f; font-size: 12px; }
.alert-state-card p { margin: 0; color: #17324d; font-size: 16px; font-weight: 700; }
.alert-state-card span, .alert-state-card small, .alert-state-empty { color: #8190a0; font-size: 11px; }
.acknowledgement-note { color: #35756e !important; overflow-wrap: anywhere; }
.alert-state-actions { justify-content: flex-start !important; min-height: 24px; }
.alert-state-actions .el-button { margin: 0; padding: 0; }
.alert-state-empty { padding: 24px; border: 1px dashed #dce4ea; border-radius: 9px; text-align: center; }
@media (max-width: 1180px) { .metric-grid { grid-template-columns: repeat(2, 1fr); } .lower-grid { grid-template-columns: 1fr; } }
@media (max-width: 980px) { .webhook-grid { grid-template-columns: 1fr; } .alert-state-grid { grid-template-columns: 1fr; } }
@media (max-width: 760px) { .dimension-filter-controls, .comparison-grid { grid-template-columns: 1fr; } }
</style>
