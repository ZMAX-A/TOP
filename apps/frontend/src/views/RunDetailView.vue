<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowLeft,
  CircleClose,
  Connection,
  Download,
  Files,
  Refresh,
  RefreshRight,
  Timer,
  WarningFilled,
} from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'

import { ApiError, api, streamRunEvents } from '@/api/client'
import type { Artifact, ProjectMember, RunDetail, RunEvent, RunStatus } from '@/api/types'
import { auth } from '@/auth'
import { formatDate, runTagType, shortDigest } from '@/presentation'

const TERMINAL_STATUSES = new Set<RunStatus>([
  'PASSED',
  'FAILED',
  'CANCELED',
  'TIMED_OUT',
  'INFRA_ERROR',
])
const DISPATCH_WAIT_LABELS: Record<string, string> = {
  RUNNER_POOL_NOT_FOUND: '绑定的 Runner Pool 不存在',
  RUNNER_POOL_DRAINING: 'Runner Pool 正在排空，暂不接收新任务',
  RUNNER_POOL_DISABLED: 'Runner Pool 已禁用',
  NO_HEALTHY_RUNNER: '正在等待健康 Runner 心跳',
  RUNNER_CAPABILITY_MISMATCH: '当前健康 Runner 的能力与此 Run 不匹配',
  RUNNER_POOL_CAPACITY_EXHAUSTED: 'Runner Pool 槽位已满，正在等待释放',
  BROKER_PUBLISH_FAILED: '消息队列投递失败，系统正在自动重试',
}

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
const members = ref<ProjectMember[]>([])
let streamController: AbortController | null = null
let retryTimer: number | null = null
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
const progressLabel = computed(() => {
  if (!run.value) return '读取中'
  const finished = run.value.cases.filter((item) =>
    ['PASSED', 'FAILED', 'SKIPPED', 'INFRA_ERROR', 'TIMED_OUT'].includes(item.status),
  ).length
  return `${finished} / ${run.value.case_count}`
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

function eventLabel(event: RunEvent): string {
  const labels: Record<string, string> = {
    run_created: '运行已创建',
    status_changed: '运行状态变更',
    run_started: 'Runner 开始执行',
    case_started: '用例开始',
    case_finished: '用例结束',
    run_finished: 'Runner 执行结束',
    result_recorded: '不可变结果已入库',
    cancel_requested: '已请求取消',
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
  if (['status_changed', 'run_finished', 'result_recorded'].includes(event.event_type)) {
    void refreshDetail()
  }
}

async function refreshDetail(): Promise<void> {
  try {
    run.value = await api.run(runId.value)
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
    run.value = detail
    events.value = timeline.sort((left, right) => left.sequence - right.sequence)
    members.value = memberList
    void startEventStream()
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : 'Run 详情加载失败')
  } finally {
    loading.value = false
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
    const created = await api.rerunRun(run.value.id, mode, crypto.randomUUID())
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
})
</script>

<template>
  <div v-loading="loading || actionBusy" class="page-container run-page">
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
      v-if="streamError && !terminal"
      :title="`${streamError}，页面将在后台重连。`"
      type="warning"
      show-icon
      :closable="false"
      class="stream-alert"
    />

    <section v-if="run" class="metric-grid run-metrics">
      <article class="metric-card status-card">
        <span class="metric-icon teal"><Connection /></span>
        <div><span>当前状态</span><el-tag :type="runTagType(run.status)">{{ run.status }}</el-tag></div>
        <small>{{ terminal ? '执行已结束' : '实时更新中' }}</small>
      </article>
      <article class="metric-card">
        <span class="metric-icon blue"><Files /></span>
        <div><strong>{{ progressLabel }}</strong><span>完成用例</span></div>
        <small>{{ failedCases.length }} 条异常</small>
      </article>
      <article class="metric-card">
        <span class="metric-icon amber"><Timer /></span>
        <div><strong>{{ run.result ? formatDuration(run.result.case_results.reduce((sum, item) => sum + item.duration_ms, 0)) : '—' }}</strong><span>累计用例耗时</span></div>
        <small>{{ formatDate(run.started_at) }}</small>
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
            <div><dt>浏览器</dt><dd>{{ run.snapshot.browser || '—' }}</dd></div>
            <div><dt>快照摘要</dt><dd class="mono">{{ shortDigest(run.snapshot_digest) }}</dd></div>
            <div><dt>配置摘要</dt><dd class="mono">{{ shortDigest(run.snapshot.config_hash) }}</dd></div>
            <div><dt>创建时间</dt><dd>{{ formatDate(run.created_at) }}</dd></div>
            <div><dt>结束时间</dt><dd>{{ formatDate(run.finished_at) }}</dd></div>
          </dl>
        </section>

        <section class="surface timeline-card">
          <div class="timeline-heading">
            <div><h2>事件时间线</h2><span>{{ events.length }} 条</span></div>
            <span v-if="streamConnected" class="pulse" />
          </div>
          <el-timeline v-if="events.length">
            <el-timeline-item
              v-for="event in events"
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
  color: #168579;
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
.stream-alert,
.diagnostic-alert {
  margin-bottom: 18px;
}

.run-metrics {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
  margin-bottom: 20px;
}

.metric-card {
  display: grid;
  grid-template-columns: 48px 1fr auto;
  gap: 15px;
  align-items: center;
  padding: 22px;
  border: 1px solid #e5eaee;
  border-radius: 11px;
  background: #fff;
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
</style>
