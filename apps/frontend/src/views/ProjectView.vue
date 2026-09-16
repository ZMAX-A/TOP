<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowLeft,
  Box,
  DataAnalysis,
  DocumentAdd,
  Lock,
  MagicStick,
  Refresh,
  Setting,
  VideoPlay,
} from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'

import { ApiError, api } from '@/api/client'
import type {
  Baseline,
  CaseBaseline,
  ChangeSummary,
  Project,
  Run,
} from '@/api/types'
import {
  changeStatusLabel,
  changeTagType,
  formatDate,
  runTagType,
  shortDigest,
} from '@/presentation'

const route = useRoute()
const router = useRouter()
const projectId = computed(() => String(route.params.projectId))
const loading = ref(true)
const project = ref<Project>()
const baselines = ref<Baseline[]>([])
const changes = ref<ChangeSummary[]>([])
const runs = ref<Run[]>([])
const activeTab = ref(typeof route.query.view === 'string' ? route.query.view : 'overview')

const createVisible = ref(false)
const createBusy = ref(false)
const baselineDocument = ref<CaseBaseline>()
const draftForm = reactive({
  base_baseline_id: '',
  candidate_version: '',
  title: '',
  reason: '',
  case_id: '',
  new_title: '',
})

const selectedCase = computed(() =>
  baselineDocument.value?.cases.find((item) => item.case_id === draftForm.case_id),
)
const latestBaseline = computed(() => baselines.value[baselines.value.length - 1])
const recentRuns = computed(() => runs.value.slice(0, 10))
const latestRun = computed(() => recentRuns.value[0])
const recentRunStats = computed(() => {
  const items = recentRuns.value
  const passed = items.filter((item) => item.status === 'PASSED').length
  const infra = items.filter((item) => item.status === 'INFRA_ERROR').length
  const failed = items.filter((item) =>
    ['FAILED', 'TIMED_OUT', 'CANCELED'].includes(item.status),
  ).length
  const active = items.filter((item) =>
    ['QUEUED', 'PREPARING', 'RUNNING'].includes(item.status),
  ).length
  return {
    total: items.length,
    passed,
    infra,
    failed,
    active,
    other: Math.max(0, items.length - passed - infra - failed - active),
  }
})
const recentPassRate = computed(() =>
  recentRunStats.value.total
    ? Math.round((recentRunStats.value.passed / recentRunStats.value.total) * 100)
    : 0,
)
const runDistribution = computed(() => [
  { key: 'passed', label: '通过', count: recentRunStats.value.passed },
  { key: 'failed', label: '失败', count: recentRunStats.value.failed },
  { key: 'infra', label: '基础设施异常', count: recentRunStats.value.infra },
  { key: 'active', label: '执行中', count: recentRunStats.value.active },
  { key: 'other', label: '其他', count: recentRunStats.value.other },
])
const copilotSignal = computed(() => {
  const stats = recentRunStats.value
  if (!stats.total) {
    return {
      tone: 'neutral',
      eyebrow: '等待运行数据',
      title: '尚无可分析的测试运行',
      summary: '创建首个 Run 后，AI 助手将基于平台证据提供统计与排查建议。',
    }
  }
  if (stats.active) {
    return {
      tone: 'active',
      eyebrow: '运行观察中',
      title: `${stats.active} 次运行仍在执行`,
      summary: '当前结论仍可能变化，建议等待运行结束后再进行完整的失败归因。',
    }
  }
  if (stats.infra) {
    return {
      tone: 'warning',
      eyebrow: '环境风险优先',
      title: `${stats.infra} 次基础设施异常`,
      summary: '建议优先检查执行资源、浏览器启动和容器环境，再判断业务用例是否回归。',
    }
  }
  if (stats.failed) {
    return {
      tone: 'danger',
      eyebrow: '回归风险',
      title: `${stats.failed} 次运行未通过`,
      summary: '可让 AI 按断言、脚本和环境证据聚类，快速缩小需要人工排查的范围。',
    }
  }
  return {
    tone: 'stable',
    eyebrow: '近期稳定',
    title: '最近运行全部通过',
    summary: '当前未发现失败运行，建议继续关注通过率趋势和基线变更后的首次全量回归。',
  }
})

function distributionWidth(count: number): string {
  const total = Math.max(1, recentRunStats.value.total)
  return `${(count / total) * 100}%`
}

function runDuration(item: Run): string {
  if (!item.started_at) return item.status === 'QUEUED' ? '等待执行' : '未开始'
  if (!item.finished_at) return '执行中'
  const durationSeconds = Math.max(
    0,
    Math.round((Date.parse(item.finished_at) - Date.parse(item.started_at)) / 1000),
  )
  if (durationSeconds < 60) return `${durationSeconds} 秒`
  const minutes = Math.floor(durationSeconds / 60)
  const seconds = durationSeconds % 60
  return `${minutes} 分 ${seconds} 秒`
}

function openRun(id: string): void {
  void router.push({ name: 'run-detail', params: { projectId: projectId.value, runId: id } })
}

function openAiAssistant(runId = ''): void {
  void router.push({
    name: 'ai-assistant',
    query: {
      projectId: projectId.value,
      ...(runId ? { runId } : {}),
    },
  })
}

function nextVersion(version: string): string {
  const match = /^case-v(\d+)\.(\d+)\.(\d+)$/.exec(version)
  if (!match) return `${version}-next`
  return `case-v${match[1]}.${match[2]}.${Number(match[3]) + 1}`
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const [projectList, baselineList, changeList, runList] = await Promise.all([
      api.projects(),
      api.baselines(projectId.value),
      api.changes(projectId.value),
      api.runs(projectId.value),
    ])
    project.value = projectList.find((item) => item.id === projectId.value)
    baselines.value = baselineList
    changes.value = changeList
    runs.value = runList.items
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : '项目数据加载失败')
  } finally {
    loading.value = false
  }
}

async function selectBaseline(baselineId: string): Promise<void> {
  baselineDocument.value = await api.baseline(projectId.value, baselineId)
  draftForm.case_id = baselineDocument.value.cases[0]?.case_id ?? ''
  draftForm.new_title = baselineDocument.value.cases[0]?.title ?? ''
}

async function openCreate(): Promise<void> {
  const latest = latestBaseline.value
  if (!latest) {
    ElMessage.warning('项目还没有可派生的 Released 基线')
    return
  }
  Object.assign(draftForm, {
    base_baseline_id: latest.baseline_id,
    candidate_version: nextVersion(latest.version),
    title: '',
    reason: '',
    case_id: '',
    new_title: '',
  })
  try {
    await selectBaseline(latest.baseline_id)
    createVisible.value = true
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : '基线内容加载失败')
  }
}

function selectCase(caseId: string): void {
  const selected = baselineDocument.value?.cases.find((item) => item.case_id === caseId)
  draftForm.new_title = selected?.title ?? ''
}

async function createDraft(): Promise<void> {
  const selected = selectedCase.value
  if (
    !selected ||
    !draftForm.title.trim() ||
    !draftForm.reason.trim() ||
    !draftForm.candidate_version.trim()
  ) {
    ElMessage.warning('请完整填写候选版本、申请标题、原因和用例')
    return
  }
  if (!draftForm.new_title.trim() || draftForm.new_title.trim() === selected.title) {
    ElMessage.warning('请输入与当前标题不同的新标题')
    return
  }
  createBusy.value = true
  try {
    const created = await api.createChange(projectId.value, {
      base_baseline_id: draftForm.base_baseline_id,
      candidate_version: draftForm.candidate_version,
      title: draftForm.title,
      reason: draftForm.reason,
      changes: [
        {
          change_type: 'MODIFY',
          case_id: selected.case_id,
          case: { ...selected, title: draftForm.new_title.trim() },
        },
      ],
    })
    createVisible.value = false
    ElMessage.success('变更草稿已创建，下一步执行受影响用例验证')
    await router.push({
      name: 'change-detail',
      params: { projectId: projectId.value, requestId: created.id },
    })
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : '变更草稿创建失败')
  } finally {
    createBusy.value = false
  }
}

function switchTab(name: string | number): void {
  void router.replace({ query: name === 'overview' ? {} : { view: String(name) } })
}

onMounted(load)
</script>

<template>
  <div class="page-container" v-loading="loading">
    <div class="page-sticky-header">
      <el-button text class="back-link" @click="router.push('/projects')">
        <el-icon><ArrowLeft /></el-icon>
        返回项目空间
      </el-button>
      <header class="page-heading project-heading">
        <div class="project-summary">
          <div class="project-key">{{ project?.key }}</div>
          <h1>{{ project?.name || '项目' }}</h1>
          <p>{{ project?.description || '标准用例治理与自动化执行控制面' }}</p>
        </div>
        <div class="heading-actions">
          <el-button :icon="Refresh" @click="load">刷新</el-button>
          <el-button
            :icon="Box"
            @click="router.push({ name: 'project-automation-packages', params: { projectId } })"
          >自动化包</el-button>
          <el-button
            :icon="DataAnalysis"
            @click="router.push({ name: 'project-quality', params: { projectId } })"
          >质量分析</el-button>
          <el-button
            :icon="Setting"
            @click="router.push({ name: 'project-settings', params: { projectId } })"
          >项目设置</el-button>
          <el-button type="primary" :icon="DocumentAdd" @click="openCreate">新建变更申请</el-button>
        </div>
      </header>
    </div>

    <el-tabs v-model="activeTab" class="project-tabs" @tab-change="switchTab">
      <el-tab-pane label="概览" name="overview">
        <div class="metric-grid">
          <article class="metric-card primary-metric">
            <span class="metric-icon teal"><DataAnalysis /></span>
            <div>
              <strong>{{ recentRunStats.total ? `${recentPassRate}%` : '—' }}</strong>
              <span>最近运行通过率</span>
            </div>
            <small>{{ recentRunStats.passed }} / {{ recentRunStats.total }} 次通过</small>
          </article>
          <article class="metric-card">
            <span class="metric-icon blue"><VideoPlay /></span>
            <div><strong>{{ recentRunStats.total }}</strong><span>最近测试运行</span></div>
            <small>{{ recentRunStats.active }} 次仍在执行</small>
          </article>
          <article class="metric-card">
            <span class="metric-icon amber"><DocumentAdd /></span>
            <div>
              <strong>{{ changes.filter((item) => item.status !== 'PUBLISHED').length }}</strong>
              <span>进行中变更</span>
            </div>
            <small>{{ changes.length }} 个历史申请</small>
          </article>
          <article class="metric-card">
            <span class="metric-icon violet"><Lock /></span>
            <div><strong>{{ baselines.length }}</strong><span>Released 基线</span></div>
            <small>{{ latestBaseline?.version || '尚未发布' }}</small>
          </article>
        </div>

        <div class="overview-dashboard">
          <section class="surface run-insight-panel">
            <div class="toolbar insight-toolbar">
              <div>
                <strong>最近 10 次测试运行</strong>
                <span class="section-note">按平台 Run 统计，不是单条用例执行次数</span>
              </div>
              <el-button text @click="activeTab = 'runs'">查看运行记录</el-button>
            </div>
            <div class="run-distribution" aria-label="最近运行状态分布">
              <span
                v-for="item in runDistribution"
                :key="item.key"
                :class="`distribution-${item.key}`"
                :style="{ width: distributionWidth(item.count) }"
              />
            </div>
            <div class="distribution-legend">
              <span v-for="item in runDistribution" :key="item.key">
                <i :class="`distribution-${item.key}`" />
                {{ item.label }} <strong>{{ item.count }}</strong>
              </span>
            </div>
            <div v-if="recentRuns.length" class="recent-run-list">
              <button
                v-for="item in recentRuns.slice(0, 4)"
                :key="item.id"
                type="button"
                class="recent-run-row"
                @click="openRun(item.id)"
              >
                <span class="run-status-dot" :class="`status-${item.status.toLowerCase()}`" />
                <span class="run-identity">
                  <strong class="mono">{{ item.id.slice(0, 8) }}</strong>
                  <small>{{ formatDate(item.created_at) }}</small>
                </span>
                <el-tag :type="runTagType(item.status)" effect="light" size="small">
                  {{ item.status }}
                </el-tag>
                <span>{{ item.case_count }} 条用例</span>
                <span>{{ runDuration(item) }}</span>
              </button>
            </div>
            <div v-else class="run-empty-state">暂无测试运行记录</div>
          </section>

          <aside class="surface copilot-card">
            <div class="copilot-heading">
              <span class="copilot-mark"><el-icon><MagicStick /></el-icon></span>
              <div>
                <small>TOP AI · Quality Copilot</small>
                <strong>智能质量助手</strong>
              </div>
              <span class="copilot-mode">只读</span>
            </div>
            <div class="copilot-signal" :class="`signal-${copilotSignal.tone}`">
              <small>{{ copilotSignal.eyebrow }}</small>
              <strong>{{ copilotSignal.title }}</strong>
              <p>{{ copilotSignal.summary }}</p>
            </div>
            <div class="copilot-actions">
              <el-button class="copilot-primary" type="primary" @click="openAiAssistant()">
                项目全局分析
              </el-button>
              <el-button v-if="latestRun" @click="openAiAssistant(latestRun.id)">
                分析最新 Run
              </el-button>
            </div>
            <button
              v-if="latestRun"
              type="button"
              class="latest-run-link"
              @click="openRun(latestRun.id)"
            >
              <span>
                <small>Latest Run</small>
                <strong class="mono">{{ latestRun.id.slice(0, 8) }}</strong>
              </span>
              <el-tag :type="runTagType(latestRun.status)" effect="light" size="small">
                {{ latestRun.status }}
              </el-tag>
            </button>
          </aside>
        </div>

        <section class="surface overview-section">
          <div class="toolbar">
            <div><strong>最近变更</strong><span class="section-note">字段级 Diff 与审批状态</span></div>
            <el-button text @click="activeTab = 'changes'">查看全部</el-button>
          </div>
          <el-table :data="changes.slice(0, 5)" empty-text="暂无变更申请">
            <el-table-column label="变更申请" min-width="280">
              <template #default="scope">
                <span
                  class="table-link"
                  @click="router.push(`/projects/${projectId}/changes/${scope.row.id}`)"
                >{{ scope.row.title }}</span>
                <div class="muted mono">{{ scope.row.candidate_version }}</div>
              </template>
            </el-table-column>
            <el-table-column prop="change_count" label="变更数" width="90" />
            <el-table-column label="状态" width="125">
              <template #default="scope">
                <el-tag :type="changeTagType(scope.row.status)" effect="light">
                  {{ changeStatusLabel(scope.row.status) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="更新时间" width="170">
              <template #default="scope">{{ formatDate(scope.row.updated_at) }}</template>
            </el-table-column>
          </el-table>
        </section>
      </el-tab-pane>

      <el-tab-pane label="标准基线" name="baselines">
        <section class="surface">
          <div class="toolbar">
            <div><strong>只读标准基线</strong><span class="section-note">Released 后不可原地覆盖</span></div>
          </div>
          <el-table :data="baselines" empty-text="暂无已发布基线">
            <el-table-column prop="version" label="版本" min-width="150" />
            <el-table-column label="用例" width="140">
              <template #default="scope">
                {{ scope.row.enabled_case_count }} / {{ scope.row.case_count }} 启用
              </template>
            </el-table-column>
            <el-table-column prop="source_kind" label="来源" width="160" />
            <el-table-column label="摘要" min-width="220">
              <template #default="scope"><code>{{ shortDigest(scope.row.digest) }}</code></template>
            </el-table-column>
            <el-table-column label="发布时间" width="180">
              <template #default="scope">{{ formatDate(scope.row.created_at) }}</template>
            </el-table-column>
          </el-table>
        </section>
      </el-tab-pane>

      <el-tab-pane label="变更申请" name="changes">
        <section class="surface">
          <div class="toolbar">
            <div><strong>用例变更工作流</strong><span class="section-note">草稿 → 验证 → 审批 → 全回归 → 发布</span></div>
            <el-button type="primary" :icon="DocumentAdd" @click="openCreate">新建变更</el-button>
          </div>
          <el-table :data="changes" empty-text="暂无变更申请">
            <el-table-column label="申请" min-width="300">
              <template #default="scope">
                <span
                  class="table-link"
                  @click="router.push(`/projects/${projectId}/changes/${scope.row.id}`)"
                >{{ scope.row.title }}</span>
                <div class="muted">{{ scope.row.reason }}</div>
              </template>
            </el-table-column>
            <el-table-column prop="candidate_version" label="候选版本" width="145" />
            <el-table-column prop="change_count" label="变更数" width="90" />
            <el-table-column label="草稿验证" width="130">
              <template #default="scope"><code>{{ scope.row.validation_status }}</code></template>
            </el-table-column>
            <el-table-column label="状态" width="120">
              <template #default="scope">
                <el-tag :type="changeTagType(scope.row.status)">
                  {{ changeStatusLabel(scope.row.status) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="更新时间" width="175">
              <template #default="scope">{{ formatDate(scope.row.updated_at) }}</template>
            </el-table-column>
          </el-table>
        </section>
      </el-tab-pane>

      <el-tab-pane label="运行记录" name="runs">
        <section class="surface">
          <div class="toolbar">
            <div><strong>可追溯运行</strong><span class="section-note">Snapshot 与 Result 均不可变</span></div>
            <el-button
              type="primary"
              @click="router.push({ name: 'project-runs', params: { projectId } })"
            >进入运行运营</el-button>
          </div>
          <el-table :data="runs" empty-text="暂无运行记录">
            <el-table-column label="Run ID" min-width="260">
              <template #default="scope">
                <span
                  class="table-link mono"
                  @click="router.push({ name: 'run-detail', params: { projectId, runId: scope.row.id } })"
                >{{ scope.row.id }}</span>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="125">
              <template #default="scope">
                <el-tag :type="runTagType(scope.row.status)" class="status-tag">
                  {{ scope.row.status }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="case_count" label="用例数" width="90" />
            <el-table-column label="快照摘要" min-width="220">
              <template #default="scope"><code>{{ shortDigest(scope.row.snapshot_digest) }}</code></template>
            </el-table-column>
            <el-table-column label="创建时间" width="175">
              <template #default="scope">{{ formatDate(scope.row.created_at) }}</template>
            </el-table-column>
          </el-table>
        </section>
      </el-tab-pane>
    </el-tabs>

    <el-dialog v-model="createVisible" title="创建用例变更草稿" width="660px" destroy-on-close>
      <el-alert
        title="首个前端闭环支持修改单条用例标题；后端协议已支持 ADD / MODIFY / DELETE。"
        type="info"
        :closable="false"
        show-icon
      />
      <el-form label-position="top" class="draft-form">
        <div class="form-grid">
          <el-form-item label="基础 Released 基线" required>
            <el-select
              v-model="draftForm.base_baseline_id"
              style="width: 100%"
              @change="selectBaseline"
            >
              <el-option
                v-for="baseline in baselines"
                :key="baseline.baseline_id"
                :label="`${baseline.version} · ${baseline.enabled_case_count} 条启用`"
                :value="baseline.baseline_id"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="候选版本" required>
            <el-input v-model="draftForm.candidate_version" placeholder="case-v1.0.2" />
            <p class="form-field-hint">必填；格式必须为 case-v主版本.次版本.修订号，例如 case-v1.0.2。</p>
          </el-form-item>
        </div>
        <el-form-item label="变更申请标题" required>
          <el-input v-model="draftForm.title" maxlength="200" show-word-limit />
          <p class="form-field-hint">必填；1–200 个字符，不能只包含空格。</p>
        </el-form-item>
        <el-form-item label="变更原因" required>
          <el-input v-model="draftForm.reason" type="textarea" :rows="3" maxlength="4000" show-word-limit />
          <p class="form-field-hint">必填；1–4000 个字符，请说明修改目的和影响。</p>
        </el-form-item>
        <el-form-item label="选择用例" required>
          <el-select
            v-model="draftForm.case_id"
            filterable
            style="width: 100%"
            @change="selectCase"
          >
            <el-option
              v-for="item in baselineDocument?.cases"
              :key="item.case_id"
              :label="`${item.case_code} · ${item.title}`"
              :value="item.case_id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="新标题" required>
          <el-input v-model="draftForm.new_title" maxlength="200" show-word-limit />
          <div class="current-value">当前：{{ selectedCase?.title }}</div>
          <p class="form-field-hint">必填；1–200 个字符，且必须与当前标题不同。</p>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="createBusy" @click="createDraft">创建草稿</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.project-heading {
  align-items: center;
  display: grid;
  grid-template-columns: minmax(280px, 0.72fr) minmax(660px, 1.28fr);
  gap: 20px;
  margin-bottom: 0;
}

.project-summary {
  min-width: 0;
}

.project-summary h1 {
  margin: 5px 0 3px;
  font-size: 26px;
}

.project-summary p {
  overflow: hidden;
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.project-key {
  color: var(--top-accent-600);
  font-family: "SFMono-Regular", Consolas, monospace;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.heading-actions {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 8px;
  padding: 8px;
  border: 1px solid var(--top-border);
  border-radius: var(--top-radius-lg);
  background: rgb(255 255 255 / 72%);
  box-shadow: var(--top-shadow-1);
}

.heading-actions .el-button {
  width: 100%;
  min-width: 0;
  height: 38px;
  margin-left: 0;
  padding: 0 10px;
  font-weight: 650;
}

.project-tabs :deep(.el-tabs__header) {
  margin-top: 14px;
  margin-bottom: 22px;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
  margin-bottom: 20px;
}

.metric-card {
  display: grid;
  min-width: 0;
  grid-template-columns: 44px minmax(0, 1fr);
  gap: 12px;
  align-items: center;
  padding: 18px;
  border: 1px solid var(--top-border);
  border-radius: var(--top-radius-lg);
  background: var(--top-surface);
  box-shadow: var(--top-shadow-1);
  transition:
    border-color var(--top-duration-fast) ease,
    transform var(--top-duration-fast) ease,
    box-shadow var(--top-duration-fast) ease;
}

.metric-card:hover {
  border-color: var(--top-border-strong);
  box-shadow: 0 12px 30px rgb(16 42 67 / 7%);
  transform: translateY(-1px);
}

.metric-card.primary-metric {
  border-color: rgb(15 130 119 / 22%);
  background: linear-gradient(145deg, #fff, rgb(223 246 241 / 52%));
}

.metric-icon {
  display: grid;
  width: 44px;
  height: 44px;
  place-items: center;
  border-radius: var(--top-radius-md);
}

.metric-icon svg {
  width: 21px;
}

.metric-icon.teal {
  color: #127c70;
  background: #e8f7f3;
}

.metric-icon.amber {
  color: #a56813;
  background: #fff4dc;
}

.metric-icon.blue {
  color: #626970;
  background: #eff0ed;
}

.metric-icon.violet {
  color: var(--top-ai-600);
  background: var(--top-ai-100);
}

.metric-card strong,
.metric-card span {
  display: block;
}

.metric-card strong {
  color: var(--top-text-strong);
  font-size: 24px;
  letter-spacing: -0.02em;
}

.metric-card span {
  margin-top: 2px;
  color: var(--top-text-muted);
  font-size: 12px;
}

.metric-card small {
  grid-column: 2;
  overflow: hidden;
  margin-top: -7px;
  color: var(--top-text-faint);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.overview-dashboard {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 290px;
  gap: 16px;
  margin-bottom: 20px;
}

.run-insight-panel,
.copilot-card {
  min-width: 0;
  overflow: hidden;
}

.insight-toolbar {
  padding: 16px 18px;
}

.run-distribution {
  display: flex;
  height: 10px;
  margin: 20px 18px 12px;
  overflow: hidden;
  border-radius: 999px;
  background: var(--top-surface-muted);
}

.run-distribution > span {
  min-width: 0;
  transition: width var(--top-duration-base) ease;
}

.distribution-passed {
  background: #1d9a78;
}

.distribution-failed {
  background: #db5560;
}

.distribution-infra {
  background: #d68a2f;
}

.distribution-active {
  background: #748094;
}

.distribution-other {
  background: #a7b2bd;
}

.distribution-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 7px 16px;
  padding: 0 18px 16px;
  color: var(--top-text-muted);
  font-size: 10px;
}

.distribution-legend span {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.distribution-legend i {
  width: 7px;
  height: 7px;
  border-radius: 50%;
}

.distribution-legend strong {
  color: var(--top-text);
}

.recent-run-list {
  border-top: 1px solid var(--top-border);
}

.recent-run-row {
  display: grid;
  width: 100%;
  grid-template-columns: 8px minmax(130px, 1fr) 112px 90px 86px;
  gap: 10px;
  align-items: center;
  padding: 11px 18px;
  border: 0;
  border-bottom: 1px solid var(--top-border);
  color: var(--top-text-muted);
  background: transparent;
  font: inherit;
  font-size: 10px;
  text-align: left;
  cursor: pointer;
  transition: background var(--top-duration-fast) ease;
}

.recent-run-row:last-child {
  border-bottom: 0;
}

.recent-run-row:hover {
  background: var(--top-surface-subtle);
}

.run-status-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #a7b2bd;
}

.run-status-dot.status-passed {
  background: #1d9a78;
  box-shadow: 0 0 0 3px rgb(29 154 120 / 10%);
}

.run-status-dot.status-failed,
.run-status-dot.status-timed_out,
.run-status-dot.status-canceled {
  background: #db5560;
}

.run-status-dot.status-infra_error {
  background: #d68a2f;
}

.run-status-dot.status-running,
.run-status-dot.status-preparing,
.run-status-dot.status-queued {
  background: #748094;
  box-shadow: 0 0 0 3px rgb(116 128 148 / 10%);
}

.run-identity {
  display: grid;
  min-width: 0;
  gap: 2px;
}

.run-identity strong {
  color: var(--top-text);
  font-size: 11px;
}

.run-identity small {
  overflow: hidden;
  color: var(--top-text-faint);
  font-size: 9px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.copilot-card {
  position: relative;
  padding: 18px;
  background:
    radial-gradient(circle at 100% 0%, rgb(109 85 199 / 14%), transparent 13rem),
    var(--top-surface);
}

.copilot-card::after {
  position: absolute;
  right: -42px;
  top: -54px;
  width: 150px;
  height: 150px;
  border: 1px solid rgb(109 85 199 / 9%);
  border-radius: 50%;
  content: "";
}

.copilot-heading {
  position: relative;
  z-index: 1;
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 16px;
}

.copilot-mark {
  display: grid;
  width: 36px;
  height: 36px;
  flex: 0 0 36px;
  place-items: center;
  border-radius: 11px;
  color: #fff;
  background: linear-gradient(145deg, var(--top-ai-600), var(--top-ai-500));
  box-shadow: 0 8px 20px rgb(88 66 174 / 18%);
}

.copilot-heading > div {
  display: grid;
  min-width: 0;
  flex: 1;
  gap: 2px;
}

.copilot-heading small {
  color: var(--top-ai-600);
  font-size: 8px;
  font-weight: 800;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.copilot-heading strong {
  color: var(--top-text-strong);
  font-size: 14px;
}

.copilot-mode {
  padding: 3px 7px;
  border: 1px solid rgb(109 85 199 / 15%);
  border-radius: 999px;
  color: var(--top-ai-700);
  background: rgb(239 235 253 / 72%);
  font-size: 8px;
  font-weight: 750;
}

.copilot-signal {
  position: relative;
  z-index: 1;
  display: grid;
  gap: 5px;
  padding: 14px;
  border: 1px solid rgb(109 85 199 / 12%);
  border-radius: var(--top-radius-md);
  background: rgb(250 249 253 / 86%);
}

.copilot-signal::before {
  position: absolute;
  inset: 13px auto 13px 0;
  width: 3px;
  border-radius: 0 4px 4px 0;
  background: var(--top-ai-600);
  content: "";
}

.copilot-signal.signal-warning::before {
  background: var(--top-warning);
}

.copilot-signal.signal-danger::before {
  background: var(--top-danger);
}

.copilot-signal.signal-active::before {
  background: var(--top-info);
}

.copilot-signal.signal-stable::before {
  background: var(--top-success);
}

.copilot-signal small {
  color: var(--top-text-faint);
  font-size: 9px;
  font-weight: 750;
  letter-spacing: 0.06em;
}

.copilot-signal strong {
  color: var(--top-text-strong);
  font-size: 17px;
  line-height: 1.35;
}

.copilot-signal p {
  margin: 1px 0 0;
  color: var(--top-text-muted);
  font-size: 10px;
  line-height: 1.6;
}

.copilot-actions {
  position: relative;
  z-index: 1;
  display: grid;
  grid-template-columns: 1fr;
  gap: 8px;
  margin-top: 12px;
}

.copilot-actions .el-button {
  width: 100%;
  margin-left: 0;
}

.copilot-actions .copilot-primary {
  --el-button-bg-color: var(--top-ai-600);
  --el-button-border-color: var(--top-ai-600);
  --el-button-hover-bg-color: var(--top-ai-700);
  --el-button-hover-border-color: var(--top-ai-700);
  --el-button-active-bg-color: var(--top-ai-700);
  --el-button-active-border-color: var(--top-ai-700);
}

.latest-run-link {
  position: relative;
  z-index: 1;
  display: flex;
  width: 100%;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 12px;
  padding: 10px 11px;
  border: 1px solid var(--top-border);
  border-radius: var(--top-radius-md);
  color: inherit;
  background: rgb(255 255 255 / 76%);
  cursor: pointer;
  text-align: left;
  transition:
    border-color var(--top-duration-fast) ease,
    transform var(--top-duration-fast) ease;
}

.latest-run-link:hover {
  border-color: rgb(109 85 199 / 28%);
  transform: translateY(-1px);
}

.latest-run-link > span {
  display: grid;
  gap: 2px;
}

.latest-run-link small {
  color: var(--top-text-faint);
  font-size: 8px;
  font-weight: 750;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.latest-run-link strong {
  color: var(--top-text);
  font-size: 11px;
}

.run-empty-state,
.latest-run-empty {
  padding: 38px 18px;
  color: var(--top-text-faint);
  font-size: 11px;
  text-align: center;
}

.overview-section {
  margin-top: 0;
}

.section-note {
  margin-left: 12px;
  color: #8a98a6;
  font-size: 12px;
  font-weight: 400;
}

.muted {
  overflow: hidden;
  max-width: 520px;
  margin-top: 4px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.draft-form {
  margin-top: 22px;
}

.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

.current-value {
  margin-top: 5px;
  color: #8392a5;
  font-size: 12px;
}

code {
  color: #4f6478;
  font-size: 11px;
}

@media (max-width: 1240px) {
  .project-heading {
    grid-template-columns: 1fr;
    gap: 12px;
  }

  .heading-actions {
    grid-template-columns: repeat(5, minmax(112px, 1fr));
    overflow-x: auto;
  }

  .metric-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .overview-dashboard {
    grid-template-columns: 1fr;
  }

  .copilot-card {
    display: none;
  }
}
</style>
