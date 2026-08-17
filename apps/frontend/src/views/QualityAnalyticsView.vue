<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
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
import { ElMessage } from 'element-plus'

import { ApiError, api } from '@/api/client'
import type {
  Baseline,
  Environment,
  FailureCluster,
  FlakyCase,
  Project,
  ProjectMember,
  QualityAnalytics,
  QualityAnalyticsQuery,
  QualityPolicy,
  QualityTrendPoint,
  Target,
} from '@/api/types'
import { auth } from '@/auth'
import { formatDate, formatUtcDate, shortDigest } from '@/presentation'

const route = useRoute()
const router = useRouter()
const projectId = computed(() => String(route.params.projectId))
const loading = ref(true)
const policyBusy = ref(false)
const project = ref<Project>()
const members = ref<ProjectMember[]>([])
const targets = ref<Target[]>([])
const environmentsByTarget = ref<Record<string, Environment[]>>({})
const baselines = ref<Baseline[]>([])
const policy = ref<QualityPolicy>()
const analytics = ref<QualityAnalytics>()
const selectedWindowDays = ref(30)
const policyForm = reactive({ target_pass_rate_percent: 95, window_days: 30 })
const filterForm = reactive({ target_id: '', environment_id: '', baseline_id: '' })
let analyticsRequestSequence = 0

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
    const [projectList, memberList, loadedPolicy, targetList, baselineList, loadedAnalytics] =
      await Promise.all([
        api.projects(),
        api.projectMembers(projectId.value),
        api.qualityPolicy(projectId.value),
        api.targets(projectId.value),
        api.baselines(projectId.value),
        api.qualityAnalytics(projectId.value),
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
    selectedWindowDays.value = loadedAnalytics.window_days
    Object.assign(policyForm, {
      target_pass_rate_percent: loadedPolicy.target_pass_rate_percent,
      window_days: loadedPolicy.window_days,
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
    })
    policy.value = saved
    Object.assign(policyForm, {
      target_pass_rate_percent: saved.target_pass_rate_percent,
      window_days: saved.window_days,
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

onMounted(initialize)
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
.trend-surface, .flaky-surface, .failure-surface, .policy-surface { padding: 20px; }
.trend-surface, .flaky-surface { margin-bottom: 18px; }
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
.policy-note { margin: 12px 0 0; color: #8b99a6; font-size: 11px; }
@media (max-width: 1180px) { .metric-grid { grid-template-columns: repeat(2, 1fr); } .lower-grid { grid-template-columns: 1fr; } }
@media (max-width: 760px) { .dimension-filter-controls { grid-template-columns: 1fr; } }
</style>
