<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowLeft,
  Box,
  CircleCheck,
  CircleClose,
  Plus,
  Refresh,
  VideoPlay,
  Warning,
} from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'

import { ApiError, api } from '@/api/client'
import type {
  AutomationPackage,
  AutomationPackageSupplyChainEnvelope,
  AutomationPackageSupplyChainVerification,
  Baseline,
  Environment,
  Project,
  ProjectMember,
  Run,
  Target,
} from '@/api/types'
import { auth } from '@/auth'
import { formatDate, runTagType, shortDigest } from '@/presentation'

type RetirementAction = 'DEPRECATE' | 'REVOKE'

const route = useRoute()
const router = useRouter()
const projectId = computed(() => String(route.params.projectId))
const loading = ref(true)
const actionBusy = ref(false)
const project = ref<Project>()
const members = ref<ProjectMember[]>([])
const targets = ref<Target[]>([])
const baselines = ref<Baseline[]>([])
const environments = ref<Environment[]>([])
const packages = ref<AutomationPackage[]>([])
const packageRuns = ref<Run[]>([])
const supplyChainVerifications = ref<AutomationPackageSupplyChainVerification[]>([])
const supplyChainEnvelopes = ref<AutomationPackageSupplyChainEnvelope[]>([])
const selectedTargetId = ref('')
const selectedPackageId = ref('')

const currentMember = computed(() =>
  members.value.find((item) => item.user_id === auth.state.user?.id),
)
const canManage = computed(
  () =>
    auth.state.user?.system_role === 'SYSTEM_ADMIN' ||
    currentMember.value?.role === 'PROJECT_ADMIN',
)
const webTargets = computed(() => targets.value.filter((item) => item.target_type === 'WEB'))
const selectedTarget = computed(() =>
  targets.value.find((item) => item.id === selectedTargetId.value),
)
const displayPackages = computed(() => [...packages.value].reverse())
const selectedPackage = computed(() =>
  packages.value.find((item) => item.id === selectedPackageId.value),
)
const eligibleValidationRuns = computed(() =>
  packageRuns.value.filter((item) => item.status === 'PASSED' && item.result_digest),
)
const currentSupplyChainVerification = computed(() => supplyChainVerifications.value[0])
const packageCounts = computed(() => ({
  total: packages.value.length,
  active: packages.value.filter((item) => item.status === 'ACTIVE').length,
  draft: packages.value.filter((item) => item.status === 'DRAFT').length,
  admitted: packages.value.filter((item) => item.supply_chain_status === 'VERIFIED').length,
}))

const draftVisible = ref(false)
const draftForm = reactive({
  name: '',
  version: '',
  digest: 'sha256:',
  runner_type: 'WEB_PLAYWRIGHT' as const,
  image_repository: '',
  supersedes_id: '',
})

const validationVisible = ref(false)
const validationForm = reactive({ environment_id: '', baseline_id: '' })

const activationVisible = ref(false)
const activationForm = reactive({ validation_run_id: '' })

const retirementVisible = ref(false)
const retirementAction = ref<RetirementAction>('DEPRECATE')
const retirementForm = reactive({ reason: '' })

const packageStatusLabels: Record<AutomationPackage['status'], string> = {
  DRAFT: '草稿',
  ACTIVE: '已激活',
  DEPRECATED: '已弃用',
  REVOKED: '已吊销',
}

const supplyChainStatusLabels: Record<AutomationPackage['supply_chain_status'], string> = {
  LEGACY: '历史兼容',
  PENDING: '待供应链验证',
  VERIFIED: '已准入',
  REJECTED: '已拒绝',
}

function report(error: unknown, fallback: string): void {
  ElMessage.error(error instanceof ApiError ? error.message : fallback)
}

function packageTagType(
  status: AutomationPackage['status'],
): 'success' | 'warning' | 'danger' | 'info' {
  if (status === 'ACTIVE') return 'success'
  if (status === 'DRAFT') return 'warning'
  if (status === 'REVOKED') return 'danger'
  return 'info'
}

function supplyChainTagType(
  status: AutomationPackage['supply_chain_status'],
): 'success' | 'warning' | 'danger' | 'info' {
  if (status === 'VERIFIED') return 'success'
  if (status === 'REJECTED') return 'danger'
  if (status === 'PENDING') return 'warning'
  return 'info'
}

async function loadPackageEvidence(packageId = selectedPackageId.value): Promise<void> {
  if (!packageId || !selectedTargetId.value) {
    packageRuns.value = []
    supplyChainVerifications.value = []
    supplyChainEnvelopes.value = []
    return
  }
  const [page, verifications, envelopes] = await Promise.all([
    api.runs(projectId.value, {
      target_id: selectedTargetId.value,
      automation_package_id: packageId,
      limit: 50,
    }),
    api.packageSupplyChainVerifications(
      projectId.value,
      selectedTargetId.value,
      packageId,
    ),
    api.packageSupplyChainEnvelopes(
      projectId.value,
      selectedTargetId.value,
      packageId,
    ),
  ])
  packageRuns.value = page.items
  supplyChainVerifications.value = verifications
  supplyChainEnvelopes.value = envelopes
}

async function loadTargetData(preferredPackageId = ''): Promise<void> {
  if (!selectedTargetId.value) {
    environments.value = []
    packages.value = []
    selectedPackageId.value = ''
    packageRuns.value = []
    supplyChainVerifications.value = []
    supplyChainEnvelopes.value = []
    return
  }
  const [environmentList, packageList] = await Promise.all([
    api.environments(projectId.value, selectedTargetId.value),
    api.packages(projectId.value, selectedTargetId.value),
  ])
  environments.value = environmentList
  packages.value = packageList
  const nextId = packageList.some((item) => item.id === preferredPackageId)
    ? preferredPackageId
    : [...packageList].reverse()[0]?.id ?? ''
  selectedPackageId.value = nextId
  await loadPackageEvidence(nextId)
}

async function initialize(): Promise<void> {
  loading.value = true
  try {
    const [projectList, memberList, targetList, baselineList] = await Promise.all([
      api.projects(),
      api.projectMembers(projectId.value),
      api.targets(projectId.value),
      api.baselines(projectId.value),
    ])
    project.value = projectList.find((item) => item.id === projectId.value)
    members.value = memberList
    targets.value = targetList
    baselines.value = baselineList
    selectedTargetId.value =
      webTargets.value.find((item) => item.id === route.query.targetId)?.id ??
      webTargets.value[0]?.id ??
      ''
    await loadTargetData(
      typeof route.query.packageId === 'string' ? route.query.packageId : '',
    )
  } catch (error) {
    report(error, '自动化包工作台加载失败')
  } finally {
    loading.value = false
  }
}

async function changeTarget(): Promise<void> {
  loading.value = true
  try {
    await router.replace({
      query: selectedTargetId.value ? { targetId: selectedTargetId.value } : {},
    })
    await loadTargetData()
  } catch (error) {
    report(error, '目标自动化包加载失败')
  } finally {
    loading.value = false
  }
}

async function refreshTargetData(): Promise<void> {
  loading.value = true
  try {
    await loadTargetData(selectedPackageId.value)
  } catch (error) {
    report(error, '自动化包目录刷新失败')
  } finally {
    loading.value = false
  }
}

async function refreshPackageRuns(): Promise<void> {
  try {
    await loadPackageEvidence()
  } catch (error) {
    report(error, '关联 Run 刷新失败')
  }
}

async function selectPackage(row: AutomationPackage | null): Promise<void> {
  if (!row || row.id === selectedPackageId.value) return
  selectedPackageId.value = row.id
  try {
    await loadPackageEvidence(row.id)
    await router.replace({
      query: { targetId: selectedTargetId.value, packageId: row.id },
    })
  } catch (error) {
    report(error, '自动化包运行记录加载失败')
  }
}

function openDraft(): void {
  const base = selectedPackage.value
  Object.assign(draftForm, {
    name: base?.status !== 'REVOKED' ? base?.name ?? '' : '',
    version: '',
    digest: 'sha256:',
    runner_type: 'WEB_PLAYWRIGHT',
    image_repository: base?.status !== 'REVOKED' ? base?.image_repository ?? '' : '',
    supersedes_id: base?.status !== 'REVOKED' ? base?.id ?? '' : '',
  })
  draftVisible.value = true
}

async function createDraft(): Promise<void> {
  const digest = draftForm.digest.trim().toLowerCase()
  if (!/^[a-z0-9][a-z0-9._-]*$/.test(draftForm.name.trim())) {
    ElMessage.warning('包名只能包含小写字母、数字、点、下划线和连字符')
    return
  }
  if (!draftForm.version.trim() || !/^sha256:[0-9a-f]{64}$/.test(digest)) {
    ElMessage.warning('请填写版本和完整的 sha256 摘要')
    return
  }
  if (!draftForm.image_repository.trim()) {
    ElMessage.warning('请填写不带协议、标签和摘要的 OCI 仓库名')
    return
  }
  const superseded = packages.value.find((item) => item.id === draftForm.supersedes_id)
  if (superseded && superseded.name !== draftForm.name.trim()) {
    ElMessage.warning('替代版本必须与新草稿使用相同包名')
    return
  }
  actionBusy.value = true
  try {
    const created = await api.createPackageDraft(projectId.value, selectedTargetId.value, {
      ...draftForm,
      name: draftForm.name.trim(),
      version: draftForm.version.trim(),
      digest,
      image_repository: draftForm.image_repository.trim(),
      supersedes_id: draftForm.supersedes_id || null,
    })
    draftVisible.value = false
    ElMessage.success('自动化包草稿已创建；请先确认 Worker 承载该摘要，再发起验证')
    await loadTargetData(created.id)
  } catch (error) {
    report(error, '自动化包草稿创建失败')
  } finally {
    actionBusy.value = false
  }
}

function openValidation(row: AutomationPackage): void {
  if (row.supply_chain_status !== 'VERIFIED') {
    ElMessage.warning('必须先由可信供应链验证器完成签名、provenance 和 SBOM 准入')
    return
  }
  selectedPackageId.value = row.id
  validationForm.environment_id =
    environments.value.find((item) => item.status === 'ACTIVE')?.id ?? ''
  validationForm.baseline_id = baselines.value[baselines.value.length - 1]?.baseline_id ?? ''
  validationVisible.value = true
}

async function startValidation(): Promise<void> {
  const current = selectedPackage.value
  if (!current || !validationForm.environment_id || !validationForm.baseline_id) {
    ElMessage.warning('请选择运行环境和 Released 全量基线')
    return
  }
  actionBusy.value = true
  try {
    const run = await api.createPackageValidationRun(
      projectId.value,
      selectedTargetId.value,
      current.id,
      crypto.randomUUID(),
      validationForm,
    )
    validationVisible.value = false
    ElMessage.success(`全量验证 Run 已创建：${run.id}`)
    await loadPackageEvidence(current.id)
  } catch (error) {
    report(error, '自动化包验证 Run 创建失败')
  } finally {
    actionBusy.value = false
  }
}

async function openActivation(row: AutomationPackage): Promise<void> {
  if (row.supply_chain_status !== 'VERIFIED') {
    ElMessage.warning('供应链状态未准入，不能激活该自动化包')
    return
  }
  selectedPackageId.value = row.id
  try {
    await loadPackageEvidence(row.id)
    activationForm.validation_run_id = eligibleValidationRuns.value[0]?.id ?? ''
    activationVisible.value = true
  } catch (error) {
    report(error, '验证记录加载失败')
  }
}

async function activate(): Promise<void> {
  const current = selectedPackage.value
  if (!current || !activationForm.validation_run_id) {
    ElMessage.warning('请选择已经 PASSED 的全量验证 Run')
    return
  }
  actionBusy.value = true
  try {
    await api.activatePackage(
      projectId.value,
      selectedTargetId.value,
      current.id,
      activationForm.validation_run_id,
    )
    activationVisible.value = false
    ElMessage.success('自动化包已激活，可以用于普通执行和回归计划')
    await loadTargetData(current.id)
  } catch (error) {
    report(error, '自动化包激活失败')
  } finally {
    actionBusy.value = false
  }
}

function openRetirement(row: AutomationPackage, action: RetirementAction): void {
  selectedPackageId.value = row.id
  retirementAction.value = action
  retirementForm.reason = ''
  retirementVisible.value = true
}

async function retire(): Promise<void> {
  const current = selectedPackage.value
  if (!current || !retirementForm.reason.trim()) {
    ElMessage.warning('请填写可审计的状态变更原因')
    return
  }
  actionBusy.value = true
  try {
    if (retirementAction.value === 'DEPRECATE') {
      await api.deprecatePackage(
        projectId.value,
        selectedTargetId.value,
        current.id,
        retirementForm.reason.trim(),
      )
      ElMessage.success('自动化包已弃用')
    } else {
      await api.revokePackage(
        projectId.value,
        selectedTargetId.value,
        current.id,
        retirementForm.reason.trim(),
      )
      ElMessage.success('自动化包已吊销，新执行和历史重跑将被阻止')
    }
    retirementVisible.value = false
    await loadTargetData(current.id)
  } catch (error) {
    report(
      error,
      retirementAction.value === 'DEPRECATE' ? '自动化包弃用失败' : '自动化包吊销失败',
    )
  } finally {
    actionBusy.value = false
  }
}

onMounted(initialize)
</script>

<template>
  <div class="page-container" v-loading="loading || actionBusy">
    <el-button
      text
      class="back-link"
      @click="router.push({ name: 'project', params: { projectId } })"
    >
      <el-icon><ArrowLeft /></el-icon>
      返回项目
    </el-button>

    <header class="page-heading packages-heading">
      <div>
        <div class="project-key">{{ project?.key }}</div>
        <h1>{{ project?.name || '项目' }} · 自动化包</h1>
        <p>以不可变 OCI 摘要、Sigstore 签名、构建来源证明和 SBOM 治理包发布。</p>
      </div>
      <div class="heading-actions">
        <el-select
          v-model="selectedTargetId"
          placeholder="选择 Web 目标"
          class="target-select"
          @change="changeTarget"
        >
          <el-option
            v-for="target in webTargets"
            :key="target.id"
            :label="`${target.name} · ${target.key}`"
            :value="target.id"
          />
        </el-select>
        <el-button :icon="Refresh" @click="refreshTargetData">刷新</el-button>
        <el-button
          v-if="canManage && selectedTargetId"
          type="primary"
          :icon="Plus"
          @click="openDraft"
        >创建草稿</el-button>
      </div>
    </header>

    <el-alert
      v-if="!canManage"
      title="当前账号可查看包版本与验证记录；只有 Project Admin 或 System Admin 可以变更生命周期。"
      type="info"
      :closable="false"
      show-icon
      class="permission-alert"
    />
    <el-alert
      v-if="targets.length > 0 && webTargets.length === 0"
      title="当前项目没有 Web 目标；自动化包工作台暂只支持 WEB_PLAYWRIGHT。"
      type="warning"
      :closable="false"
      show-icon
      class="permission-alert"
    />

    <div class="metric-grid">
      <article class="metric-card"><el-icon><Box /></el-icon><strong>{{ packageCounts.total }}</strong><span>版本总数</span></article>
      <article class="metric-card active"><el-icon><CircleCheck /></el-icon><strong>{{ packageCounts.active }}</strong><span>已激活</span></article>
      <article class="metric-card draft"><el-icon><VideoPlay /></el-icon><strong>{{ packageCounts.draft }}</strong><span>待验证草稿</span></article>
      <article class="metric-card active"><el-icon><CircleCheck /></el-icon><strong>{{ packageCounts.admitted }}</strong><span>供应链准入</span></article>
    </div>

    <div class="workspace-grid">
      <section class="surface package-list">
        <div class="toolbar">
          <div>
            <strong>版本目录</strong>
            <span class="section-note">选择版本查看不可变引用和关联 Run</span>
          </div>
        </div>
        <el-table
          :data="displayPackages"
          highlight-current-row
          empty-text="该目标暂无自动化包"
          @current-change="selectPackage"
        >
          <el-table-column label="自动化包" min-width="220">
            <template #default="scope">
              <strong>{{ scope.row.name }}@{{ scope.row.version }}</strong>
              <div class="muted mono">{{ scope.row.runner_type }}</div>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="105">
            <template #default="scope">
              <el-tag :type="packageTagType(scope.row.status)">
                {{ packageStatusLabels[scope.row.status as AutomationPackage['status']] }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="供应链" width="125">
            <template #default="scope">
              <el-tag :type="supplyChainTagType(scope.row.supply_chain_status)" effect="plain">
                {{ supplyChainStatusLabels[scope.row.supply_chain_status as AutomationPackage['supply_chain_status']] }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="不可变摘要" min-width="190">
            <template #default="scope"><code>{{ shortDigest(scope.row.digest) }}</code></template>
          </el-table-column>
          <el-table-column label="创建时间" width="165">
            <template #default="scope">{{ formatDate(scope.row.created_at) }}</template>
          </el-table-column>
          <el-table-column v-if="canManage" label="操作" width="240" fixed="right">
            <template #default="scope">
              <el-button
                v-if="scope.row.status === 'DRAFT'"
                link
                type="primary"
                :disabled="scope.row.supply_chain_status !== 'VERIFIED'"
                @click.stop="openValidation(scope.row as AutomationPackage)"
              >验证</el-button>
              <el-button
                v-if="scope.row.status === 'DRAFT'"
                link
                type="success"
                :disabled="scope.row.supply_chain_status !== 'VERIFIED'"
                @click.stop="openActivation(scope.row as AutomationPackage)"
              >激活</el-button>
              <el-button
                v-if="scope.row.status === 'ACTIVE'"
                link
                type="warning"
                @click.stop="openRetirement(scope.row as AutomationPackage, 'DEPRECATE')"
              >弃用</el-button>
              <el-button
                v-if="scope.row.status !== 'REVOKED'"
                link
                type="danger"
                @click.stop="openRetirement(scope.row as AutomationPackage, 'REVOKE')"
              >吊销</el-button>
            </template>
          </el-table-column>
        </el-table>
      </section>

      <aside class="surface package-detail">
        <template v-if="selectedPackage">
          <div class="detail-heading">
            <div>
              <span>当前版本</span>
              <h2>{{ selectedPackage.name }}@{{ selectedPackage.version }}</h2>
            </div>
            <div class="detail-tags">
              <el-tag :type="packageTagType(selectedPackage.status)">
                {{ packageStatusLabels[selectedPackage.status] }}
              </el-tag>
              <el-tag :type="supplyChainTagType(selectedPackage.supply_chain_status)" effect="plain">
                {{ supplyChainStatusLabels[selectedPackage.supply_chain_status] }}
              </el-tag>
            </div>
          </div>
          <el-alert
            v-if="selectedPackage.supply_chain_status === 'PENDING'"
            title="必须先由可信 CI 验证器核验签名、透明日志、SLSA provenance 和 SBOM；项目管理员不能自行标记通过。"
            type="warning"
            :closable="false"
            show-icon
            class="evidence-alert"
          />
          <el-alert
            v-else-if="selectedPackage.supply_chain_status === 'REJECTED'"
            :title="currentSupplyChainVerification?.reason || '供应链策略拒绝该摘要，已阻止验证、激活、新执行和重跑。'"
            type="error"
            :closable="false"
            show-icon
            class="evidence-alert"
          />
          <dl class="detail-list">
            <div><dt>目标</dt><dd>{{ selectedTarget?.name }}</dd></div>
            <div><dt>Runner</dt><dd><code>{{ selectedPackage.runner_type }}</code></dd></div>
            <div class="wide"><dt>OCI 仓库</dt><dd class="mono wrap">{{ selectedPackage.image_repository }}</dd></div>
            <div class="wide"><dt>内容摘要</dt><dd class="mono wrap">{{ selectedPackage.digest }}</dd></div>
            <div><dt>替代版本</dt><dd class="mono">{{ selectedPackage.supersedes_id || '—' }}</dd></div>
            <div><dt>激活时间</dt><dd>{{ formatDate(selectedPackage.activated_at) }}</dd></div>
            <div><dt>供应链准入</dt><dd>{{ formatDate(selectedPackage.supply_chain_verified_at) }}</dd></div>
            <div><dt>策略版本</dt><dd>{{ currentSupplyChainVerification?.policy_version || '—' }}</dd></div>
            <div><dt>服务凭据</dt><dd class="mono wrap">{{ supplyChainEnvelopes[0]?.credential_id || '—' }}</dd></div>
            <div class="wide"><dt>验证 Run</dt><dd class="mono">{{ selectedPackage.validated_run_id || '—' }}</dd></div>
            <div class="wide"><dt>证明报告摘要</dt><dd class="mono wrap">{{ currentSupplyChainVerification?.report_digest || '—' }}</dd></div>
            <div v-if="selectedPackage.status_reason" class="wide status-reason">
              <dt>状态原因</dt><dd>{{ selectedPackage.status_reason }}</dd>
            </div>
          </dl>

          <div class="runs-heading">
            <div><strong>供应链证明</strong><span>{{ supplyChainVerifications.length }} 次不可变判定</span></div>
          </div>
          <el-table
            :data="supplyChainVerifications"
            size="small"
            max-height="240"
            empty-text="历史兼容包没有供应链证明"
            class="evidence-table"
          >
            <el-table-column type="expand" width="38">
              <template #default="scope">
                <dl class="evidence-grid">
                  <div><dt>报告摘要</dt><dd class="mono wrap">{{ scope.row.report_digest }}</dd></div>
                  <div><dt>签名 Bundle</dt><dd class="mono wrap">{{ scope.row.signature_bundle_digest }}</dd></div>
                  <div><dt>Provenance</dt><dd class="mono wrap">{{ scope.row.provenance_digest }}</dd></div>
                  <div><dt>SBOM</dt><dd class="mono wrap">{{ scope.row.sbom_digest }}</dd></div>
                  <div><dt>证书颁发者</dt><dd class="wrap">{{ scope.row.certificate_issuer }}</dd></div>
                  <div><dt>证书身份</dt><dd class="wrap">{{ scope.row.certificate_identity }}</dd></div>
                  <div><dt>源码仓库</dt><dd class="wrap">{{ scope.row.source_repository }}</dd></div>
                  <div><dt>源码修订</dt><dd class="mono wrap">{{ scope.row.source_revision }}</dd></div>
                  <div v-if="scope.row.reason" class="wide"><dt>拒绝原因</dt><dd>{{ scope.row.reason }}</dd></div>
                </dl>
              </template>
            </el-table-column>
            <el-table-column label="结果" width="92">
              <template #default="scope">
                <el-tag :type="scope.row.outcome === 'VERIFIED' ? 'success' : 'danger'" size="small">
                  {{ scope.row.outcome }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="验证器 / 构建器" min-width="205">
              <template #default="scope">
                <strong>{{ scope.row.verifier }}</strong>
                <div class="muted mono wrap">{{ scope.row.builder_id }}</div>
              </template>
            </el-table-column>
            <el-table-column label="时间" width="145">
              <template #default="scope">{{ formatDate(scope.row.verified_at) }}</template>
            </el-table-column>
          </el-table>

          <div class="runs-heading">
            <div><strong>签名 Envelope</strong><span>{{ supplyChainEnvelopes.length }} 次已认证请求</span></div>
          </div>
          <el-table
            :data="supplyChainEnvelopes"
            size="small"
            max-height="220"
            empty-text="M9.4.1 历史记录没有服务签名 envelope"
            class="evidence-table"
          >
            <el-table-column type="expand" width="38">
              <template #default="scope">
                <dl class="evidence-grid">
                  <div><dt>Envelope Profile</dt><dd class="mono wrap">{{ scope.row.envelope_profile }}</dd></div>
                  <div><dt>签名算法</dt><dd class="mono">{{ scope.row.signature_algorithm }}</dd></div>
                  <div><dt>工作负载身份</dt><dd class="mono wrap">{{ scope.row.workload_identity || '旧 HMAC 无身份绑定' }}</dd></div>
                  <div><dt>公钥指纹</dt><dd class="mono wrap">{{ scope.row.key_fingerprint || '—' }}</dd></div>
                  <div><dt>请求摘要</dt><dd class="mono wrap">{{ scope.row.request_digest }}</dd></div>
                  <div><dt>签名摘要</dt><dd class="mono wrap">{{ scope.row.signature_digest }}</dd></div>
                  <div><dt>Nonce</dt><dd class="mono wrap">{{ scope.row.nonce }}</dd></div>
                  <div><dt>验证记录</dt><dd class="mono wrap">{{ scope.row.verification_id }}</dd></div>
                </dl>
              </template>
            </el-table-column>
            <el-table-column label="验证器凭据" min-width="210">
              <template #default="scope">
                <strong>{{ scope.row.verifier }}</strong>
                <div class="muted mono wrap">{{ scope.row.credential_id }}</div>
                <el-tag
                  size="small"
                  :type="scope.row.signature_algorithm === 'ED25519' ? 'success' : 'warning'"
                >{{ scope.row.signature_algorithm }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="签发 / 接收" width="165">
              <template #default="scope">
                <div>{{ formatDate(scope.row.issued_at) }}</div>
                <div class="muted">{{ formatDate(scope.row.received_at) }}</div>
              </template>
            </el-table-column>
          </el-table>

          <div class="runs-heading">
            <div><strong>关联 Run</strong><span>最近 {{ packageRuns.length }} 条</span></div>
            <el-button text :icon="Refresh" @click="refreshPackageRuns">刷新</el-button>
          </div>
          <el-table :data="packageRuns" size="small" max-height="310" empty-text="暂无验证或执行记录">
            <el-table-column label="Run" min-width="190">
              <template #default="scope">
                <span
                  class="table-link mono"
                  @click="router.push({ name: 'run-detail', params: { projectId, runId: scope.row.id } })"
                >{{ scope.row.id.slice(0, 13) }}…</span>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="105">
              <template #default="scope">
                <el-tag :type="runTagType(scope.row.status)" size="small">{{ scope.row.status }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="用例" width="65" prop="case_count" />
          </el-table>
        </template>
        <el-empty v-else description="选择一个自动化包版本查看详情" />
      </aside>
    </div>

    <el-dialog v-model="draftVisible" title="创建自动化包草稿" width="680px" destroy-on-close>
      <el-alert
        title="创建后状态为待供应链验证。可信 CI 必须验证最终 OCI 摘要的签名、provenance 和 SBOM，准入后才能执行全量验证。"
        type="info"
        :closable="false"
        show-icon
      />
      <el-form label-position="top" class="dialog-form">
        <div class="form-grid">
          <el-form-item label="包名" required>
            <el-input v-model="draftForm.name" placeholder="yanjia-web" />
          </el-form-item>
          <el-form-item label="版本" required>
            <el-input v-model="draftForm.version" placeholder="0.2.0" />
          </el-form-item>
        </div>
        <el-form-item label="OCI 仓库（不带协议、标签和摘要）" required>
          <el-input v-model="draftForm.image_repository" placeholder="registry.example.com/testops/yanjia-web" />
        </el-form-item>
        <el-form-item label="OCI Manifest Digest" required>
          <el-input v-model="draftForm.digest" class="mono-input" placeholder="sha256:..." />
        </el-form-item>
        <div class="form-grid">
          <el-form-item label="Runner 类型">
            <el-input v-model="draftForm.runner_type" disabled />
          </el-form-item>
          <el-form-item label="替代现有版本">
            <el-select v-model="draftForm.supersedes_id" clearable style="width: 100%">
              <el-option
                v-for="item in packages.filter((candidate) =>
                  candidate.status !== 'REVOKED' && candidate.name === draftForm.name.trim()
                )"
                :key="item.id"
                :label="`${item.name}@${item.version} · ${packageStatusLabels[item.status]}`"
                :value="item.id"
              />
            </el-select>
          </el-form-item>
        </div>
      </el-form>
      <template #footer>
        <el-button @click="draftVisible = false">取消</el-button>
        <el-button type="primary" :loading="actionBusy" @click="createDraft">创建草稿</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="validationVisible" title="发起全量验证" width="600px" destroy-on-close>
      <el-alert
        :title="`验证 ${selectedPackage?.name}@${selectedPackage?.version}；平台会固定草稿摘要和 Released 基线并执行全部启用用例。`"
        type="warning"
        :closable="false"
        show-icon
      />
      <el-form label-position="top" class="dialog-form">
        <el-form-item label="运行环境" required>
          <el-select v-model="validationForm.environment_id" style="width: 100%">
            <el-option
              v-for="item in environments.filter((candidate) => candidate.status === 'ACTIVE')"
              :key="item.id"
              :label="`${item.name} · ${item.key}`"
              :value="item.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="Released 全量基线" required>
          <el-select v-model="validationForm.baseline_id" style="width: 100%">
            <el-option
              v-for="item in baselines"
              :key="item.baseline_id"
              :label="`${item.version} · ${item.enabled_case_count} 条启用用例`"
              :value="item.baseline_id"
            />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="validationVisible = false">取消</el-button>
        <el-button type="primary" :loading="actionBusy" @click="startValidation">创建验证 Run</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="activationVisible" title="激活自动化包" width="600px" destroy-on-close>
      <el-alert
        v-if="eligibleValidationRuns.length === 0"
        title="尚无 PASSED 的验证 Run。请先发起全量验证并等待执行完成。"
        type="warning"
        :closable="false"
        show-icon
      />
      <el-alert
        v-else
        title="激活后该不可变版本可用于普通执行和定时回归；验证基线必须覆盖全部启用用例。"
        type="success"
        :closable="false"
        show-icon
      />
      <el-form label-position="top" class="dialog-form">
        <el-form-item label="PASSED 验证 Run" required>
          <el-select v-model="activationForm.validation_run_id" style="width: 100%">
            <el-option
              v-for="run in eligibleValidationRuns"
              :key="run.id"
              :label="`${run.id} · ${run.case_count} 条 · ${formatDate(run.finished_at)}`"
              :value="run.id"
            />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="activationVisible = false">取消</el-button>
        <el-button
          type="success"
          :disabled="eligibleValidationRuns.length === 0"
          :loading="actionBusy"
          @click="activate"
        >确认激活</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="retirementVisible"
      :title="retirementAction === 'DEPRECATE' ? '弃用自动化包' : '吊销自动化包'"
      width="600px"
      destroy-on-close
    >
      <el-alert
        :title="retirementAction === 'DEPRECATE'
          ? '弃用前必须先暂停或迁移所有仍引用该版本的 ACTIVE 回归计划。'
          : '吊销用于安全或完整性事件，将立即阻止新执行和历史重跑；已经在途的 Run 不会被强制篡改。'"
        :type="retirementAction === 'DEPRECATE' ? 'warning' : 'error'"
        :icon="retirementAction === 'DEPRECATE' ? Warning : CircleClose"
        :closable="false"
        show-icon
      />
      <el-form label-position="top" class="dialog-form">
        <el-form-item label="可审计原因" required>
          <el-input v-model="retirementForm.reason" type="textarea" :rows="4" maxlength="500" show-word-limit />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="retirementVisible = false">取消</el-button>
        <el-button
          :type="retirementAction === 'DEPRECATE' ? 'warning' : 'danger'"
          :loading="actionBusy"
          @click="retire"
        >{{ retirementAction === 'DEPRECATE' ? '确认弃用' : '确认吊销' }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.back-link {
  margin: -8px 0 10px -12px;
  color: #66788a;
}

.packages-heading {
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

.heading-actions,
.detail-heading,
.runs-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.detail-tags {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 6px;
}

.target-select {
  width: 250px;
}

.permission-alert {
  margin-bottom: 18px;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 14px;
  margin-bottom: 18px;
}

.metric-card {
  display: grid;
  grid-template-columns: 40px auto;
  column-gap: 12px;
  align-items: center;
  padding: 17px 19px;
  border: 1px solid #e5eaee;
  border-radius: 11px;
  color: #4c6378;
  background: #fff;
}

.metric-card .el-icon {
  grid-row: 1 / 3;
  width: 40px;
  height: 40px;
  border-radius: 10px;
  color: #287469;
  background: #e7f7f3;
}

.metric-card.active .el-icon { color: #168250; background: #e8f7ef; }
.metric-card.draft .el-icon { color: #a56c12; background: #fff3d8; }
.metric-card.revoked .el-icon { color: #b63d46; background: #fdebed; }

.metric-card strong {
  color: #17324d;
  font-size: 23px;
}

.metric-card span {
  font-size: 12px;
}

.workspace-grid {
  display: grid;
  grid-template-columns: minmax(650px, 1.45fr) minmax(410px, 0.75fr);
  gap: 18px;
  align-items: start;
}

.package-list,
.package-detail {
  overflow: hidden;
}

.toolbar {
  padding: 18px 20px;
  border-bottom: 1px solid #edf0f2;
}

.section-note {
  margin-left: 10px;
  color: #8a98a6;
  font-size: 12px;
  font-weight: 400;
}

.muted {
  margin-top: 4px;
  color: #8392a5;
  font-size: 11px;
}

.package-detail {
  padding: 21px;
}

.evidence-alert {
  margin-top: 16px;
}

.detail-heading span,
.runs-heading span {
  display: block;
  margin-top: 3px;
  color: #8291a1;
  font-size: 11px;
}

.detail-heading h2 {
  margin: 4px 0 0;
  color: #17324d;
  font-size: 19px;
}

.detail-list {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0;
  margin: 18px 0 22px;
  border: 1px solid #edf0f2;
  border-radius: 9px;
  overflow: hidden;
}

.detail-list div {
  min-width: 0;
  padding: 12px 13px;
  border-bottom: 1px solid #edf0f2;
}

.detail-list div:nth-last-child(-n + 2) {
  border-bottom: 0;
}

.detail-list .wide {
  grid-column: 1 / -1;
}

.detail-list dt {
  margin-bottom: 4px;
  color: #8a98a6;
  font-size: 10px;
  text-transform: uppercase;
}

.detail-list dd {
  margin: 0;
  color: #3b5065;
  font-size: 12px;
}

.detail-list .status-reason {
  color: #9a3c43;
  background: #fff8f8;
}

.runs-heading {
  margin: 18px 0 10px;
}

.evidence-table {
  margin-bottom: 4px;
}

.evidence-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px 16px;
  margin: 0;
  padding: 13px 18px;
  background: #f8fafb;
}

.evidence-grid .wide {
  grid-column: 1 / -1;
}

.evidence-grid dt {
  margin-bottom: 3px;
  color: #8a98a6;
  font-size: 10px;
  text-transform: uppercase;
}

.evidence-grid dd {
  margin: 0;
  color: #40566b;
  font-size: 11px;
}

.dialog-form {
  margin-top: 20px;
}

.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

.mono,
code,
.mono-input :deep(input) {
  font-family: "SFMono-Regular", Consolas, monospace;
}

code {
  color: #526a7d;
  font-size: 11px;
}

.wrap {
  overflow-wrap: anywhere;
}
</style>
