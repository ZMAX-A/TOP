<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowLeft,
  CircleClose,
  Connection,
  Download,
  Files,
  MagicStick,
  Refresh,
  RefreshRight,
  Timer,
  VideoCamera,
  WarningFilled,
} from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'

import { ApiError, api, streamRunEvents } from '@/api/client'
import type {
  AiFailureCategory,
  AiRunAnalysis,
  Artifact,
  ProjectMember,
  RunDetail,
  RunEvent,
  RunLiveViewStatus,
  RunStatus,
} from '@/api/types'
import { auth } from '@/auth'
import { createClientRequestId } from '@/idempotency'
import { formatDate, formatUtcDate, runTagType, shortDigest } from '@/presentation'

const TERMINAL_STATUSES = new Set<RunStatus>([
  'PASSED',
  'FAILED',
  'CANCELED',
  'TIMED_OUT',
  'INFRA_ERROR',
])
const AI_ANALYZABLE_STATUSES = new Set<RunStatus>([
  'FAILED',
  'CANCELED',
  'TIMED_OUT',
  'INFRA_ERROR',
])
const AI_CATEGORY_LABELS: Record<AiFailureCategory, string> = {
  PRODUCT_DEFECT: '产品缺陷',
  AUTOMATION_SCRIPT: '自动化脚本',
  ASSERTION: '断言问题',
  ENVIRONMENT: '环境问题',
  TEST_DATA: '测试数据',
  FLAKY_TIMING: '时序不稳定',
  PLATFORM_RESOURCE: '平台资源',
  UNKNOWN: '证据不足',
}
const DISPATCH_WAIT_LABELS: Record<string, string> = {
  RUNNER_POOL_NOT_FOUND: '绑定的 Runner Pool 不存在',
  RUNNER_POOL_DRAINING: 'Runner Pool 正在排空，暂不接收新任务',
  RUNNER_POOL_DISABLED: 'Runner Pool 已禁用',
  NO_HEALTHY_RUNNER: '正在等待健康 Runner 心跳',
  RUNNER_CAPABILITY_MISMATCH: '当前健康 Runner 的能力与此 Run 不匹配',
  RUNNER_ISOLATION_UNAVAILABLE: '当前健康 Runner 未启用凭据最小化的子进程隔离',
  AUTOMATION_PACKAGE_UNAVAILABLE: '当前健康 Runner 未承载该不可变自动化包',
  RUNNER_POOL_CAPACITY_EXHAUSTED: 'Runner Pool 槽位已满，正在等待释放',
  BROKER_PUBLISH_FAILED: '消息队列投递失败，系统正在自动重试',
}

interface RunProgressSnapshot {
  current_round: number
  total_rounds: number
  round_completed: number
  round_total: number
  completed: number
  total: number
  passed: number
  failed: number
  skipped: number
  current_case_code: string | null
}

const FINISHED_CASE_STATUSES = new Set([
  'PASSED',
  'FAILED',
  'SKIPPED',
  'INFRA_ERROR',
  'TIMED_OUT',
])

const route = useRoute()
const router = useRouter()
const projectId = computed(() => String(route.params.projectId))
const runId = computed(() => String(route.params.runId))
const loading = ref(true)
const run = ref<RunDetail>()
const events = ref<RunEvent[]>([])
const streamConnected = ref(false)
const streamError = ref('')
const downloadingArtifact = ref<string | null>(null)
const actionBusy = ref(false)
const aiAnalysis = ref<AiRunAnalysis>()
const aiAnalysisBusy = ref(false)
const aiAnalysisError = ref('')
const members = ref<ProjectMember[]>([])
const liveViewState = ref<RunLiveViewStatus>()
const liveViewBusy = ref(false)
const liveViewDialogVisible = ref(false)
const liveViewUrl = ref('')
let streamController: AbortController | null = null
let retryTimer: number | null = null
let liveViewPollTimer: number | null = null
let destroyed = false

const failedCases = computed(() =>
  (run.value?.cases ?? []).filter((item) =>
    ['FAILED', 'INFRA_ERROR', 'TIMED_OUT'].includes(item.status),
  ),
)
const lastEventSequence = computed(
  () => events.value[events.value.length - 1]?.sequence ?? 0,
)
const terminal = computed(() =>
  run.value ? TERMINAL_STATUSES.has(run.value.status) : false,
)
const aiAnalyzable = computed(() =>
  run.value ? AI_ANALYZABLE_STATUSES.has(run.value.status) : false,
)
const headedRun = computed(() => run.value?.snapshot.web_config?.headless === false)
const liveViewReady = computed(
  () => headedRun.value && !terminal.value && liveViewState.value?.status === 'READY',
)
const liveViewLabel = computed(() => {
  const status = liveViewState.value?.status
  if (status === 'READY') return '实时观看'
  if (status === 'STARTING') return '画面准备中'
  if (status === 'ENDED') return '观看已结束'
  if (status === 'UNAVAILABLE') return '观看不可用'
  return '实时观看'
})
const dispatchWaitMessage = computed(() => {
  const detail = run.value
  if (!detail || detail.dispatch_state !== 'WAITING' || !detail.dispatch_wait_reason) return ''
  return DISPATCH_WAIT_LABELS[detail.dispatch_wait_reason] || detail.dispatch_wait_reason
})
const currentMember = computed(() =>
  members.value.find((item) => item.user_id === auth.state.user?.id),
)
const canOperate = computed(
  () =>
    auth.state.user?.system_role === 'SYSTEM_ADMIN' ||
    ['PROJECT_ADMIN', 'TESTER'].includes(currentMember.value?.role ?? ''),
)
const visibleEvents = computed(() =>
  events.value.filter((event) => event.event_type !== 'progress_updated'),
)
const testProgress = computed<RunProgressSnapshot>(() => {
  const detail = run.value
  const fallbackTotal = detail?.case_count ?? 0
  const fallbackStatuses = (detail?.cases ?? []).filter((item) =>
    FINISHED_CASE_STATUSES.has(item.status),
  )
  const fallback: RunProgressSnapshot = {
    current_round: 1,
    total_rounds:
      detail?.snapshot.automation_package.name === 'yanjia-web-pytest' ? 3 : 1,
    round_completed: fallbackStatuses.length,
    round_total: fallbackTotal,
    completed: fallbackStatuses.length,
    total: fallbackTotal,
    passed: fallbackStatuses.filter((item) => item.status === 'PASSED').length,
    failed: fallbackStatuses.filter((item) =>
      ['FAILED', 'INFRA_ERROR', 'TIMED_OUT'].includes(item.status),
    ).length,
    skipped: fallbackStatuses.filter((item) => item.status === 'SKIPPED').length,
    current_case_code: null,
  }
  const progressEvent = [...events.value]
    .reverse()
    .find((event) => event.event_type === 'progress_updated')
  if (!progressEvent) return fallback
  const payload = progressEvent.payload
  const keys = [
    'current_round',
    'total_rounds',
    'round_completed',
    'round_total',
    'completed',
    'total',
    'passed',
    'failed',
    'skipped',
  ] as const
  if (keys.some((key) => !Number.isInteger(payload[key]) || Number(payload[key]) < 0)) {
    return fallback
  }
  const progress: RunProgressSnapshot = {
    current_round: Number(payload.current_round),
    total_rounds: Number(payload.total_rounds),
    round_completed: Number(payload.round_completed),
    round_total: Number(payload.round_total),
    completed: Number(payload.completed),
    total: Number(payload.total),
    passed: Number(payload.passed),
    failed: Number(payload.failed),
    skipped: Number(payload.skipped),
    current_case_code:
      typeof payload.current_case_code === 'string' ? payload.current_case_code : null,
  }
  if (terminal.value) progress.round_completed = progress.round_total
  return progress
})
const roundLabel = computed(() => {
  if (testProgress.value.current_round === 1) return '首轮测试'
  if (testProgress.value.current_round === 2) return '第二轮复跑'
  if (testProgress.value.current_round === 3) return '第三轮复跑'
  return `第 ${testProgress.value.current_round} 轮复跑`
})
const roundSteps = computed(() =>
  Array.from(
    { length: Math.max(1, testProgress.value.total_rounds) },
    (_, index) => {
      const round = index + 1
      let state: 'complete' | 'active' | 'error' | 'skipped' | 'pending' = 'pending'
      if (round < testProgress.value.current_round) state = 'complete'
      if (round === testProgress.value.current_round) {
        state = terminal.value
          ? run.value?.status === 'PASSED'
            ? 'complete'
            : 'error'
          : 'active'
      }
      if (terminal.value && round > testProgress.value.current_round) state = 'skipped'
      const label =
        round === 1 ? '首轮测试' : round === 2 ? '第二轮复跑' : round === 3 ? '第三轮复跑' : `第 ${round} 轮复跑`
      const detail =
        state === 'complete'
          ? '已完成'
          : state === 'active'
            ? `${testProgress.value.round_completed} / ${testProgress.value.round_total} 条`
            : state === 'error'
              ? run.value?.status ?? '执行异常'
              : state === 'skipped'
                ? '无需复跑'
                : '等待执行'
      return { round, label, detail, state }
    },
  ),
)
const roundProgressPercentage = computed(() => {
  const progress = testProgress.value
  if (terminal.value) return 100
  if (progress.round_total === 0) return 0
  return Math.min(100, Math.round((progress.round_completed / progress.round_total) * 100))
})
const progressBarStatus = computed<'success' | 'exception' | undefined>(() => {
  if (!terminal.value) return undefined
  return run.value?.status === 'PASSED' ? 'success' : 'exception'
})
const progressLabel = computed(() => {
  if (!run.value) return '读取中'
  return `${testProgress.value.completed} / ${testProgress.value.total}`
})

function statusTagType(
  status: string,
): 'success' | 'warning' | 'danger' | 'info' | 'primary' {
  if (status === 'PASSED') return 'success'
  if (['FAILED', 'INFRA_ERROR', 'TIMED_OUT'].includes(status)) return 'danger'
  if (['RUNNING', 'PREPARING'].includes(status)) return 'warning'
  if (status === 'QUEUED') return 'primary'
  return 'info'
}

function aiCategoryTagType(
  category: AiFailureCategory,
): 'success' | 'warning' | 'danger' | 'info' | 'primary' {
  if (category === 'PRODUCT_DEFECT') return 'danger'
  if (['PLATFORM_RESOURCE', 'ENVIRONMENT'].includes(category)) return 'warning'
  if (category === 'UNKNOWN') return 'info'
  return 'primary'
}

function confidencePercent(confidence: number): number {
  return Math.round(confidence * 100)
}

function eventLabel(event: RunEvent): string {
  const labels: Record<string, string> = {
    run_created: '运行已创建',
    status_changed: '运行状态变更',
    run_started: 'Runner 开始执行',
    case_started: '用例开始',
    case_finished: '用例结束',
    progress_updated: '测试进度更新',
    run_finished: 'Runner 执行结束',
    result_recorded: '不可变结果已入库',
    cancel_requested: '已请求取消',
    live_view_ready: '只读实时画面已就绪',
    live_view_ended: '只读实时画面已结束',
  }
  return labels[event.event_type] ?? event.event_type
}

function formatDuration(milliseconds: number | null): string {
  if (milliseconds === null) return '—'
  if (milliseconds < 1000) return `${milliseconds} ms`
  return `${(milliseconds / 1000).toFixed(2)} s`
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function mergeEvent(event: RunEvent): void {
  if (events.value.some((item) => item.sequence === event.sequence)) return
  events.value = [...events.value, event].sort((left, right) => left.sequence - right.sequence)
  if (
    ['status_changed', 'run_finished', 'result_recorded', 'live_view_ready', 'live_view_ended'].includes(
      event.event_type,
    )
  ) {
    void refreshDetail()
  }
}

function clearLiveViewPoll(): void {
  if (liveViewPollTimer !== null) {
    window.clearTimeout(liveViewPollTimer)
    liveViewPollTimer = null
  }
}

function scheduleLiveViewPoll(): void {
  clearLiveViewPoll()
  if (destroyed || terminal.value || !headedRun.value) return
  liveViewPollTimer = window.setTimeout(() => {
    liveViewPollTimer = null
    void refreshLiveViewStatus()
  }, 1500)
}

async function refreshLiveViewStatus(): Promise<void> {
  clearLiveViewPoll()
  if (!headedRun.value) {
    liveViewState.value = { status: 'DISABLED', read_only: true }
    return
  }
  try {
    liveViewState.value = await api.runLiveViewStatus(runId.value)
  } catch {
    liveViewState.value = { status: 'UNAVAILABLE', read_only: true }
  }
  if (liveViewState.value.status === 'STARTING') scheduleLiveViewPoll()
}

async function openLiveView(): Promise<void> {
  if (!liveViewReady.value) return
  liveViewBusy.value = true
  try {
    const access = await api.createRunLiveViewTicket(runId.value)
    const fragment = new URLSearchParams({
      ticket: access.ticket,
      password: access.password,
    })
    liveViewUrl.value = `${access.viewer_path}#${fragment.toString()}`
    liveViewDialogVisible.value = true
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : '实时观看授权失败')
    await refreshLiveViewStatus()
  } finally {
    liveViewBusy.value = false
  }
}

function closeLiveView(): void {
  liveViewUrl.value = ''
}

async function refreshDetail(): Promise<void> {
  try {
    run.value = await api.run(runId.value)
    await refreshLiveViewStatus()
  } catch (error) {
    if (!destroyed) {
      ElMessage.error(error instanceof ApiError ? error.message : 'Run 详情刷新失败')
    }
  }
}

function scheduleReconnect(): void {
  if (destroyed || terminal.value || retryTimer !== null) return
  retryTimer = window.setTimeout(() => {
    retryTimer = null
    void refreshDetail().then(startEventStream)
  }, 2000)
}

async function startEventStream(): Promise<void> {
  streamController?.abort()
  if (destroyed || terminal.value) {
    streamConnected.value = false
    return
  }
  const controller = new AbortController()
  streamController = controller
  streamError.value = ''
  streamConnected.value = true
  try {
    await streamRunEvents(
      runId.value,
      lastEventSequence.value,
      mergeEvent,
      controller.signal,
    )
    await refreshDetail()
    if (!terminal.value) scheduleReconnect()
  } catch (error) {
    if (!controller.signal.aborted && !destroyed) {
      streamError.value = error instanceof ApiError ? error.message : '实时事件连接已中断'
      scheduleReconnect()
    }
  } finally {
    if (streamController === controller) {
      streamConnected.value = false
      streamController = null
    }
  }
}

async function load(): Promise<void> {
  streamController?.abort()
  if (retryTimer !== null) {
    window.clearTimeout(retryTimer)
    retryTimer = null
  }
  loading.value = true
  try {
    const [detail, timeline, memberList] = await Promise.all([
      api.run(runId.value),
      api.runEvents(runId.value),
      api.projectMembers(projectId.value),
    ])
    if (detail.project_id !== projectId.value) {
      throw new ApiError(404, 'Run 不属于当前项目')
    }
    if (aiAnalysis.value?.run_id !== detail.id) {
      aiAnalysis.value = undefined
      aiAnalysisError.value = ''
    }
    run.value = detail
    events.value = timeline.sort((left, right) => left.sequence - right.sequence)
    members.value = memberList
    await refreshLiveViewStatus()
    void startEventStream()
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : 'Run 详情加载失败')
  } finally {
    loading.value = false
  }
}

async function analyzeFailure(): Promise<void> {
  if (!run.value || !aiAnalyzable.value) return
  aiAnalysisBusy.value = true
  aiAnalysisError.value = ''
  try {
    aiAnalysis.value = await api.analyzeRun(run.value.id)
    ElMessage.success('AI 失败分析已生成')
  } catch (error) {
    aiAnalysisError.value = error instanceof ApiError ? error.message : 'AI 失败分析生成失败'
  } finally {
    aiAnalysisBusy.value = false
  }
}

async function cancelCurrentRun(): Promise<void> {
  if (!run.value) return
  actionBusy.value = true
  try {
    await api.cancelRun(run.value.id)
    ElMessage.success('取消请求已提交')
    await load()
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : '取消运行失败')
  } finally {
    actionBusy.value = false
  }
}

async function rerunCurrent(mode: 'FULL' | 'FAILED_ONLY'): Promise<void> {
  if (!run.value) return
  actionBusy.value = true
  try {
    const created = await api.rerunRun(run.value.id, mode, createClientRequestId())
    ElMessage.success(mode === 'FULL' ? '完整重跑已创建' : '仅异常用例重跑已创建')
    await router.push({
      name: 'run-detail',
      params: { projectId: projectId.value, runId: created.id },
    })
    await load()
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : '重跑创建失败')
  } finally {
    actionBusy.value = false
  }
}

async function openSourceRun(): Promise<void> {
  const sourceRunId = run.value?.source_run_id
  if (!sourceRunId) return
  await router.push({
    name: 'run-detail',
    params: { projectId: projectId.value, runId: sourceRunId },
  })
  await load()
}

async function downloadArtifact(row: unknown): Promise<void> {
  const artifact = row as Artifact
  downloadingArtifact.value = artifact.artifact_id
  try {
    const access = await api.artifactAccess(runId.value, artifact.artifact_id)
    const link = document.createElement('a')
    link.href = access.url
    link.target = '_blank'
    link.rel = 'noopener noreferrer'
    link.download = access.name
    link.click()
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : '制品下载授权失败')
  } finally {
    downloadingArtifact.value = null
  }
}

onMounted(load)
onBeforeUnmount(() => {
  destroyed = true
  streamController?.abort()
  if (retryTimer !== null) window.clearTimeout(retryTimer)
  clearLiveViewPoll()
  closeLiveView()
})
</script>

<template>
  <div v-loading="loading || actionBusy" class="page-container run-page">
    <div class="page-sticky-header">
      <el-button
        text
        class="back-link"
        @click="router.push({ name: 'project-runs', params: { projectId } })"
      >
        <el-icon><ArrowLeft /></el-icon>
        返回运行记录
      </el-button>

      <header class="page-heading run-heading">
      <div>
        <div class="run-kicker">Immutable execution</div>
        <h1>Run 执行详情</h1>
        <p class="mono">{{ runId }}</p>
      </div>
      <div class="heading-actions">
        <span v-if="!terminal" class="live-state" :class="{ offline: !streamConnected }">
          <i />{{ streamConnected ? '实时事件已连接' : '正在重连' }}
        </span>
        <el-button
          v-if="headedRun"
          type="primary"
          plain
          :icon="VideoCamera"
          :loading="liveViewBusy"
          :disabled="!liveViewReady"
          @click="openLiveView"
        >{{ liveViewLabel }}</el-button>
        <el-button
          v-if="aiAnalyzable"
          type="primary"
          :icon="MagicStick"
          :loading="aiAnalysisBusy"
          @click="analyzeFailure"
        >{{ aiAnalysis ? '重新生成 AI 分析' : 'AI 分析失败' }}</el-button>
        <el-button :icon="Refresh" @click="load">刷新</el-button>
        <el-button
          v-if="canOperate && !terminal"
          type="danger"
          plain
          :icon="CircleClose"
          @click="cancelCurrentRun"
        >取消运行</el-button>
        <el-button
          v-if="canOperate && terminal"
          :icon="RefreshRight"
          @click="rerunCurrent('FULL')"
        >完整重跑</el-button>
        <el-button
          v-if="canOperate && terminal && failedCases.length"
          type="danger"
          plain
          @click="rerunCurrent('FAILED_ONLY')"
        >仅重跑异常</el-button>
      </div>
      </header>
    </div>

    <el-alert
      v-if="run?.source_run_id"
      :title="run.retry_mode === 'FAILED_ONLY' ? '此 Run 为仅异常用例重跑' : '此 Run 为完整重跑'"
      type="info"
      :closable="false"
      show-icon
      class="lineage-alert"
    >
      <template #default>
        来源 Run：
        <span
          class="table-link mono"
          @click="openSourceRun"
        >{{ run.source_run_id }}</span>
      </template>
    </el-alert>

    <el-alert
      v-if="dispatchWaitMessage"
      title="Run 正在等待执行容量"
      :description="dispatchWaitMessage"
      type="warning"
      show-icon
      :closable="false"
      class="stream-alert"
    />
    <el-alert
      v-if="run?.regression_schedule_id"
      title="此 Run 由回归计划创建"
      :description="`计划 ${run.regression_schedule_id.slice(0, 8)} · 计划时刻 ${formatUtcDate(run.scheduled_for)}`"
      type="info"
      :closable="false"
      show-icon
      class="schedule-origin-alert"
    />

    <el-alert
      v-if="streamError && !terminal"
      :title="`${streamError}，页面将在后台重连。`"
      type="warning"
      show-icon
      :closable="false"
      class="stream-alert"
    />

    <section
      v-if="run"
      class="surface run-progress-card"
      :class="{ 'progress-failed': terminal && run.status !== 'PASSED' }"
    >
      <div class="progress-heading">
        <div>
          <span class="progress-eyebrow">实时测试进度</span>
          <h2>{{ terminal ? '执行已结束' : roundLabel }}</h2>
        </div>
        <el-tag :type="terminal ? runTagType(run.status) : 'warning'" effect="light">
          第 {{ testProgress.current_round }} / {{ testProgress.total_rounds }} 轮
        </el-tag>
      </div>
      <div class="round-stage-track" :style="{ '--round-count': roundSteps.length }">
        <div
          v-for="step in roundSteps"
          :key="step.round"
          class="round-stage"
          :class="`stage-${step.state}`"
        >
          <span class="round-stage-node">{{ step.round }}</span>
          <div>
            <strong>{{ step.label }}</strong>
            <small>{{ step.detail }}</small>
          </div>
        </div>
      </div>
      <div class="round-progress-label">
        <span>{{ roundLabel }}</span>
        <strong>{{ testProgress.round_completed }} / {{ testProgress.round_total }} 条</strong>
      </div>
      <el-progress
        :percentage="roundProgressPercentage"
        :status="progressBarStatus"
        :stroke-width="14"
        :show-text="false"
      />
      <div class="progress-stat-grid">
        <div>
          <span>当前轮次</span>
          <strong>{{ roundLabel }}</strong>
          <small>最多 {{ testProgress.total_rounds }} 轮</small>
        </div>
        <div>
          <span>已测用例</span>
          <strong>{{ testProgress.completed }} / {{ testProgress.total }}</strong>
          <small>按唯一用例计数</small>
        </div>
        <div class="success-stat">
          <span>当前成功</span>
          <strong>{{ testProgress.passed }}</strong>
          <small>复跑通过会转为成功</small>
        </div>
        <div class="failure-stat">
          <span>当前失败</span>
          <strong>{{ testProgress.failed }}</strong>
          <small>含基础设施异常</small>
        </div>
      </div>
      <div class="progress-footnote">
        <span v-if="testProgress.current_case_code">
          最近完成：<code>{{ testProgress.current_case_code }}</code>
        </span>
        <span v-else>{{ terminal ? '没有可显示的用例进度事件' : '等待首条用例结果…' }}</span>
        <span v-if="testProgress.skipped">跳过 {{ testProgress.skipped }} 条</span>
        <span v-if="!terminal && testProgress.current_round > 1">仅复跑上一轮仍失败的用例</span>
      </div>
    </section>

    <section v-if="run" class="metric-grid run-metrics">
      <article class="metric-card status-card">
        <span class="metric-icon teal"><Connection /></span>
        <div><span>当前状态</span><el-tag :type="runTagType(run.status)">{{ run.status }}</el-tag></div>
        <small>{{ terminal ? '执行已结束' : '实时更新中' }}</small>
      </article>
      <article class="metric-card">
        <span class="metric-icon blue"><Files /></span>
        <div><strong>{{ progressLabel }}</strong><span>完成用例</span></div>
        <small>{{ testProgress.failed }} 条异常</small>
      </article>
      <article class="metric-card">
        <span class="metric-icon amber"><Timer /></span>
        <div><strong>{{ run.result ? formatDuration(run.result.case_results.reduce((sum, item) => sum + item.duration_ms, 0)) : '—' }}</strong><span>累计用例耗时</span></div>
        <small>{{ formatDate(run.started_at) }}</small>
      </article>
      <article class="metric-card">
        <span class="metric-icon amber"><Timer /></span>
        <div><strong>{{ run.timeout_at ? formatUtcDate(run.timeout_at) : '等待执行' }}</strong><span>超时截止（UTC）</span></div>
        <small>上限 {{ run.timeout_seconds }} 秒</small>
      </article>
    </section>

    <el-alert
      v-if="run?.error_message"
      title="执行失败诊断"
      :description="run.error_message"
      type="error"
      show-icon
      :closable="false"
      class="diagnostic-alert"
    />

    <section
      v-if="aiAnalysis || aiAnalysisBusy || aiAnalysisError"
      class="surface ai-analysis-section"
    >
      <div class="ai-analysis-heading">
        <div>
          <span class="ai-kicker"><el-icon><MagicStick /></el-icon> AI 只读分析</span>
          <h2>失败原因研判</h2>
        </div>
        <div v-if="aiAnalysis" class="ai-analysis-tags">
          <el-tag :type="aiCategoryTagType(aiAnalysis.classification)" effect="light">
            {{ AI_CATEGORY_LABELS[aiAnalysis.classification] }}
          </el-tag>
          <el-tag type="info" effect="plain">
            置信度 {{ confidencePercent(aiAnalysis.confidence) }}%
          </el-tag>
        </div>
      </div>

      <el-alert
        v-if="aiAnalysisError"
        title="AI 分析未生成"
        :description="aiAnalysisError"
        type="error"
        show-icon
        :closable="false"
      />
      <el-skeleton v-else-if="aiAnalysisBusy" :rows="5" animated />
      <div v-else-if="aiAnalysis" class="ai-analysis-content">
        <p class="ai-summary">{{ aiAnalysis.summary }}</p>

        <div v-if="aiAnalysis.probable_causes.length" class="ai-cause-list">
          <article
            v-for="(cause, index) in aiAnalysis.probable_causes"
            :key="`${cause.title}-${index}`"
            class="ai-cause-card"
          >
            <div>
              <strong>{{ index + 1 }}. {{ cause.title }}</strong>
              <span>{{ confidencePercent(cause.confidence) }}%</span>
            </div>
            <p>{{ cause.detail }}</p>
            <div class="ai-evidence-refs">
              <code v-for="evidenceRef in cause.evidence_refs" :key="evidenceRef">
                {{ evidenceRef }}
              </code>
            </div>
          </article>
        </div>

        <div class="ai-guidance-grid">
          <div>
            <strong>建议下一步</strong>
            <ol v-if="aiAnalysis.recommendations.length">
              <li v-for="item in aiAnalysis.recommendations" :key="item">{{ item }}</li>
            </ol>
            <p v-else class="empty-guidance">暂无明确建议</p>
          </div>
          <div>
            <strong>仍缺少的证据</strong>
            <ul v-if="aiAnalysis.missing_evidence.length">
              <li v-for="item in aiAnalysis.missing_evidence" :key="item">{{ item }}</li>
            </ul>
            <p v-else class="empty-guidance">当前证据未标记明显缺口</p>
          </div>
        </div>

        <footer class="ai-analysis-meta">
          <span>{{ aiAnalysis.model }} · {{ aiAnalysis.latency_ms }} ms</span>
          <span>分析 {{ aiAnalysis.analyzed_case_count }} 条异常用例</span>
          <span v-if="aiAnalysis.evidence_truncated">证据已按安全上限截断</span>
          <span>{{ formatDate(aiAnalysis.generated_at) }}</span>
          <strong>AI 内容仅供研判，不改变平台测试结果</strong>
        </footer>
      </div>
    </section>

    <div v-if="run" class="detail-grid">
      <main>
        <section v-if="failedCases.length" class="surface failure-section">
          <div class="toolbar">
            <div>
              <strong><el-icon><WarningFilled /></el-icon> 异常用例</strong>
              <span class="section-note">优先展示断言、步骤与基础设施失败</span>
            </div>
          </div>
          <el-table :data="failedCases">
            <el-table-column prop="case_code" label="用例" width="155" />
            <el-table-column label="状态" width="125">
              <template #default="scope">
                <el-tag :type="statusTagType(scope.row.status)">{{ scope.row.status }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="failure_category" label="类别" width="160" />
            <el-table-column label="错误" min-width="320">
              <template #default="scope"><span class="error-text">{{ scope.row.error_message }}</span></template>
            </el-table-column>
            <el-table-column label="耗时" width="100">
              <template #default="scope">{{ formatDuration(scope.row.duration_ms) }}</template>
            </el-table-column>
          </el-table>
        </section>

        <section class="surface cases-section">
          <div class="toolbar">
            <div><strong>逐用例结果</strong><span class="section-note">按不可变快照顺序</span></div>
          </div>
          <el-table :data="run.cases" empty-text="尚无用例状态">
            <el-table-column prop="sequence" label="#" width="64" />
            <el-table-column prop="case_code" label="用例编号" min-width="155" />
            <el-table-column label="状态" width="130">
              <template #default="scope">
                <el-tag :type="statusTagType(scope.row.status)" effect="light">
                  {{ scope.row.status }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="failure_category" label="失败类别" min-width="150" />
            <el-table-column label="耗时" width="110">
              <template #default="scope">{{ formatDuration(scope.row.duration_ms) }}</template>
            </el-table-column>
          </el-table>
        </section>

        <section class="surface artifact-section">
          <div class="toolbar">
            <div><strong>运行制品</strong><span class="section-note">下载链接短时有效且受项目权限保护</span></div>
          </div>
          <el-table :data="run.artifacts" empty-text="当前没有可下载制品">
            <el-table-column prop="kind" label="类型" width="120" />
            <el-table-column prop="name" label="文件" min-width="220" />
            <el-table-column label="大小" width="110">
              <template #default="scope">{{ formatBytes(scope.row.size_bytes) }}</template>
            </el-table-column>
            <el-table-column label="摘要" min-width="210">
              <template #default="scope"><code>{{ shortDigest(scope.row.digest) }}</code></template>
            </el-table-column>
            <el-table-column label="操作" width="110" fixed="right">
              <template #default="scope">
                <el-button
                  text
                  type="primary"
                  :loading="downloadingArtifact === scope.row.artifact_id"
                  @click="downloadArtifact(scope.row)"
                >
                  <el-icon><Download /></el-icon>下载
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </section>
      </main>

      <aside>
        <section class="surface snapshot-card">
          <h2>执行快照</h2>
          <dl>
            <div><dt>基线</dt><dd>{{ run.snapshot.case_baseline.version }}</dd></div>
            <div><dt>自动化包</dt><dd>{{ run.snapshot.automation_package.name }}@{{ run.snapshot.automation_package.version }}</dd></div>
            <div>
              <dt>供应链</dt>
              <dd>
                <el-tag
                  :type="run.snapshot.automation_package.supply_chain ? 'success' : 'info'"
                  size="small"
                  effect="plain"
                >{{ run.snapshot.automation_package.supply_chain ? 'VERIFIED' : 'LEGACY' }}</el-tag>
              </dd>
            </div>
            <div v-if="run.snapshot.automation_package.supply_chain">
              <dt>准入策略</dt>
              <dd>{{ run.snapshot.automation_package.supply_chain.policy_version }}</dd>
            </div>
            <div v-if="run.snapshot.automation_package.supply_chain">
              <dt>证明报告</dt>
              <dd class="mono">{{ shortDigest(run.snapshot.automation_package.supply_chain.report_digest) }}</dd>
            </div>
            <div v-if="run.snapshot.automation_package.supply_chain">
              <dt>源码修订</dt>
              <dd class="mono">{{ run.snapshot.automation_package.supply_chain.source_revision.slice(0, 12) }}</dd>
            </div>
            <div><dt>浏览器</dt><dd>{{ run.snapshot.browser || '—' }}</dd></div>
            <div>
              <dt>运行方式</dt>
              <dd>{{
                run.snapshot.automation_package.runner_type === 'ANDROID_APPIUM'
                  ? 'Android 真机'
                  : run.snapshot.web_config?.headless === false ? '有头模式' : '无头模式'
              }}</dd>
            </div>
            <div v-if="headedRun">
              <dt>实时观看</dt>
              <dd>
                <el-tag
                  :type="liveViewState?.status === 'READY' ? 'success' : liveViewState?.status === 'UNAVAILABLE' ? 'danger' : 'info'"
                  size="small"
                  effect="plain"
                >{{ liveViewLabel }} · 只读</el-tag>
              </dd>
            </div>
            <div><dt>快照摘要</dt><dd class="mono">{{ shortDigest(run.snapshot_digest) }}</dd></div>
            <div><dt>配置摘要</dt><dd class="mono">{{ shortDigest(run.snapshot.config_hash) }}</dd></div>
            <div><dt>创建时间</dt><dd>{{ formatDate(run.created_at) }}</dd></div>
            <div><dt>结束时间</dt><dd>{{ formatDate(run.finished_at) }}</dd></div>
          </dl>
        </section>

        <section v-if="run.result?.execution_isolation" class="surface snapshot-card">
          <h2>执行隔离证据</h2>
          <dl>
            <div>
              <dt>执行模式</dt>
              <dd><el-tag type="success" size="small">{{ run.result.execution_isolation.mode }}</el-tag></dd>
            </div>
            <div><dt>执行器版本</dt><dd>{{ run.result.execution_isolation.executor_version }}</dd></div>
            <div><dt>独立进程</dt><dd>{{ run.result.execution_isolation.dedicated_process ? '已启用' : '未启用' }}</dd></div>
            <div><dt>凭据范围</dt><dd>{{ run.result.execution_isolation.credential_scope }}</dd></div>
            <div><dt>工作区范围</dt><dd>{{ run.result.execution_isolation.workspace_scope }}</dd></div>
            <div><dt>只读根文件系统</dt><dd>{{ run.result.execution_isolation.read_only_root_filesystem ? '已强制' : '未强制' }}</dd></div>
            <div><dt>网络策略</dt><dd>{{ run.result.execution_isolation.network_policy }}</dd></div>
            <div><dt>资源限制</dt><dd>{{ run.result.execution_isolation.resource_limits_enforced ? '已强制' : '未强制' }}</dd></div>
            <div v-if="run.result.execution_isolation.runtime_image_id">
              <dt>不可变容器镜像</dt>
              <dd class="mono">{{ shortDigest(run.result.execution_isolation.runtime_image_id) }}</dd>
            </div>
            <div v-if="run.result.execution_isolation.memory_limit_bytes">
              <dt>内存上限</dt>
              <dd>{{ Math.round(run.result.execution_isolation.memory_limit_bytes / 1024 / 1024) }} MiB</dd>
            </div>
            <div v-if="run.result.execution_isolation.cpu_limit_millis">
              <dt>CPU 上限</dt><dd>{{ run.result.execution_isolation.cpu_limit_millis }} millicores</dd>
            </div>
            <div v-if="run.result.execution_isolation.pids_limit">
              <dt>PID 上限</dt><dd>{{ run.result.execution_isolation.pids_limit }}</dd>
            </div>
            <div v-if="run.result.execution_isolation.ephemeral_storage_limit_bytes">
              <dt>临时存储上限</dt>
              <dd>{{ Math.round(run.result.execution_isolation.ephemeral_storage_limit_bytes / 1024 / 1024) }} MiB</dd>
            </div>
            <div v-if="run.result.execution_isolation.orchestrator_namespace">
              <dt>Namespace</dt><dd>{{ run.result.execution_isolation.orchestrator_namespace }}</dd>
            </div>
            <div v-if="run.result.execution_isolation.service_account_name">
              <dt>ServiceAccount</dt><dd>{{ run.result.execution_isolation.service_account_name }}</dd>
            </div>
            <div v-if="run.result.execution_isolation.service_account_token_automounted !== null && run.result.execution_isolation.service_account_token_automounted !== undefined">
              <dt>SA Token 自动挂载</dt><dd>{{ run.result.execution_isolation.service_account_token_automounted ? '是' : '已禁用' }}</dd>
            </div>
          </dl>
          <el-alert
            v-if="!run.result.execution_isolation.read_only_root_filesystem || !run.result.execution_isolation.resource_limits_enforced"
            title="当前执行结果未完整强制文件系统或资源硬隔离，请检查 Worker 执行模式与沙箱配置。"
            type="warning"
            :closable="false"
            show-icon
          />
        </section>

        <section class="surface timeline-card">
          <div class="timeline-heading">
            <div><h2>事件时间线</h2><span>{{ visibleEvents.length }} 条</span></div>
            <span v-if="streamConnected" class="pulse" />
          </div>
          <el-timeline v-if="visibleEvents.length">
            <el-timeline-item
              v-for="event in visibleEvents"
              :key="event.id"
              :timestamp="formatDate(event.occurred_at)"
              :type="event.status ? statusTagType(event.status) : 'primary'"
              placement="top"
            >
              <strong>{{ eventLabel(event) }}</strong>
              <span v-if="event.case_code" class="event-case mono">{{ event.case_code }}</span>
              <el-tag v-if="event.status" :type="statusTagType(event.status)" size="small">
                {{ event.status }}
              </el-tag>
              <small>{{ event.source }} · #{{ event.sequence }}</small>
            </el-timeline-item>
          </el-timeline>
          <el-empty v-else description="等待运行事件" :image-size="72" />
        </section>
      </aside>
    </div>

    <el-dialog
      v-model="liveViewDialogVisible"
      title="实时观看 · 只读模式"
      width="96vw"
      top="3vh"
      append-to-body
      destroy-on-close
      class="live-view-dialog"
      @closed="closeLiveView"
    >
      <el-alert
        title="画面来自当前 Run 的隔离浏览器；只允许观看，不能点击或输入。执行结束后连接会自动失效。"
        type="info"
        :closable="false"
        show-icon
        class="live-view-alert"
      />
      <iframe
        v-if="liveViewUrl"
        :src="liveViewUrl"
        class="live-view-frame"
        title="Run 自动化执行实时画面"
        sandbox="allow-scripts allow-same-origin"
        referrerpolicy="no-referrer"
        allow="fullscreen"
      />
    </el-dialog>
  </div>
</template>

<style scoped>
.run-page {
  max-width: 1520px;
}

.back-link {
  margin: -8px 0 10px -12px;
  color: #66788a;
}

.run-heading {
  align-items: center;
}

.run-kicker {
  color: var(--top-accent-600);
  font-size: 11px;
  font-weight: 750;
  letter-spacing: 0.15em;
  text-transform: uppercase;
}

.run-heading p {
  margin-top: 7px;
}

.heading-actions,
.live-state {
  display: flex;
  align-items: center;
}

.heading-actions {
  gap: 14px;
}

.live-state {
  gap: 7px;
  color: #237e70;
  font-size: 12px;
}

.live-state i,
.pulse {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #1eb980;
  box-shadow: 0 0 0 4px rgb(30 185 128 / 12%);
}

.live-state.offline {
  color: #a56813;
}

.live-state.offline i {
  background: #e6a23c;
}

.lineage-alert,
.schedule-origin-alert,
.stream-alert,
.diagnostic-alert {
  margin-bottom: 18px;
}

.ai-analysis-section {
  margin-bottom: 20px;
  padding: 22px 24px;
  border: 1px solid #d8e5f5;
  background: linear-gradient(135deg, #fff 0%, #f4f8ff 100%);
}

.ai-analysis-heading,
.ai-analysis-tags,
.ai-cause-card > div:first-child,
.ai-analysis-meta {
  display: flex;
  align-items: center;
}

.ai-analysis-heading {
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 18px;
}

.ai-analysis-heading h2 {
  margin: 4px 0 0;
  color: #17324d;
  font-size: 20px;
}

.ai-kicker {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: #476aa8;
  font-size: 11px;
  font-weight: 750;
  letter-spacing: 0.1em;
}

.ai-analysis-tags {
  gap: 8px;
}

.ai-summary {
  margin: 0;
  color: #34495e;
  font-size: 14px;
  line-height: 1.75;
}

.ai-cause-list {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 12px;
  margin-top: 18px;
}

.ai-cause-card {
  padding: 16px;
  border: 1px solid #e1e8f2;
  border-radius: 10px;
  background: rgb(255 255 255 / 82%);
}

.ai-cause-card > div:first-child {
  justify-content: space-between;
  gap: 12px;
}

.ai-cause-card strong {
  color: #27415b;
  font-size: 13px;
}

.ai-cause-card > div:first-child span {
  color: #607ca5;
  font-size: 11px;
  font-weight: 700;
}

.ai-cause-card p {
  margin: 10px 0;
  color: #5e7083;
  font-size: 12px;
  line-height: 1.65;
}

.ai-evidence-refs {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.ai-evidence-refs code {
  padding: 3px 7px;
  border-radius: 5px;
  background: #edf3fb;
  color: #4e6b91;
}

.ai-guidance-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 18px;
  margin-top: 20px;
  padding-top: 18px;
  border-top: 1px solid #e2e9f2;
}

.ai-guidance-grid strong {
  color: #27415b;
  font-size: 13px;
}

.ai-guidance-grid ol,
.ai-guidance-grid ul {
  margin: 10px 0 0;
  padding-left: 20px;
  color: #52677b;
  font-size: 12px;
  line-height: 1.75;
}

.empty-guidance {
  margin: 10px 0 0;
  color: #8b9aaa;
  font-size: 12px;
}

.ai-analysis-meta {
  flex-wrap: wrap;
  gap: 8px 16px;
  margin-top: 18px;
  color: #8392a2;
  font-size: 10px;
}

.ai-analysis-meta strong {
  margin-left: auto;
  color: #5b7191;
}

.run-progress-card {
  margin-bottom: 18px;
  padding: 22px 24px;
  overflow: hidden;
  border: 1px solid rgb(15 130 119 / 18%);
  background:
    radial-gradient(circle at 100% 0%, rgb(24 163 148 / 10%), transparent 22rem),
    linear-gradient(145deg, #fff 0%, #f6fbfa 100%);
  box-shadow: var(--top-shadow-1);
}

.run-progress-card.progress-failed {
  border-color: rgb(210 75 86 / 19%);
  background:
    radial-gradient(circle at 100% 0%, rgb(210 75 86 / 8%), transparent 22rem),
    linear-gradient(145deg, #fff 0%, #fff9f9 100%);
}

.progress-heading,
.round-progress-label,
.progress-footnote {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.progress-heading {
  margin-bottom: 18px;
}

.progress-heading h2 {
  margin: 3px 0 0;
  color: var(--top-text-strong);
  font-size: 20px;
}

.progress-eyebrow {
  color: var(--top-accent-600);
  font-size: 11px;
  font-weight: 750;
  letter-spacing: 0.12em;
}

.round-stage-track {
  --round-count: 3;

  position: relative;
  display: grid;
  grid-template-columns: repeat(var(--round-count), minmax(0, 1fr));
  gap: 12px;
  margin: 2px 0 22px;
  padding: 14px 16px;
  border: 1px solid rgb(205 216 227 / 78%);
  border-radius: var(--top-radius-md);
  background: rgb(255 255 255 / 72%);
}

.round-stage-track::before {
  position: absolute;
  top: 30px;
  right: calc(16px + (100% - 32px) / (var(--round-count) * 2));
  left: calc(16px + (100% - 32px) / (var(--round-count) * 2));
  height: 1px;
  background: var(--top-border-strong);
  content: "";
}

.round-stage {
  position: relative;
  z-index: 1;
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 9px;
}

.round-stage-node {
  display: grid;
  width: 32px;
  height: 32px;
  flex: 0 0 32px;
  place-items: center;
  border: 1px solid var(--top-border-strong);
  border-radius: 50%;
  color: var(--top-text-faint);
  background: var(--top-surface);
  font-size: 11px;
  font-weight: 800;
  box-shadow: 0 0 0 4px rgb(255 255 255 / 78%);
}

.round-stage > div {
  display: grid;
  min-width: 0;
  gap: 2px;
}

.round-stage strong,
.round-stage small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.round-stage strong {
  color: var(--top-text);
  font-size: 11px;
}

.round-stage small {
  color: var(--top-text-faint);
  font-size: 9px;
}

.round-stage.stage-complete .round-stage-node {
  border-color: var(--top-accent-600);
  color: #fff;
  background: var(--top-accent-600);
}

.round-stage.stage-active .round-stage-node {
  border-color: var(--top-info);
  color: #fff;
  background: var(--top-info);
  box-shadow: 0 0 0 5px rgb(63 115 184 / 12%);
}

.round-stage.stage-active strong {
  color: var(--top-info);
}

.round-stage.stage-error .round-stage-node {
  border-color: var(--top-danger);
  color: #fff;
  background: var(--top-danger);
  box-shadow: 0 0 0 5px rgb(210 75 86 / 11%);
}

.round-stage.stage-error strong {
  color: var(--top-danger);
}

.round-stage.stage-skipped {
  opacity: 0.62;
}

.round-progress-label {
  margin-bottom: 8px;
  color: #617589;
  font-size: 12px;
}

.round-progress-label strong {
  color: #29465f;
  font-size: 13px;
}

.run-progress-card :deep(.el-progress-bar__outer) {
  background: #e4eeec;
}

.run-progress-card :deep(.el-progress-bar__inner) {
  background: linear-gradient(90deg, var(--top-accent-700), #37b99a);
}

.run-progress-card.progress-failed :deep(.el-progress-bar__inner) {
  background: linear-gradient(90deg, #d04a52, #ef777d);
}

.progress-stat-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  margin-top: 20px;
}

.progress-stat-grid > div {
  display: grid;
  gap: 4px;
  padding: 14px 16px;
  border: 1px solid var(--top-border);
  border-radius: var(--top-radius-md);
  background: rgb(255 255 255 / 82%);
}

.progress-stat-grid span,
.progress-stat-grid small,
.progress-footnote {
  color: #7b8d9d;
  font-size: 11px;
}

.progress-stat-grid strong {
  color: #24445d;
  font-size: 20px;
}

.progress-stat-grid .success-stat strong {
  color: #168250;
}

.progress-stat-grid .failure-stat strong {
  color: #b63d46;
}

.progress-footnote {
  gap: 18px;
  justify-content: flex-start;
  margin-top: 14px;
}

.progress-footnote span:last-child {
  margin-left: auto;
}

.run-metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
  margin-bottom: 20px;
}

.metric-card {
  display: grid;
  grid-template-columns: 48px 1fr auto;
  gap: 15px;
  align-items: center;
  padding: 22px;
  border: 1px solid var(--top-border);
  border-radius: var(--top-radius-lg);
  background: var(--top-surface);
  box-shadow: var(--top-shadow-1);
}

.metric-icon {
  display: grid;
  width: 48px;
  height: 48px;
  place-items: center;
  border-radius: 11px;
}

.metric-icon svg {
  width: 23px;
}

.metric-icon.teal {
  color: #127c70;
  background: #e8f7f3;
}

.metric-icon.blue {
  color: #2d68a1;
  background: #eaf3fb;
}

.metric-icon.amber {
  color: #a56813;
  background: #fff4dc;
}

.metric-card strong,
.metric-card > div > span {
  display: block;
}

.metric-card strong {
  color: #17324d;
  font-size: 23px;
}

.metric-card > div > span {
  margin-top: 2px;
  color: #64778a;
  font-size: 12px;
}

.metric-card small {
  color: #8c9aa7;
  font-size: 11px;
}

.status-card .el-tag {
  margin-top: 5px;
}

.detail-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 340px;
  gap: 20px;
  align-items: start;
}

.detail-grid main {
  min-width: 0;
}

.detail-grid main > section + section,
.detail-grid aside > section + section {
  margin-top: 18px;
}

.failure-section,
.cases-section,
.artifact-section,
.timeline-card {
  overflow: hidden;
}

.failure-section .toolbar strong {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: #b74444;
}

.error-text {
  color: #a63f3f;
  font-family: "SFMono-Regular", Consolas, monospace;
  font-size: 11px;
  line-height: 1.5;
}

.snapshot-card,
.timeline-card {
  padding: 20px;
}

.snapshot-card h2,
.timeline-card h2 {
  margin: 0;
  color: #17324d;
  font-size: 15px;
}

.snapshot-card dl {
  margin: 16px 0 0;
}

.snapshot-card dl > div {
  display: grid;
  gap: 5px;
  padding: 12px 0;
  border-top: 1px solid #edf1f4;
}

.snapshot-card dt {
  color: #8594a3;
  font-size: 11px;
}

.snapshot-card dd {
  margin: 0;
  overflow-wrap: anywhere;
  color: #34495e;
  font-size: 12px;
}

.timeline-heading,
.timeline-heading > div {
  display: flex;
  align-items: center;
}

.timeline-heading {
  justify-content: space-between;
  margin-bottom: 18px;
}

.timeline-heading > div {
  gap: 8px;
}

.timeline-heading span:not(.pulse) {
  color: #8a98a6;
  font-size: 11px;
}

.timeline-card :deep(.el-timeline) {
  max-height: 650px;
  margin: 0;
  padding-left: 6px;
  overflow-y: auto;
}

.timeline-card :deep(.el-timeline-item__content) {
  display: grid;
  justify-items: start;
  gap: 6px;
}

.timeline-card :deep(.el-timeline-item__timestamp) {
  font-size: 10px;
}

.timeline-card strong {
  color: #34495e;
  font-size: 12px;
}

.timeline-card small {
  color: #93a0ad;
  font-size: 10px;
}

.event-case {
  color: #167d73;
}

code {
  color: #4f6478;
  font-size: 11px;
}

.live-view-alert {
  margin-bottom: 12px;
}

.live-view-frame {
  display: block;
  width: 100%;
  height: 78vh;
  border: 1px solid #dfe7ec;
  border-radius: 10px;
  background: #091c2c;
}

:deep(.live-view-dialog) {
  max-width: 1500px;
}

:deep(.live-view-dialog .el-dialog__body) {
  padding-top: 8px;
}

@media (max-width: 1240px) {
  .run-metrics {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 900px) {
  .ai-analysis-heading {
    align-items: flex-start;
    flex-direction: column;
  }

  .ai-guidance-grid {
    grid-template-columns: 1fr;
  }

  .ai-analysis-meta strong {
    width: 100%;
    margin-left: 0;
  }

  .progress-stat-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .progress-footnote {
    align-items: flex-start;
    flex-direction: column;
    gap: 6px;
  }

  .progress-footnote span:last-child {
    margin-left: 0;
  }
}
</style>
