<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowLeft,
  Box,
  DataAnalysis,
  DocumentAdd,
  Lock,
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
    <el-button text class="back-link" @click="router.push('/projects')">
      <el-icon><ArrowLeft /></el-icon>
      返回项目空间
    </el-button>
    <header class="page-heading project-heading">
      <div>
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

    <el-tabs v-model="activeTab" class="project-tabs" @tab-change="switchTab">
      <el-tab-pane label="概览" name="overview">
        <div class="metric-grid">
          <article class="metric-card">
            <span class="metric-icon teal"><Lock /></span>
            <div><strong>{{ baselines.length }}</strong><span>Released 基线</span></div>
            <small>{{ latestBaseline?.version || '尚未发布' }}</small>
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
            <span class="metric-icon blue"><VideoPlay /></span>
            <div><strong>{{ runs.length }}</strong><span>最近运行</span></div>
            <small>{{ runs.filter((item) => item.status === 'PASSED').length }} 次通过</small>
          </article>
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
          </el-form-item>
        </div>
        <el-form-item label="变更申请标题" required>
          <el-input v-model="draftForm.title" maxlength="200" show-word-limit />
        </el-form-item>
        <el-form-item label="变更原因" required>
          <el-input v-model="draftForm.reason" type="textarea" :rows="3" maxlength="4000" />
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
.back-link {
  margin: -8px 0 10px -12px;
  color: #66788a;
}

.project-heading {
  align-items: center;
}

.project-key {
  color: #168579;
  font-family: "SFMono-Regular", Consolas, monospace;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.heading-actions {
  display: flex;
  gap: 8px;
}

.project-tabs :deep(.el-tabs__header) {
  margin-bottom: 22px;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
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

.metric-icon.amber {
  color: #a56813;
  background: #fff4dc;
}

.metric-icon.blue {
  color: #2d68a1;
  background: #eaf3fb;
}

.metric-card strong,
.metric-card span {
  display: block;
}

.metric-card strong {
  color: #17324d;
  font-size: 25px;
}

.metric-card span {
  margin-top: 2px;
  color: #64778a;
  font-size: 12px;
}

.metric-card small {
  color: #8c9aa7;
  font-size: 11px;
}

.overview-section {
  margin-top: 8px;
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
</style>
