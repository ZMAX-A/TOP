<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowLeft,
  CircleClose,
  Refresh,
  RefreshRight,
  Search,
  VideoPlay,
} from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { ApiError, api } from '@/api/client'
import type {
  Environment,
  Project,
  ProjectMember,
  Run,
  RunStatus,
  Target,
} from '@/api/types'
import { auth } from '@/auth'
import { formatDate, formatUtcDate, runTagType, shortDigest } from '@/presentation'

const TERMINAL_STATUSES = new Set<RunStatus>([
  'PASSED',
  'FAILED',
  'CANCELED',
  'TIMED_OUT',
  'INFRA_ERROR',
])
const STATUS_OPTIONS: RunStatus[] = [
  'QUEUED',
  'PREPARING',
  'RUNNING',
  'PASSED',
  'FAILED',
  'CANCELED',
  'TIMED_OUT',
  'INFRA_ERROR',
]
const DISPATCH_WAIT_LABELS: Record<string, string> = {
  RUNNER_POOL_NOT_FOUND: '资源池不存在',
  RUNNER_POOL_DRAINING: '资源池排空中',
  RUNNER_POOL_DISABLED: '资源池已禁用',
  NO_HEALTHY_RUNNER: '等待健康 Runner',
  RUNNER_CAPABILITY_MISMATCH: '等待匹配能力',
  RUNNER_POOL_CAPACITY_EXHAUSTED: '等待空闲槽位',
  BROKER_PUBLISH_FAILED: '队列投递重试中',
}

const route = useRoute()
const router = useRouter()
const projectId = computed(() => String(route.params.projectId))
const loading = ref(true)
const actionBusy = ref(false)
const project = ref<Project>()
const runs = ref<Run[]>([])
const total = ref(0)
const targets = ref<Target[]>([])
const environments = ref<Environment[]>([])
const members = ref<ProjectMember[]>([])
const selectedRuns = ref<Run[]>([])
const offset = ref(0)
const pageSize = 25

const filters = reactive<{
  statuses: RunStatus[]
  target_id: string
  environment_id: string
  created_by: string
  case_code: string
  created_range: Date[]
}>({
  statuses: [],
  target_id: '',
  environment_id: '',
  created_by: '',
  case_code: '',
  created_range: [],
})

const currentMember = computed(() =>
  members.value.find((item) => item.user_id === auth.state.user?.id),
)
const canOperate = computed(
  () =>
    auth.state.user?.system_role === 'SYSTEM_ADMIN' ||
    ['PROJECT_ADMIN', 'TESTER'].includes(currentMember.value?.role ?? ''),
)
const selectedCancelable = computed(() =>
  selectedRuns.value.filter((item) => !TERMINAL_STATUSES.has(item.status)),
)
const terminalOnPage = computed(
  () => runs.value.filter((item) => TERMINAL_STATUSES.has(item.status)).length,
)
const failedOnPage = computed(
  () => runs.value.filter((item) => ['FAILED', 'INFRA_ERROR', 'TIMED_OUT'].includes(item.status)).length,
)

function report(error: unknown, fallback: string): void {
  ElMessage.error(error instanceof ApiError ? error.message : fallback)
}

async function loadRuns(): Promise<void> {
  loading.value = true
  try {
    const page = await api.runs(projectId.value, {
      status: filters.statuses.length ? filters.statuses : undefined,
      target_id: filters.target_id || undefined,
      environment_id: filters.environment_id || undefined,
      created_by: filters.created_by || undefined,
      case_code: filters.case_code.trim().toUpperCase() || undefined,
      created_from: filters.created_range[0]?.toISOString(),
      created_to: filters.created_range[1]?.toISOString(),
      offset: offset.value,
      limit: pageSize,
    })
    runs.value = page.items
    total.value = page.total
    selectedRuns.value = []
  } catch (error) {
    report(error, '运行列表加载失败')
  } finally {
    loading.value = false
  }
}

async function initialize(): Promise<void> {
  loading.value = true
  try {
    const [projectList, targetList, memberList] = await Promise.all([
      api.projects(),
      api.targets(projectId.value),
      api.projectMembers(projectId.value),
    ])
    project.value = projectList.find((item) => item.id === projectId.value)
    targets.value = targetList
    members.value = memberList
    await loadRuns()
  } catch (error) {
    loading.value = false
    report(error, '运行运营数据加载失败')
  }
}

async function changeTarget(): Promise<void> {
  filters.environment_id = ''
  environments.value = []
  if (!filters.target_id) return
  try {
    environments.value = await api.environments(projectId.value, filters.target_id)
  } catch (error) {
    report(error, '环境列表加载失败')
  }
}

function applyFilters(): void {
  offset.value = 0
  void loadRuns()
}

function resetFilters(): void {
  filters.statuses = []
  filters.target_id = ''
  filters.environment_id = ''
  filters.created_by = ''
  filters.case_code = ''
  filters.created_range = []
  environments.value = []
  offset.value = 0
  void loadRuns()
}

function changePage(page: number): void {
  offset.value = (page - 1) * pageSize
  void loadRuns()
}

function selectRows(rows: unknown[]): void {
  selectedRuns.value = rows as Run[]
}

async function batchCancel(): Promise<void> {
  const candidates = selectedCancelable.value
  if (!candidates.length) {
    ElMessage.warning('请选择尚未结束的 Run')
    return
  }
  try {
    await ElMessageBox.confirm(
      `将向 ${candidates.length} 个 Run 发出取消请求，已经结束的 Run 不会改变。`,
      '批量取消运行',
      { type: 'warning', confirmButtonText: '确认取消', cancelButtonText: '返回' },
    )
  } catch {
    return
  }
  actionBusy.value = true
  try {
    const result = await api.batchCancelRuns(
      projectId.value,
      candidates.map((item) => item.id),
    )
    ElMessage.success(`已处理 ${result.requested} 个 Run，${result.changed} 个状态发生变化`)
    await loadRuns()
  } catch (error) {
    report(error, '批量取消失败')
  } finally {
    actionBusy.value = false
  }
}

async function rerun(run: Run, mode: 'FULL' | 'FAILED_ONLY'): Promise<void> {
  actionBusy.value = true
  try {
    const created = await api.rerunRun(run.id, mode, crypto.randomUUID())
    ElMessage.success(mode === 'FULL' ? '完整重跑已创建' : '仅异常用例重跑已创建')
    await router.push({
      name: 'run-detail',
      params: { projectId: projectId.value, runId: created.id },
    })
  } catch (error) {
    report(error, mode === 'FULL' ? '完整重跑创建失败' : '异常用例重跑创建失败')
  } finally {
    actionBusy.value = false
  }
}

function canRetryFailed(run: Run): boolean {
  return ['FAILED', 'INFRA_ERROR'].includes(run.status)
}

function dispatchWaitLabel(run: Run): string | null {
  if (run.dispatch_state !== 'WAITING' || !run.dispatch_wait_reason) return null
  return DISPATCH_WAIT_LABELS[run.dispatch_wait_reason] || run.dispatch_wait_reason
}

onMounted(initialize)
</script>

<template>
  <div class="page-container" v-loading="loading || actionBusy">
    <el-button text class="back-link" @click="router.push({ name: 'project', params: { projectId }, query: { view: 'runs' } })">
      <el-icon><ArrowLeft /></el-icon>
      返回项目
    </el-button>
    <header class="page-heading operations-heading">
      <div>
        <div class="project-key">{{ project?.key }}</div>
        <h1>{{ project?.name || '项目' }} · 运行运营</h1>
        <p>筛选执行历史、批量取消进行中任务，并从不可变 Snapshot 创建可追溯重跑。</p>
      </div>
      <div class="heading-actions">
        <el-button :icon="Refresh" @click="loadRuns">刷新</el-button>
        <el-button
          v-if="canOperate"
          type="danger"
          plain
          :icon="CircleClose"
          :disabled="selectedCancelable.length === 0"
          @click="batchCancel"
        >批量取消（{{ selectedCancelable.length }}）</el-button>
      </div>
    </header>

    <div class="metric-grid">
      <article class="metric-card"><el-icon><VideoPlay /></el-icon><strong>{{ total }}</strong><span>匹配运行</span></article>
      <article class="metric-card"><el-icon><RefreshRight /></el-icon><strong>{{ terminalOnPage }}</strong><span>本页已结束</span></article>
      <article class="metric-card danger"><el-icon><CircleClose /></el-icon><strong>{{ failedOnPage }}</strong><span>本页异常</span></article>
    </div>

    <section class="surface filters-panel">
      <div class="filters-grid">
        <el-select v-model="filters.statuses" multiple collapse-tags clearable placeholder="运行状态">
          <el-option v-for="status in STATUS_OPTIONS" :key="status" :label="status" :value="status" />
        </el-select>
        <el-select v-model="filters.target_id" clearable placeholder="测试目标" @change="changeTarget">
          <el-option v-for="target in targets" :key="target.id" :label="target.name" :value="target.id" />
        </el-select>
        <el-select
          v-model="filters.environment_id"
          clearable
          :disabled="!filters.target_id"
          placeholder="运行环境"
        >
          <el-option
            v-for="environment in environments"
            :key="environment.id"
            :label="environment.name"
            :value="environment.id"
          />
        </el-select>
        <el-select v-model="filters.created_by" clearable filterable placeholder="创建人">
          <el-option
            v-for="member in members"
            :key="member.user_id"
            :label="`${member.display_name} · ${member.username}`"
            :value="member.user_id"
          />
        </el-select>
        <el-input v-model="filters.case_code" clearable placeholder="用例编号，如 TC-LOGIN-001" />
        <el-date-picker
          v-model="filters.created_range"
          type="datetimerange"
          start-placeholder="开始时间"
          end-placeholder="结束时间"
          range-separator="至"
        />
      </div>
      <div class="filter-actions">
        <el-button :icon="RefreshRight" @click="resetFilters">重置</el-button>
        <el-button type="primary" :icon="Search" @click="applyFilters">应用筛选</el-button>
      </div>
    </section>

    <section class="surface runs-surface">
      <el-table :data="runs" empty-text="暂无匹配运行" @selection-change="selectRows">
        <el-table-column v-if="canOperate" type="selection" width="48" />
        <el-table-column label="Run" min-width="285">
          <template #default="scope">
            <span
              class="table-link mono"
              @click="router.push({ name: 'run-detail', params: { projectId, runId: scope.row.id } })"
            >{{ scope.row.id }}</span>
            <div v-if="scope.row.source_run_id" class="lineage">
              {{ scope.row.retry_mode === 'FAILED_ONLY' ? '异常重跑' : '完整重跑' }} · 来源
              <span
                class="table-link mono"
                @click="router.push({ name: 'run-detail', params: { projectId, runId: scope.row.source_run_id } })"
              >{{ scope.row.source_run_id.slice(0, 8) }}</span>
            </div>
            <div v-if="scope.row.regression_schedule_id" class="lineage scheduled-lineage">
              定时回归 · 计划 <span class="mono">{{ scope.row.regression_schedule_id.slice(0, 8) }}</span>
              <template v-if="scope.row.scheduled_for"> · {{ formatUtcDate(scope.row.scheduled_for) }}</template>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="190">
          <template #default="scope">
            <el-tag :type="runTagType(scope.row.status)" class="status-tag">{{ scope.row.status }}</el-tag>
            <div v-if="dispatchWaitLabel(scope.row as Run)" class="dispatch-wait">
              {{ dispatchWaitLabel(scope.row as Run) }}
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="case_count" label="用例" width="82" />
        <el-table-column label="Snapshot" min-width="185">
          <template #default="scope"><code>{{ shortDigest(scope.row.snapshot_digest) }}</code></template>
        </el-table-column>
        <el-table-column label="创建时间" width="175">
          <template #default="scope">{{ formatDate(scope.row.created_at) }}</template>
        </el-table-column>
        <el-table-column v-if="canOperate" label="操作" width="220" fixed="right">
          <template #default="scope">
            <template v-if="TERMINAL_STATUSES.has(scope.row.status)">
              <el-button link type="primary" @click="rerun(scope.row as Run, 'FULL')">完整重跑</el-button>
              <el-button
                v-if="canRetryFailed(scope.row as Run)"
                link
                type="danger"
                @click="rerun(scope.row as Run, 'FAILED_ONLY')"
              >仅异常</el-button>
            </template>
            <span v-else class="muted">可选择后批量取消</span>
          </template>
        </el-table-column>
      </el-table>
      <el-pagination
        v-if="total > pageSize"
        class="pagination"
        layout="prev, pager, next, total"
        :page-size="pageSize"
        :total="total"
        :current-page="Math.floor(offset / pageSize) + 1"
        @current-change="changePage"
      />
    </section>
  </div>
</template>

<style scoped>
.back-link {
  margin: -8px 0 10px -12px;
  color: #66788a;
}

.operations-heading {
  align-items: center;
}

.project-key {
  color: #168579;
  font: 700 11px "SFMono-Regular", Consolas, monospace;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.heading-actions {
  display: flex;
  gap: 8px;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 16px;
  margin-bottom: 18px;
}

.metric-card {
  display: grid;
  grid-template-columns: 42px auto 1fr;
  align-items: center;
  gap: 12px;
  padding: 18px;
  border: 1px solid #e5eaee;
  border-radius: 11px;
  background: #fff;
}

.metric-card .el-icon {
  width: 42px;
  height: 42px;
  border-radius: 10px;
  color: #167d73;
  background: #e9f6f3;
  font-size: 20px;
}

.metric-card.danger .el-icon {
  color: #b74343;
  background: #fcecec;
}

.metric-card strong {
  color: #17324d;
  font-size: 23px;
}

.metric-card span {
  color: #718096;
  font-size: 12px;
}

.filters-panel {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 18px;
  padding: 18px 20px;
}

.filters-grid {
  display: grid;
  flex: 1;
  grid-template-columns: repeat(3, minmax(180px, 1fr));
  gap: 12px;
}

.filters-grid > :deep(*) {
  width: 100%;
}

.filter-actions {
  display: flex;
  gap: 8px;
}

.runs-surface {
  overflow: hidden;
}

.lineage {
  margin-top: 5px;
  color: #8795a5;
  font-size: 11px;
}

.dispatch-wait {
  margin-top: 5px;
  color: #c27a16;
  font-size: 11px;
}

.pagination {
  justify-content: flex-end;
  padding: 18px 20px;
}

code {
  color: #4f6478;
  font-size: 11px;
}
</style>
