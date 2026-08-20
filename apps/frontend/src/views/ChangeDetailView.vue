<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowLeft,
  Check,
  CircleCheck,
  Close,
  Refresh,
  VideoPlay,
} from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { ApiError, api } from '@/api/client'
import type {
  AutomationPackage,
  ChangeDetail,
  Environment,
  Run,
  Target,
} from '@/api/types'
import { auth } from '@/auth'
import {
  changeStatusLabel,
  changeTagType,
  formatDate,
  shortDigest,
} from '@/presentation'

interface DiffRow {
  key: string
  caseCode: string
  changeType: string
  field: string
  before: unknown
  after: unknown
}

const route = useRoute()
const router = useRouter()
const projectId = computed(() => String(route.params.projectId))
const requestId = computed(() => String(route.params.requestId))
const loading = ref(true)
const actionBusy = ref(false)
const detail = ref<ChangeDetail>()
const runs = ref<Run[]>([])

const runDialogVisible = ref(false)
const runKind = ref<'validation' | 'regression'>('validation')
const targets = ref<Target[]>([])
const environments = ref<Environment[]>([])
const packages = ref<AutomationPackage[]>([])
const runForm = reactive({ target_id: '', environment_id: '', automation_package_id: '' })

const reviewVisible = ref(false)
const reviewForm = reactive({ decision: 'APPROVE' as 'APPROVE' | 'REQUEST_CHANGES', comment: '' })

const diffRows = computed<DiffRow[]>(() => {
  const rows: DiffRow[] = []
  for (const item of detail.value?.changes ?? []) {
    for (const field of item.changed_fields) {
      rows.push({
        key: `${item.id}:${field}`,
        caseCode: item.case_code,
        changeType: item.change_type,
        field,
        before: field === '$case' ? item.before : item.before?.[field],
        after: field === '$case' ? item.after : item.after?.[field],
      })
    }
  }
  return rows
})

const validationRun = computed(() =>
  runs.value.find((run) => run.id === detail.value?.validation_run_id),
)
const candidateRuns = computed(() =>
  runs.value
    .filter((run) => run.baseline_id === detail.value?.candidate_baseline_id)
    .sort((left, right) => right.created_at.localeCompare(left.created_at)),
)
const passedRegression = computed(() => candidateRuns.value.find((run) => run.status === 'PASSED'))
const workflowStep = computed(() => {
  if (detail.value?.status === 'PUBLISHED') return 4
  if (detail.value?.status === 'CANDIDATE') return 3
  if (detail.value?.status === 'IN_REVIEW') return 2
  if (detail.value?.validation_status === 'PASSED' || detail.value?.validation_status === 'NOT_REQUIRED') {
    return 1
  }
  return 0
})

function renderValue(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'string') return value
  return JSON.stringify(value, null, 2)
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const [change, runList] = await Promise.all([
      api.change(projectId.value, requestId.value),
      api.runs(projectId.value),
    ])
    detail.value = change
    runs.value = runList.items
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : '变更申请加载失败')
  } finally {
    loading.value = false
  }
}

async function loadTargetResources(targetId: string): Promise<void> {
  const [environmentList, packageList] = await Promise.all([
    api.environments(projectId.value, targetId),
    api.packages(projectId.value, targetId),
  ])
  environments.value = environmentList
  packages.value = packageList.filter((item) => item.status === 'ACTIVE')
  runForm.environment_id = environmentList[0]?.id ?? ''
  runForm.automation_package_id = packages.value[0]?.id ?? ''
}

async function openRunDialog(kind: 'validation' | 'regression'): Promise<void> {
  runKind.value = kind
  try {
    targets.value = await api.targets(projectId.value)
    runForm.target_id = targets.value[0]?.id ?? ''
    if (!runForm.target_id) {
      ElMessage.warning('项目尚未配置测试目标')
      return
    }
    await loadTargetResources(runForm.target_id)
    runDialogVisible.value = true
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : '执行资源加载失败')
  }
}

async function startRun(): Promise<void> {
  if (!runForm.target_id || !runForm.environment_id || !runForm.automation_package_id) {
    ElMessage.warning('请选择测试目标、环境和自动化包')
    return
  }
  actionBusy.value = true
  const payload = { ...runForm }
  try {
    const key = `${runKind.value}-${requestId.value}-${crypto.randomUUID()}`
    const run =
      runKind.value === 'validation'
        ? await api.startValidation(projectId.value, requestId.value, payload, key)
        : await api.startRegression(projectId.value, requestId.value, payload, key)
    runDialogVisible.value = false
    ElMessage.success(
      run ? `任务已进入队列：${run.id.slice(0, 8)}` : '此变更无需执行用例验证',
    )
    await load()
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : '任务创建失败')
  } finally {
    actionBusy.value = false
  }
}

async function submitForReview(): Promise<void> {
  actionBusy.value = true
  try {
    detail.value = await api.submitChange(projectId.value, requestId.value)
    ElMessage.success('已提交审批')
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : '提交审批失败')
  } finally {
    actionBusy.value = false
  }
}

function openReview(decision: 'APPROVE' | 'REQUEST_CHANGES'): void {
  reviewForm.decision = decision
  reviewForm.comment = ''
  reviewVisible.value = true
}

async function decide(): Promise<void> {
  if (reviewForm.decision === 'REQUEST_CHANGES' && !reviewForm.comment.trim()) {
    ElMessage.warning('提出修改时必须填写评审意见')
    return
  }
  actionBusy.value = true
  try {
    detail.value = await api.decideChange(projectId.value, requestId.value, {
      decision: reviewForm.decision,
      comment: reviewForm.comment || null,
    })
    reviewVisible.value = false
    ElMessage.success(reviewForm.decision === 'APPROVE' ? '审批通过' : '已退回修改')
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : '审批操作失败')
  } finally {
    actionBusy.value = false
  }
}

async function publish(): Promise<void> {
  const regression = passedRegression.value
  if (!regression) {
    ElMessage.warning('候选基线尚无通过的完整回归')
    return
  }
  try {
    await ElMessageBox.confirm(
      `将 ${detail.value?.candidate_version} 发布为新的只读标准基线。发布后不可修改，是否继续？`,
      '确认发布',
      { confirmButtonText: '确认发布', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  actionBusy.value = true
  try {
    detail.value = await api.publishChange(projectId.value, requestId.value, regression.id)
    ElMessage.success('新标准基线已发布')
    await load()
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : '发布失败')
  } finally {
    actionBusy.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="page-container change-page" v-loading="loading">
    <el-button text class="back-link" @click="router.push(`/projects/${projectId}?view=changes`)">
      <el-icon><ArrowLeft /></el-icon>
      返回变更列表
    </el-button>

    <header class="page-heading change-heading">
      <div>
        <div class="request-id mono">CR · {{ requestId }}</div>
        <h1>{{ detail?.title || '变更申请' }}</h1>
        <p>{{ detail?.reason }}</p>
      </div>
      <div class="heading-status">
        <el-tag v-if="detail" :type="changeTagType(detail.status)" size="large">
          {{ changeStatusLabel(detail.status) }}
        </el-tag>
        <el-button :icon="Refresh" @click="load">刷新状态</el-button>
      </div>
    </header>

    <section class="surface workflow-card">
      <el-steps :active="workflowStep" align-center finish-status="success">
        <el-step title="草稿验证" :description="detail?.validation_status" />
        <el-step title="提交审批" description="提交人不可自审" />
        <el-step title="评审通过" description="形成候选基线" />
        <el-step title="完整回归" description="全部启用用例" />
        <el-step title="正式发布" description="Released 只读" />
      </el-steps>
    </section>

    <div class="detail-grid">
      <main>
        <section class="surface diff-section">
          <div class="toolbar">
            <div>
              <strong>字段级 Diff</strong>
              <span class="section-note">{{ detail?.change_count || 0 }} 条用例变更</span>
            </div>
            <span class="mono muted">{{ detail?.candidate_version }}</span>
          </div>
          <el-table :data="diffRows" row-key="key" empty-text="没有字段变化">
            <el-table-column prop="caseCode" label="用例" width="145" />
            <el-table-column label="类型" width="90">
              <template #default="scope">
                <el-tag effect="plain" size="small">{{ scope.row.changeType }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="field" label="字段" width="150" />
            <el-table-column label="修改前" min-width="240">
              <template #default="scope"><pre class="diff-value before">{{ renderValue(scope.row.before) }}</pre></template>
            </el-table-column>
            <el-table-column label="修改后" min-width="240">
              <template #default="scope"><pre class="diff-value after">{{ renderValue(scope.row.after) }}</pre></template>
            </el-table-column>
          </el-table>
        </section>

        <section class="surface approvals-section">
          <div class="toolbar"><strong>评审记录</strong></div>
          <el-empty v-if="!detail?.approvals.length" description="暂无评审记录" :image-size="72" />
          <div v-else class="approval-list">
            <article v-for="approval in detail.approvals" :key="approval.id">
              <span :class="['decision-mark', approval.decision === 'APPROVE' ? 'pass' : 'reject']">
                <el-icon><Check v-if="approval.decision === 'APPROVE'" /><Close v-else /></el-icon>
              </span>
              <div>
                <strong>{{ approval.decision === 'APPROVE' ? '审批通过' : '要求修改' }}</strong>
                <p>{{ approval.comment || '未填写评审备注' }}</p>
                <small>{{ formatDate(approval.created_at) }} · {{ approval.reviewer_id }}</small>
              </div>
            </article>
          </div>
        </section>
      </main>

      <aside>
        <section class="surface action-card">
          <h3>当前操作</h3>
          <template v-if="detail?.status === 'DRAFT' || detail?.status === 'CHANGES_REQUESTED'">
            <div class="gate-state">
              <span>草稿验证</span>
              <el-tag :type="detail.validation_status === 'PASSED' ? 'success' : 'warning'">
                {{ detail.validation_status }}
              </el-tag>
            </div>
            <el-button
              type="primary"
              :icon="VideoPlay"
              :disabled="detail.validation_status === 'QUEUED' || detail.validation_status === 'RUNNING'"
              @click="openRunDialog('validation')"
            >执行受影响用例</el-button>
            <el-button
              :disabled="
                detail.created_by !== auth.state.user?.id ||
                !['PASSED', 'NOT_REQUIRED'].includes(detail.validation_status)
              "
              :loading="actionBusy"
              @click="submitForReview"
            >提交审批</el-button>
          </template>
          <template v-else-if="detail?.status === 'IN_REVIEW'">
            <p class="action-copy">请结合字段 Diff 和草稿验证结果作出决定。提交人无法审批自己的申请。</p>
            <el-button type="primary" :icon="CircleCheck" @click="openReview('APPROVE')">
              审批通过
            </el-button>
            <el-button :icon="Close" @click="openReview('REQUEST_CHANGES')">要求修改</el-button>
          </template>
          <template v-else-if="detail?.status === 'CANDIDATE'">
            <div class="gate-state">
              <span>完整回归</span>
              <el-tag :type="passedRegression ? 'success' : 'warning'">
                {{ passedRegression ? 'PASSED' : candidateRuns[0]?.status || 'NOT_STARTED' }}
              </el-tag>
            </div>
            <el-button type="primary" :icon="VideoPlay" @click="openRunDialog('regression')">
              执行完整回归
            </el-button>
            <el-button type="success" :disabled="!passedRegression" @click="publish">
              发布标准基线
            </el-button>
          </template>
          <template v-else>
            <el-result icon="success" title="基线已发布" sub-title="该申请与候选内容均已锁定为只读" />
          </template>
        </section>

        <section class="surface metadata-card">
          <h3>版本与证据</h3>
          <dl>
            <div><dt>候选版本</dt><dd>{{ detail?.candidate_version }}</dd></div>
            <div><dt>候选摘要</dt><dd class="mono">{{ detail ? shortDigest(detail.candidate_digest) : '—' }}</dd></div>
            <div><dt>草稿验证 Run</dt><dd class="mono">{{ validationRun?.id || '—' }}</dd></div>
            <div><dt>验证状态</dt><dd>{{ validationRun?.status || detail?.validation_status }}</dd></div>
            <div><dt>创建时间</dt><dd>{{ formatDate(detail?.created_at) }}</dd></div>
            <div><dt>发布时间</dt><dd>{{ formatDate(detail?.published_at) }}</dd></div>
          </dl>
        </section>
      </aside>
    </div>

    <el-dialog
      v-model="runDialogVisible"
      :title="runKind === 'validation' ? '执行受影响用例验证' : '执行候选基线完整回归'"
      width="560px"
    >
      <el-alert
        :title="
          runKind === 'validation'
            ? '仅执行 ADD / MODIFY 中已启用的受影响用例。'
            : '执行候选基线全部启用用例；通过后才能发布。'
        "
        type="info"
        :closable="false"
        show-icon
      />
      <el-form label-position="top" class="run-form">
        <el-form-item label="测试目标" required>
          <el-select v-model="runForm.target_id" style="width: 100%" @change="loadTargetResources">
            <el-option v-for="item in targets" :key="item.id" :label="item.name" :value="item.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="执行环境" required>
          <el-select v-model="runForm.environment_id" style="width: 100%">
            <el-option v-for="item in environments" :key="item.id" :label="item.name" :value="item.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="自动化脚本包" required>
          <el-select v-model="runForm.automation_package_id" style="width: 100%">
            <el-option
              v-for="item in packages"
              :key="item.id"
              :label="`${item.name}@${item.version}`"
              :value="item.id"
            />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="runDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="actionBusy" @click="startRun">创建任务</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="reviewVisible"
      :title="reviewForm.decision === 'APPROVE' ? '审批通过' : '要求修改'"
      width="540px"
    >
      <el-alert
        v-if="reviewForm.decision === 'APPROVE'"
        title="审批通过后将生成候选基线，但仍需完整回归通过才能发布。"
        type="warning"
        :closable="false"
        show-icon
      />
      <el-form label-position="top" class="review-form">
        <el-form-item label="评审意见" :required="reviewForm.decision === 'REQUEST_CHANGES'">
          <el-input v-model="reviewForm.comment" type="textarea" :rows="4" maxlength="4000" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="reviewVisible = false">取消</el-button>
        <el-button
          :type="reviewForm.decision === 'APPROVE' ? 'primary' : 'danger'"
          :loading="actionBusy"
          @click="decide"
        >确认</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.back-link {
  margin: -8px 0 10px -12px;
  color: #66788a;
}

.change-heading {
  align-items: center;
}

.request-id {
  color: #168579;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.06em;
}

.heading-status {
  display: flex;
  align-items: center;
  gap: 10px;
}

.workflow-card {
  margin-bottom: 20px;
  padding: 28px 18px 24px;
}

.detail-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 330px;
  gap: 20px;
  align-items: start;
}

.detail-grid main,
.detail-grid aside {
  display: grid;
  gap: 20px;
}

.detail-grid > main {
  min-width: 0;
}

.diff-section {
  overflow: hidden;
}

.diff-section :deep(.el-table) {
  width: 100%;
}

.section-note {
  margin-left: 10px;
  color: #8795a3;
  font-size: 12px;
  font-weight: 400;
}

.diff-value {
  max-height: 150px;
  margin: 0;
  padding: 8px 10px;
  overflow: auto;
  border-radius: 5px;
  font-family: "SFMono-Regular", Consolas, monospace;
  font-size: 11px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-word;
}

.diff-value.before {
  color: #7c4b4b;
  background: #fff4f3;
}

.diff-value.after {
  color: #18665e;
  background: #eef9f6;
}

.approvals-section .el-empty {
  padding: 24px;
}

.approval-list {
  padding: 0 20px;
}

.approval-list article {
  display: grid;
  grid-template-columns: 34px 1fr;
  gap: 12px;
  padding: 18px 0;
  border-bottom: 1px solid #edf0f2;
}

.approval-list article:last-child {
  border-bottom: 0;
}

.decision-mark {
  display: grid;
  width: 30px;
  height: 30px;
  place-items: center;
  border-radius: 50%;
}

.decision-mark.pass {
  color: #168579;
  background: #e8f7f3;
}

.decision-mark.reject {
  color: #c45656;
  background: #fff0ef;
}

.approval-list strong {
  color: #263d52;
  font-size: 13px;
}

.approval-list p {
  margin: 6px 0;
  color: #637588;
  font-size: 13px;
}

.approval-list small {
  color: #91a0ae;
  font-size: 10px;
}

.action-card,
.metadata-card {
  padding: 20px;
}

.action-card h3,
.metadata-card h3 {
  margin: 0 0 18px;
  color: #233b53;
  font-size: 14px;
}

.action-card > .el-button {
  width: 100%;
  margin: 0 0 9px;
}

.gate-state {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
  padding: 11px 12px;
  border-radius: 7px;
  background: #f4f7f9;
  color: #53687c;
  font-size: 12px;
}

.action-copy {
  margin: 0 0 16px;
  color: #6c7f91;
  font-size: 12px;
  line-height: 1.7;
}

.action-card :deep(.el-result) {
  padding: 8px 0;
}

.metadata-card dl {
  margin: 0;
}

.metadata-card dl div {
  display: grid;
  grid-template-columns: 92px 1fr;
  gap: 8px;
  padding: 10px 0;
  border-bottom: 1px solid #edf0f2;
}

.metadata-card dl div:last-child {
  border-bottom: 0;
}

.metadata-card dt {
  color: #8795a3;
  font-size: 11px;
}

.metadata-card dd {
  overflow: hidden;
  margin: 0;
  color: #42576b;
  font-size: 11px;
  text-overflow: ellipsis;
}

.run-form,
.review-form {
  margin-top: 22px;
}
</style>
