<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowLeft,
  Calendar,
  Connection,
  Plus,
  Refresh,
  Setting,
  User,
  VideoPlay,
} from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'

import { ApiError, api } from '@/api/client'
import type {
  AutomationPackage,
  Baseline,
  Environment,
  ExecutionPolicy,
  Project,
  ProjectMember,
  ProjectMemberCandidate,
  ProjectRole,
  RegressionSchedule,
  RegressionScheduleFiring,
  RunnerPoolCatalog,
  SecretBinding,
  Target,
} from '@/api/types'
import { auth } from '@/auth'
import { formatDate, formatUtcDate, shortDigest } from '@/presentation'

const route = useRoute()
const router = useRouter()
const projectId = computed(() => String(route.params.projectId))
const loading = ref(true)
const activeTab = ref('members')
const project = ref<Project>()
const members = ref<ProjectMember[]>([])
const candidates = ref<ProjectMemberCandidate[]>([])
const targets = ref<Target[]>([])
const environmentsByTarget = ref<Record<string, Environment[]>>({})
const executionPolicy = ref<ExecutionPolicy>()
const runnerPools = ref<RunnerPoolCatalog[]>([])
const regressionSchedules = ref<RegressionSchedule[]>([])
const baselines = ref<Baseline[]>([])
const packagesByTarget = ref<Record<string, AutomationPackage[]>>({})

const currentMember = computed(() =>
  members.value.find((item) => item.user_id === auth.state.user?.id),
)
const canManage = computed(
  () =>
    auth.state.user?.system_role === 'SYSTEM_ADMIN' ||
    currentMember.value?.role === 'PROJECT_ADMIN',
)
const environmentCount = computed(() =>
  Object.values(environmentsByTarget.value).reduce((total, items) => total + items.length, 0),
)
const activeScheduleCount = computed(
  () => regressionSchedules.value.filter((item) => item.status === 'ACTIVE').length,
)
const inFlightPercent = computed(() => {
  const policy = executionPolicy.value
  return policy ? Math.min(100, Math.round((policy.in_flight_runs / policy.max_in_flight_runs) * 100)) : 0
})
const dailyPercent = computed(() => {
  const policy = executionPolicy.value
  return policy ? Math.min(100, Math.round((policy.runs_created_today / policy.max_daily_runs) * 100)) : 0
})
const policyStatusLabel = computed(() => {
  const status = executionPolicy.value?.quota_status
  if (status === 'BLOCKED') return '配额已满'
  if (status === 'NEAR_LIMIT') return '接近上限'
  return '容量充足'
})

const memberVisible = ref(false)
const memberBusy = ref(false)
const memberQuery = ref('')
const memberForm = reactive({ user_id: '', role: 'VIEWER' as ProjectRole })

const targetVisible = ref(false)
const targetBusy = ref(false)
const targetForm = reactive({
  key: '',
  name: '',
  target_type: 'WEB' as Target['target_type'],
  browser: 'chromium',
  runner_pool_id: '',
})

const environmentVisible = ref(false)
const environmentBusy = ref(false)
const environmentTarget = ref<Target>()
const environmentForm = reactive({
  key: '',
  name: '',
  base_url: '',
  secret_bindings: '',
  runner_pool_id: '',
})

const environmentEditVisible = ref(false)
const environmentEditBusy = ref(false)
const environmentEditTarget = ref<Target>()
const environmentEdit = ref<Environment>()
const environmentEditForm = reactive({
  name: '',
  status: 'ACTIVE' as Environment['status'],
  secret_bindings: '',
  runner_pool_id: '',
})
const policyBusy = ref(false)
const policyForm = reactive({
  max_in_flight_runs: 20,
  max_daily_runs: 500,
  run_timeout_seconds: 3600,
})

const scheduleVisible = ref(false)
const scheduleBusy = ref(false)
const scheduleEditId = ref('')
const scheduleForm = reactive({
  key: '',
  name: '',
  description: '',
  target_id: '',
  environment_id: '',
  baseline_id: '',
  automation_package_id: '',
  case_codes: '',
  cron_expression: '0 2 * * *',
  timezone: 'Asia/Shanghai',
  misfire_policy: 'FIRE_ONCE' as RegressionSchedule['misfire_policy'],
  misfire_grace_seconds: 300,
  status: 'ACTIVE' as 'ACTIVE' | 'PAUSED',
})
const scheduleTarget = computed(() =>
  targets.value.find((item) => item.id === scheduleForm.target_id),
)
const scheduleEnvironments = computed(
  () => environmentsByTarget.value[scheduleForm.target_id] || [],
)
const schedulePackages = computed(() => packagesByTarget.value[scheduleForm.target_id] || [])
const firingVisible = ref(false)
const firingLoading = ref(false)
const firingSchedule = ref<RegressionSchedule>()
const firings = ref<RegressionScheduleFiring[]>([])

const roleLabels: Record<ProjectRole, string> = {
  VIEWER: '只读成员',
  TESTER: '测试人员',
  REVIEWER: '审批人员',
  PROJECT_ADMIN: '项目管理员',
}

function report(error: unknown, fallback: string): void {
  ElMessage.error(error instanceof ApiError ? error.message : fallback)
}

function poolLabel(poolId: string | null | undefined, fallback = '未绑定（默认队列）'): string {
  if (!poolId) return fallback
  const pool = runnerPools.value.find((item) => item.id === poolId)
  return pool ? `${pool.name} · ${pool.available_slots} 槽可用` : 'Runner Pool 不可用'
}

function compatiblePools(targetType: Target['target_type']): RunnerPoolCatalog[] {
  return runnerPools.value.filter((pool) => pool.target_types.includes(targetType))
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const [
      projectList,
      memberList,
      targetList,
      policy,
      poolCatalog,
      scheduleList,
      baselineList,
    ] = await Promise.all([
      api.projects(),
      api.projectMembers(projectId.value),
      api.targets(projectId.value),
      api.executionPolicy(projectId.value),
      api.runnerPoolCatalog(),
      api.regressionSchedules(projectId.value),
      api.baselines(projectId.value),
    ])
    project.value = projectList.find((item) => item.id === projectId.value)
    members.value = memberList
    targets.value = targetList
    executionPolicy.value = policy
    runnerPools.value = poolCatalog
    regressionSchedules.value = scheduleList
    baselines.value = baselineList
    Object.assign(policyForm, {
      max_in_flight_runs: policy.max_in_flight_runs,
      max_daily_runs: policy.max_daily_runs,
      run_timeout_seconds: policy.run_timeout_seconds,
    })
    const environmentLists = await Promise.all(
      targetList.map(async (target) => [target.id, await api.environments(projectId.value, target.id)] as const),
    )
    environmentsByTarget.value = Object.fromEntries(environmentLists)
    const packageLists = await Promise.all(
      targetList.map(async (target) => [target.id, await api.packages(projectId.value, target.id)] as const),
    )
    packagesByTarget.value = Object.fromEntries(packageLists)
  } catch (error) {
    report(error, '项目设置加载失败')
  } finally {
    loading.value = false
  }
}

async function searchCandidates(): Promise<void> {
  try {
    candidates.value = await api.memberCandidates(projectId.value, memberQuery.value)
  } catch (error) {
    report(error, '候选用户加载失败')
  }
}

async function openMember(): Promise<void> {
  memberQuery.value = ''
  memberForm.user_id = ''
  memberForm.role = 'VIEWER'
  await searchCandidates()
  memberVisible.value = true
}

async function saveMember(): Promise<void> {
  if (!memberForm.user_id) {
    ElMessage.warning('请选择用户')
    return
  }
  memberBusy.value = true
  try {
    await api.upsertProjectMember(projectId.value, memberForm)
    memberVisible.value = false
    ElMessage.success('项目成员已保存')
    await load()
  } catch (error) {
    report(error, '项目成员保存失败')
  } finally {
    memberBusy.value = false
  }
}

async function changeMemberRole(member: ProjectMember, role: ProjectRole): Promise<void> {
  try {
    await api.upsertProjectMember(projectId.value, { user_id: member.user_id, role })
    ElMessage.success('成员角色已更新')
    await load()
  } catch (error) {
    report(error, '成员角色更新失败')
  }
}

async function removeMember(member: ProjectMember): Promise<void> {
  try {
    await api.removeProjectMember(projectId.value, member.user_id)
    ElMessage.success('成员已移除')
    await load()
  } catch (error) {
    report(error, '成员移除失败')
  }
}

function openTarget(): void {
  Object.assign(targetForm, {
    key: '',
    name: '',
    target_type: 'WEB',
    browser: 'chromium',
    runner_pool_id: '',
  })
  targetVisible.value = true
}

async function createTarget(): Promise<void> {
  if (!targetForm.key || !targetForm.name) {
    ElMessage.warning('目标标识和名称不能为空')
    return
  }
  targetBusy.value = true
  try {
    await api.createTarget(projectId.value, {
      key: targetForm.key,
      name: targetForm.name,
      target_type: targetForm.target_type,
      browser: targetForm.target_type === 'WEB' ? targetForm.browser : null,
      runner_pool_id: targetForm.runner_pool_id || null,
    })
    targetVisible.value = false
    ElMessage.success('测试目标已创建')
    await load()
  } catch (error) {
    report(error, '测试目标创建失败')
  } finally {
    targetBusy.value = false
  }
}

async function bindTargetPool(target: Target, runnerPoolId: string): Promise<void> {
  try {
    await api.updateTarget(projectId.value, target.id, {
      runner_pool_id: runnerPoolId || null,
    })
    ElMessage.success('目标 Runner Pool 已更新')
    await load()
  } catch (error) {
    report(error, '目标 Runner Pool 更新失败')
  }
}

async function toggleTarget(target: Target): Promise<void> {
  try {
    await api.updateTarget(projectId.value, target.id, {
      status: target.status === 'ACTIVE' ? 'ARCHIVED' : 'ACTIVE',
    })
    ElMessage.success(target.status === 'ACTIVE' ? '测试目标已归档' : '测试目标已恢复')
    await load()
  } catch (error) {
    report(error, '测试目标状态更新失败')
  }
}

function bindingsText(bindings: SecretBinding[]): string {
  return bindings.map((item) => `${item.name}=${item.ref}`).join('\n')
}

function parseBindings(value: string): SecretBinding[] {
  const result: SecretBinding[] = []
  for (const rawLine of value.split(/\r?\n/)) {
    const line = rawLine.trim()
    if (!line) continue
    const separator = line.indexOf('=')
    if (separator <= 0) throw new Error(`密钥绑定格式错误：${line}`)
    const name = line.slice(0, separator).trim()
    const ref = line.slice(separator + 1).trim()
    if (!ref.startsWith('secret://')) throw new Error(`密钥引用必须以 secret:// 开头：${name}`)
    result.push({ name, ref })
  }
  return result
}

function openEnvironment(target: Target): void {
  environmentTarget.value = target
  Object.assign(environmentForm, {
    key: '',
    name: '',
    base_url: '',
    secret_bindings: '',
    runner_pool_id: '',
  })
  environmentVisible.value = true
}

async function createEnvironment(): Promise<void> {
  const target = environmentTarget.value
  if (!target || !environmentForm.key || !environmentForm.name) {
    ElMessage.warning('环境标识和名称不能为空')
    return
  }
  if (target.target_type === 'WEB' && !environmentForm.base_url) {
    ElMessage.warning('Web 环境必须填写 Base URL')
    return
  }
  environmentBusy.value = true
  try {
    await api.createEnvironment(projectId.value, target.id, {
      key: environmentForm.key,
      name: environmentForm.name,
      web_config:
        target.target_type === 'WEB' ? { base_url: environmentForm.base_url } : null,
      secret_bindings: parseBindings(environmentForm.secret_bindings),
      runner_pool_id: environmentForm.runner_pool_id || null,
    })
    environmentVisible.value = false
    ElMessage.success('运行环境已创建，仅保存 Secret 引用')
    await load()
  } catch (error) {
    report(error, error instanceof Error ? error.message : '运行环境创建失败')
  } finally {
    environmentBusy.value = false
  }
}

function openEnvironmentEdit(target: Target, environment: Environment): void {
  environmentEditTarget.value = target
  environmentEdit.value = environment
  Object.assign(environmentEditForm, {
    name: environment.name,
    status: environment.status,
    secret_bindings: bindingsText(environment.secret_bindings),
    runner_pool_id: environment.runner_pool_id || '',
  })
  environmentEditVisible.value = true
}

async function saveEnvironment(): Promise<void> {
  const target = environmentEditTarget.value
  const environment = environmentEdit.value
  if (!target || !environment || !environmentEditForm.name) return
  environmentEditBusy.value = true
  try {
    await api.updateEnvironment(projectId.value, target.id, environment.id, {
      name: environmentEditForm.name,
      status: environmentEditForm.status,
      secret_bindings: parseBindings(environmentEditForm.secret_bindings),
      runner_pool_id: environmentEditForm.runner_pool_id || null,
    })
    environmentEditVisible.value = false
    ElMessage.success('运行环境已更新')
    await load()
  } catch (error) {
    report(error, error instanceof Error ? error.message : '运行环境更新失败')
  } finally {
    environmentEditBusy.value = false
  }
}

async function saveExecutionPolicy(): Promise<void> {
  policyBusy.value = true
  try {
    const saved = await api.updateExecutionPolicy(projectId.value, {
      max_in_flight_runs: policyForm.max_in_flight_runs,
      max_daily_runs: policyForm.max_daily_runs,
      run_timeout_seconds: policyForm.run_timeout_seconds,
    })
    executionPolicy.value = saved
    Object.assign(policyForm, {
      max_in_flight_runs: saved.max_in_flight_runs,
      max_daily_runs: saved.max_daily_runs,
      run_timeout_seconds: saved.run_timeout_seconds,
    })
    ElMessage.success('执行策略已更新')
  } catch (error) {
    report(error, '执行策略更新失败')
  } finally {
    policyBusy.value = false
  }
}

function parseCaseCodes(value: string): string[] {
  return value
    .split(/[\s,]+/)
    .map((item) => item.trim())
    .filter(Boolean)
}

function scheduleTargetChanged(): void {
  scheduleForm.environment_id = scheduleEnvironments.value.find((item) => item.status === 'ACTIVE')?.id || ''
  scheduleForm.automation_package_id = schedulePackages.value[0]?.id || ''
}

function openSchedule(schedule?: RegressionSchedule): void {
  scheduleEditId.value = schedule?.id || ''
  const targetId = schedule?.target_id || targets.value.find((item) => item.status === 'ACTIVE')?.id || ''
  Object.assign(scheduleForm, {
    key: schedule?.key || '',
    name: schedule?.name || '',
    description: schedule?.description || '',
    target_id: targetId,
    environment_id:
      schedule?.environment_id ||
      environmentsByTarget.value[targetId]?.find((item) => item.status === 'ACTIVE')?.id ||
      '',
    baseline_id:
      schedule?.baseline_id ||
      [...baselines.value].reverse().find((item) => item.status === 'RELEASED')?.baseline_id ||
      '',
    automation_package_id:
      schedule?.automation_package_id || packagesByTarget.value[targetId]?.[0]?.id || '',
    case_codes: schedule?.case_codes.join(', ') || '',
    cron_expression: schedule?.cron_expression || '0 2 * * *',
    timezone: schedule?.timezone || 'Asia/Shanghai',
    misfire_policy: schedule?.misfire_policy || 'FIRE_ONCE',
    misfire_grace_seconds: schedule?.misfire_grace_seconds || 300,
    status: schedule?.status === 'PAUSED' ? 'PAUSED' : 'ACTIVE',
  })
  scheduleVisible.value = true
}

async function saveSchedule(): Promise<void> {
  if (
    !scheduleForm.key ||
    !scheduleForm.name ||
    !scheduleForm.target_id ||
    !scheduleForm.environment_id ||
    !scheduleForm.baseline_id ||
    !scheduleForm.automation_package_id ||
    !scheduleForm.cron_expression ||
    !scheduleForm.timezone
  ) {
    ElMessage.warning('请填写计划标识、名称、运行资源、Cron 和时区')
    return
  }
  scheduleBusy.value = true
  const payload = {
    name: scheduleForm.name,
    description: scheduleForm.description || null,
    target_id: scheduleForm.target_id,
    environment_id: scheduleForm.environment_id,
    baseline_id: scheduleForm.baseline_id,
    automation_package_id: scheduleForm.automation_package_id,
    case_codes: parseCaseCodes(scheduleForm.case_codes),
    cron_expression: scheduleForm.cron_expression,
    timezone: scheduleForm.timezone,
    misfire_policy: scheduleForm.misfire_policy,
    misfire_grace_seconds: scheduleForm.misfire_grace_seconds,
    status: scheduleForm.status,
  }
  try {
    if (scheduleEditId.value) {
      await api.updateRegressionSchedule(projectId.value, scheduleEditId.value, payload)
    } else {
      await api.createRegressionSchedule(projectId.value, { key: scheduleForm.key, ...payload })
    }
    scheduleVisible.value = false
    ElMessage.success(scheduleEditId.value ? '回归计划已更新' : '回归计划已创建')
    await load()
  } catch (error) {
    report(error, '回归计划保存失败')
  } finally {
    scheduleBusy.value = false
  }
}

async function changeScheduleStatus(
  schedule: RegressionSchedule,
  status: RegressionSchedule['status'],
): Promise<void> {
  try {
    await api.updateRegressionSchedule(projectId.value, schedule.id, { status })
    ElMessage.success(status === 'ACTIVE' ? '回归计划已恢复' : status === 'PAUSED' ? '回归计划已暂停' : '回归计划已归档')
    await load()
  } catch (error) {
    report(error, '回归计划状态更新失败')
  }
}

async function triggerSchedule(schedule: RegressionSchedule): Promise<void> {
  try {
    const run = await api.triggerRegressionSchedule(
      projectId.value,
      schedule.id,
      `manual:${schedule.id}:${crypto.randomUUID()}`,
    )
    ElMessage.success('已创建一次立即回归 Run')
    await router.push({ name: 'run-detail', params: { projectId: projectId.value, runId: run.id } })
  } catch (error) {
    report(error, '立即回归触发失败')
  }
}

async function openFirings(schedule: RegressionSchedule): Promise<void> {
  firingSchedule.value = schedule
  firingVisible.value = true
  firingLoading.value = true
  try {
    firings.value = await api.regressionScheduleFirings(projectId.value, schedule.id)
  } catch (error) {
    report(error, '触发记录加载失败')
  } finally {
    firingLoading.value = false
  }
}

function targetLabel(targetId: string): string {
  return targets.value.find((item) => item.id === targetId)?.name || targetId
}

function environmentLabel(targetId: string, environmentId: string): string {
  return environmentsByTarget.value[targetId]?.find((item) => item.id === environmentId)?.name || environmentId
}

function baselineLabel(baselineId: string): string {
  return baselines.value.find((item) => item.baseline_id === baselineId)?.version || baselineId
}

onMounted(load)
</script>

<template>
  <div class="page-container" v-loading="loading">
    <el-button text class="back-link" @click="router.push({ name: 'project', params: { projectId } })">
      <el-icon><ArrowLeft /></el-icon>
      返回项目
    </el-button>
    <header class="page-heading settings-heading">
      <div>
        <div class="project-key">{{ project?.key }}</div>
        <h1>{{ project?.name || '项目' }} · 设置</h1>
        <p>管理成员角色、测试资源、Runner Pool、定时回归、Secret 引用和项目执行策略。</p>
      </div>
      <el-button :icon="Refresh" @click="load">刷新</el-button>
    </header>

    <div class="metric-grid">
      <article class="metric-card"><el-icon><User /></el-icon><strong>{{ members.length }}</strong><span>项目成员</span></article>
      <article class="metric-card"><el-icon><Connection /></el-icon><strong>{{ targets.length }}</strong><span>测试目标</span></article>
      <article class="metric-card"><el-icon><Setting /></el-icon><strong>{{ environmentCount }}</strong><span>运行环境</span></article>
      <article class="metric-card"><el-icon><Calendar /></el-icon><strong>{{ activeScheduleCount }}</strong><span>启用回归计划</span></article>
      <article class="metric-card"><el-icon><Setting /></el-icon><strong>{{ executionPolicy?.remaining_in_flight_runs ?? '—' }}</strong><span>剩余在途名额</span></article>
    </div>

    <el-alert
      v-if="!canManage"
      title="当前账号拥有项目只读权限；只有 Project Admin 或 System Admin 可以修改设置。"
      type="info"
      :closable="false"
      show-icon
      class="permission-alert"
    />

    <section class="surface settings-surface">
      <el-tabs v-model="activeTab" class="settings-tabs">
        <el-tab-pane label="成员与角色" name="members">
          <div class="toolbar">
            <div><strong>项目成员</strong><span class="section-note">至少保留一名 Project Admin</span></div>
            <el-button v-if="canManage" type="primary" :icon="Plus" @click="openMember">添加成员</el-button>
          </div>
          <el-table :data="members" empty-text="暂无成员">
            <el-table-column label="成员" min-width="260">
              <template #default="scope">
                <strong>{{ scope.row.display_name }}</strong>
                <div class="muted mono">{{ scope.row.username }}</div>
              </template>
            </el-table-column>
            <el-table-column label="项目角色" width="220">
              <template #default="scope">
                <el-select
                  v-if="canManage"
                  :model-value="scope.row.role"
                  @change="changeMemberRole(scope.row as ProjectMember, $event)"
                >
                  <el-option v-for="(label, role) in roleLabels" :key="role" :label="label" :value="role" />
                </el-select>
                <el-tag v-else>{{ roleLabels[scope.row.role as ProjectRole] }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="加入时间" width="180">
              <template #default="scope">{{ formatDate(scope.row.created_at) }}</template>
            </el-table-column>
            <el-table-column v-if="canManage" label="操作" width="100" fixed="right">
              <template #default="scope">
                <el-popconfirm
                  title="确定移除该项目成员？"
                  @confirm="removeMember(scope.row as ProjectMember)"
                >
                  <template #reference><el-button link type="danger">移除</el-button></template>
                </el-popconfirm>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <el-tab-pane label="目标与环境" name="resources">
          <div class="toolbar">
            <div><strong>项目运行资源</strong><span class="section-note">敏感值不得进入环境配置</span></div>
            <el-button v-if="canManage" type="primary" :icon="Plus" @click="openTarget">创建目标</el-button>
          </div>
          <el-empty v-if="targets.length === 0" description="暂无测试目标" />
          <article v-for="target in targets" :key="target.id" class="target-block">
            <header class="target-header">
              <div>
                <div class="target-title">
                  <strong>{{ target.name }}</strong>
                  <code>{{ target.key }}</code>
                  <el-tag size="small">{{ target.target_type }}</el-tag>
                  <el-tag :type="target.status === 'ACTIVE' ? 'success' : 'info'" size="small">
                    {{ target.status }}
                  </el-tag>
                </div>
                <span class="target-note">
                  浏览器：{{ target.browser || '不适用' }} · {{ poolLabel(target.runner_pool_id) }}
                </span>
              </div>
              <div v-if="canManage" class="target-actions">
                <el-select
                  :model-value="target.runner_pool_id || ''"
                  clearable
                  placeholder="默认队列"
                  class="pool-select"
                  @change="bindTargetPool(target, $event as string)"
                >
                  <el-option
                    v-for="pool in compatiblePools(target.target_type)"
                    :key="pool.id"
                    :label="`${pool.name} · ${pool.available_slots} 槽`"
                    :value="pool.id"
                  />
                </el-select>
                <el-button @click="openEnvironment(target)">创建环境</el-button>
                <el-button @click="toggleTarget(target)">
                  {{ target.status === 'ACTIVE' ? '归档目标' : '恢复目标' }}
                </el-button>
              </div>
            </header>
            <el-table :data="environmentsByTarget[target.id] || []" empty-text="该目标暂无运行环境">
              <el-table-column label="环境" min-width="210">
                <template #default="scope">
                  <strong>{{ scope.row.name }}</strong>
                  <div class="muted mono">{{ scope.row.key }}</div>
                </template>
              </el-table-column>
              <el-table-column label="Secret 引用" min-width="330">
                <template #default="scope">
                  <div v-if="scope.row.secret_bindings.length">
                    <code v-for="binding in scope.row.secret_bindings" :key="binding.name" class="binding">
                      {{ binding.name }} → {{ binding.ref }}
                    </code>
                  </div>
                  <span v-else class="muted">无</span>
                </template>
              </el-table-column>
              <el-table-column label="配置摘要" min-width="190">
                <template #default="scope"><code>{{ shortDigest(scope.row.config_hash) }}</code></template>
              </el-table-column>
              <el-table-column label="Runner Pool" min-width="230">
                <template #default="scope">
                  <span>{{ poolLabel(scope.row.runner_pool_id, `继承目标 · ${poolLabel(target.runner_pool_id)}`) }}</span>
                </template>
              </el-table-column>
              <el-table-column label="状态" width="110">
                <template #default="scope">
                  <el-tag :type="scope.row.status === 'ACTIVE' ? 'success' : 'info'">
                    {{ scope.row.status }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column v-if="canManage" label="操作" width="90" fixed="right">
                <template #default="scope">
                  <el-button
                    link
                    type="primary"
                    @click="openEnvironmentEdit(target, scope.row as Environment)"
                  >编辑</el-button>
                </template>
              </el-table-column>
            </el-table>
          </article>
        </el-tab-pane>

        <el-tab-pane label="定时回归" name="schedules">
          <div class="toolbar">
            <div>
              <strong>项目回归计划</strong>
              <span class="section-note">Cron 按项目时区解释，服务端统一保存和派发 UTC 时刻</span>
            </div>
            <el-button v-if="canManage" type="primary" :icon="Plus" @click="openSchedule()">
              创建计划
            </el-button>
          </div>
          <el-table :data="regressionSchedules" empty-text="暂无定时回归计划">
            <el-table-column label="计划" min-width="220">
              <template #default="scope">
                <strong>{{ scope.row.name }}</strong>
                <div class="muted mono">{{ scope.row.key }}</div>
                <div v-if="scope.row.last_error" class="schedule-error">{{ scope.row.last_error }}</div>
              </template>
            </el-table-column>
            <el-table-column label="Cron / 时区" min-width="210">
              <template #default="scope">
                <code>{{ scope.row.cron_expression }}</code>
                <div class="muted">{{ scope.row.timezone }}</div>
              </template>
            </el-table-column>
            <el-table-column label="运行资源" min-width="250">
              <template #default="scope">
                <div>{{ targetLabel(scope.row.target_id) }} · {{ environmentLabel(scope.row.target_id, scope.row.environment_id) }}</div>
                <div class="muted">基线 {{ baselineLabel(scope.row.baseline_id) }} · {{ scope.row.case_codes.length || '全部启用' }} 个用例范围</div>
              </template>
            </el-table-column>
            <el-table-column label="下次派发（UTC）" min-width="210">
              <template #default="scope">
                <span v-if="scope.row.next_fire_at">{{ formatUtcDate(scope.row.next_fire_at) }}</span>
                <span v-else class="muted">已暂停</span>
                <div class="muted">{{ scope.row.misfire_policy === 'FIRE_ONCE' ? '错过后补一次' : '错过后跳过' }}</div>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="135">
              <template #default="scope">
                <el-select
                  v-if="canManage && scope.row.status !== 'ARCHIVED'"
                  :model-value="scope.row.status"
                  @change="changeScheduleStatus(scope.row as RegressionSchedule, $event as RegressionSchedule['status'])"
                >
                  <el-option label="启用" value="ACTIVE" />
                  <el-option label="暂停" value="PAUSED" />
                  <el-option label="归档" value="ARCHIVED" />
                </el-select>
                <el-tag v-else :type="scope.row.status === 'ACTIVE' ? 'success' : 'info'">
                  {{ scope.row.status }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="操作" min-width="220" fixed="right">
              <template #default="scope">
                <el-button
                  v-if="canManage && scope.row.status !== 'ARCHIVED'"
                  link
                  type="primary"
                  :icon="VideoPlay"
                  @click="triggerSchedule(scope.row as RegressionSchedule)"
                >立即运行</el-button>
                <el-button link @click="openFirings(scope.row as RegressionSchedule)">记录</el-button>
                <el-button
                  v-if="canManage && scope.row.status !== 'ARCHIVED'"
                  link
                  @click="openSchedule(scope.row as RegressionSchedule)"
                >编辑</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <el-tab-pane label="执行策略" name="capacity">
          <div class="toolbar">
            <div>
              <strong>项目执行准入与超时</strong>
              <span class="section-note">统计口径为 UTC 自然日；超时策略在 Run 创建时冻结</span>
            </div>
            <el-tag
              v-if="executionPolicy"
              :type="executionPolicy.quota_status === 'BLOCKED' ? 'danger' : executionPolicy.quota_status === 'NEAR_LIMIT' ? 'warning' : 'success'"
              effect="light"
            >{{ policyStatusLabel }}</el-tag>
          </div>

          <div v-if="executionPolicy" class="quota-layout">
            <section class="quota-overview">
              <article class="quota-meter">
                <header>
                  <div><strong>在途 Run</strong><span>QUEUED + PREPARING + RUNNING</span></div>
                  <b>{{ executionPolicy.in_flight_runs }} / {{ executionPolicy.max_in_flight_runs }}</b>
                </header>
                <el-progress
                  :percentage="inFlightPercent"
                  :status="executionPolicy.remaining_in_flight_runs === 0 ? 'exception' : inFlightPercent >= 80 ? 'warning' : 'success'"
                  :stroke-width="10"
                />
                <div class="quota-breakdown">
                  <span>排队 {{ executionPolicy.queued_runs }}</span>
                  <span>准备 {{ executionPolicy.preparing_runs }}</span>
                  <span>执行 {{ executionPolicy.running_runs }}</span>
                  <span>剩余 {{ executionPolicy.remaining_in_flight_runs }}</span>
                </div>
              </article>

              <article class="quota-meter">
                <header>
                  <div><strong>今日创建量</strong><span>窗口开始 {{ formatUtcDate(executionPolicy.daily_window_started_at) }}</span></div>
                  <b>{{ executionPolicy.runs_created_today }} / {{ executionPolicy.max_daily_runs }}</b>
                </header>
                <el-progress
                  :percentage="dailyPercent"
                  :status="executionPolicy.remaining_daily_runs === 0 ? 'exception' : dailyPercent >= 80 ? 'warning' : 'success'"
                  :stroke-width="10"
                />
                <div class="quota-breakdown">
                  <span>已创建 {{ executionPolicy.runs_created_today }}</span>
                  <span>剩余 {{ executionPolicy.remaining_daily_runs }}</span>
                </div>
              </article>
            </section>

            <el-form label-position="top" class="policy-form">
              <h3>准入上限</h3>
              <p>普通执行、验证、回归和重跑使用同一套项目配额。</p>
              <el-form-item label="最大在途 Run 数">
                <el-input-number
                  :key="`in-flight-${executionPolicy.max_in_flight_runs}`"
                  v-model="policyForm.max_in_flight_runs"
                  :min="1"
                  :max="500"
                  :disabled="!canManage"
                  controls-position="right"
                />
              </el-form-item>
              <el-form-item label="每日最大创建量">
                <el-input-number
                  :key="`daily-${executionPolicy.max_daily_runs}`"
                  v-model="policyForm.max_daily_runs"
                  :min="1"
                  :max="100000"
                  :disabled="!canManage"
                  controls-position="right"
                />
              </el-form-item>
              <el-form-item label="单次 Run 超时（秒）">
                <el-input-number
                  :key="`timeout-${executionPolicy.run_timeout_seconds}`"
                  v-model="policyForm.run_timeout_seconds"
                  :min="60"
                  :max="86400"
                  :step="60"
                  :disabled="!canManage"
                  controls-position="right"
                />
              </el-form-item>
              <el-button
                v-if="canManage"
                type="primary"
                :loading="policyBusy"
                @click="saveExecutionPolicy"
              >保存执行策略</el-button>
              <span class="policy-updated">最近更新 {{ formatDate(executionPolicy.updated_at) }}</span>
            </el-form>
          </div>
        </el-tab-pane>
      </el-tabs>
    </section>

    <el-dialog v-model="memberVisible" title="添加项目成员" width="540px" destroy-on-close>
      <el-form label-position="top">
        <el-form-item label="查找平台用户">
          <el-input v-model="memberQuery" clearable placeholder="用户名或显示名" @keyup.enter="searchCandidates">
            <template #append><el-button @click="searchCandidates">搜索</el-button></template>
          </el-input>
        </el-form-item>
        <el-form-item label="用户" required>
          <el-select v-model="memberForm.user_id" filterable style="width: 100%">
            <el-option
              v-for="candidate in candidates"
              :key="candidate.id"
              :label="`${candidate.display_name} · ${candidate.username}`"
              :value="candidate.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="项目角色" required>
          <el-select v-model="memberForm.role" style="width: 100%">
            <el-option v-for="(label, role) in roleLabels" :key="role" :label="label" :value="role" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="memberVisible = false">取消</el-button>
        <el-button type="primary" :loading="memberBusy" @click="saveMember">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="targetVisible" title="创建测试目标" width="540px" destroy-on-close>
      <el-form label-position="top">
        <el-form-item label="目标标识" required><el-input v-model="targetForm.key" placeholder="web" /></el-form-item>
        <el-form-item label="目标名称" required><el-input v-model="targetForm.name" /></el-form-item>
        <el-form-item label="目标类型" required>
          <el-select v-model="targetForm.target_type" style="width: 100%">
            <el-option label="Web" value="WEB" />
            <el-option label="API" value="API" />
            <el-option label="App" value="APP" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="targetForm.target_type === 'WEB'" label="浏览器">
          <el-select v-model="targetForm.browser" style="width: 100%">
            <el-option label="Chromium" value="chromium" />
            <el-option label="Firefox" value="firefox" />
            <el-option label="WebKit" value="webkit" />
          </el-select>
        </el-form-item>
        <el-form-item label="Runner Pool">
          <el-select v-model="targetForm.runner_pool_id" clearable placeholder="未绑定，使用默认队列" style="width: 100%">
            <el-option
              v-for="pool in compatiblePools(targetForm.target_type)"
              :key="pool.id"
              :label="`${pool.name} · ${pool.available_slots} 槽可用`"
              :value="pool.id"
            />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="targetVisible = false">取消</el-button>
        <el-button type="primary" :loading="targetBusy" @click="createTarget">创建</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="environmentVisible" title="创建运行环境" width="580px" destroy-on-close>
      <el-alert
        title="配置中只允许填写 secret:// 引用，平台不会保存实际密钥值。"
        type="info"
        :closable="false"
        show-icon
      />
      <el-form label-position="top" class="dialog-form">
        <div class="form-grid">
          <el-form-item label="环境标识" required><el-input v-model="environmentForm.key" /></el-form-item>
          <el-form-item label="环境名称" required><el-input v-model="environmentForm.name" /></el-form-item>
        </div>
        <el-form-item v-if="environmentTarget?.target_type === 'WEB'" label="Base URL" required>
          <el-input v-model="environmentForm.base_url" placeholder="https://test.example.com" />
        </el-form-item>
        <el-form-item label="Runner Pool（可覆盖目标绑定）">
          <el-select v-model="environmentForm.runner_pool_id" clearable placeholder="继承目标绑定" style="width: 100%">
            <el-option
              v-for="pool in compatiblePools(environmentTarget?.target_type || 'WEB')"
              :key="pool.id"
              :label="`${pool.name} · ${pool.available_slots} 槽可用`"
              :value="pool.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="Secret 引用（每行 NAME=secret://path）">
          <el-input v-model="environmentForm.secret_bindings" type="textarea" :rows="5" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="environmentVisible = false">取消</el-button>
        <el-button type="primary" :loading="environmentBusy" @click="createEnvironment">创建</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="environmentEditVisible" title="编辑运行环境" width="580px" destroy-on-close>
      <el-form label-position="top">
        <el-form-item label="环境名称" required><el-input v-model="environmentEditForm.name" /></el-form-item>
        <el-form-item label="状态">
          <el-select v-model="environmentEditForm.status" style="width: 100%">
            <el-option label="启用" value="ACTIVE" />
            <el-option label="归档" value="ARCHIVED" />
          </el-select>
        </el-form-item>
        <el-form-item label="Runner Pool（可覆盖目标绑定）">
          <el-select v-model="environmentEditForm.runner_pool_id" clearable placeholder="继承目标绑定" style="width: 100%">
            <el-option
              v-for="pool in compatiblePools(environmentEditTarget?.target_type || 'WEB')"
              :key="pool.id"
              :label="`${pool.name} · ${pool.available_slots} 槽可用`"
              :value="pool.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="Secret 引用（每行 NAME=secret://path）">
          <el-input v-model="environmentEditForm.secret_bindings" type="textarea" :rows="6" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="environmentEditVisible = false">取消</el-button>
        <el-button type="primary" :loading="environmentEditBusy" @click="saveEnvironment">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="scheduleVisible"
      :title="scheduleEditId ? '编辑回归计划' : '创建回归计划'"
      width="760px"
      destroy-on-close
    >
      <el-alert
        title="Cron 使用 5 段数字格式：分钟 小时 日 月 星期。下次派发时间会明确显示为 UTC。"
        type="info"
        :closable="false"
        show-icon
      />
      <el-form label-position="top" class="dialog-form">
        <div class="form-grid">
          <el-form-item label="计划标识" required>
            <el-input v-model="scheduleForm.key" :disabled="Boolean(scheduleEditId)" placeholder="nightly-login" />
          </el-form-item>
          <el-form-item label="计划名称" required>
            <el-input v-model="scheduleForm.name" placeholder="每日登录回归" />
          </el-form-item>
        </div>
        <el-form-item label="说明"><el-input v-model="scheduleForm.description" /></el-form-item>
        <div class="form-grid">
          <el-form-item label="测试目标" required>
            <el-select v-model="scheduleForm.target_id" style="width: 100%" @change="scheduleTargetChanged">
              <el-option
                v-for="target in targets.filter((item) => item.status === 'ACTIVE' || item.id === scheduleForm.target_id)"
                :key="target.id"
                :label="`${target.name} · ${target.target_type}`"
                :value="target.id"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="运行环境" required>
            <el-select v-model="scheduleForm.environment_id" style="width: 100%">
              <el-option
                v-for="environment in scheduleEnvironments"
                :key="environment.id"
                :label="environment.name"
                :value="environment.id"
                :disabled="environment.status !== 'ACTIVE'"
              />
            </el-select>
          </el-form-item>
        </div>
        <div class="form-grid">
          <el-form-item label="Released 基线" required>
            <el-select v-model="scheduleForm.baseline_id" style="width: 100%">
              <el-option
                v-for="baseline in baselines.filter((item) => item.status === 'RELEASED')"
                :key="baseline.baseline_id"
                :label="`${baseline.version} · ${baseline.enabled_case_count} 启用`"
                :value="baseline.baseline_id"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="自动化包" required>
            <el-select v-model="scheduleForm.automation_package_id" style="width: 100%">
              <el-option
                v-for="item in schedulePackages"
                :key="item.id"
                :label="`${item.name}@${item.version}`"
                :value="item.id"
              />
            </el-select>
          </el-form-item>
        </div>
        <el-form-item label="用例编号（逗号或空格分隔；留空表示全部启用用例）">
          <el-input v-model="scheduleForm.case_codes" type="textarea" :rows="2" placeholder="TC-LOGIN-001, TC-LOGIN-007" />
        </el-form-item>
        <div class="form-grid">
          <el-form-item label="Cron 表达式" required>
            <el-input v-model="scheduleForm.cron_expression" placeholder="0 2 * * *" />
          </el-form-item>
          <el-form-item label="IANA 时区" required>
            <el-select
              v-model="scheduleForm.timezone"
              filterable
              allow-create
              default-first-option
              style="width: 100%"
            >
              <el-option label="Asia/Shanghai" value="Asia/Shanghai" />
              <el-option label="UTC" value="UTC" />
              <el-option label="America/New_York" value="America/New_York" />
              <el-option label="Europe/London" value="Europe/London" />
            </el-select>
          </el-form-item>
        </div>
        <div class="form-grid schedule-policy-grid">
          <el-form-item label="错过触发策略">
            <el-select v-model="scheduleForm.misfire_policy" style="width: 100%">
              <el-option label="补触发一次" value="FIRE_ONCE" />
              <el-option label="跳过" value="SKIP" />
            </el-select>
          </el-form-item>
          <el-form-item label="宽限时间（秒）">
            <el-input-number v-model="scheduleForm.misfire_grace_seconds" :min="60" :max="86400" style="width: 100%" />
          </el-form-item>
          <el-form-item label="状态">
            <el-select v-model="scheduleForm.status" style="width: 100%">
              <el-option label="启用" value="ACTIVE" />
              <el-option label="暂停" value="PAUSED" />
            </el-select>
          </el-form-item>
        </div>
        <div class="schedule-summary">
          当前目标：{{ scheduleTarget?.name || '未选择' }}；计划只保存资源版本 ID，触发时生成不可变 Run Snapshot。
        </div>
      </el-form>
      <template #footer>
        <el-button @click="scheduleVisible = false">取消</el-button>
        <el-button type="primary" :loading="scheduleBusy" @click="saveSchedule">保存计划</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="firingVisible"
      :title="`${firingSchedule?.name || '回归计划'} · 触发记录`"
      width="820px"
      destroy-on-close
    >
      <el-table v-loading="firingLoading" :data="firings" empty-text="暂无触发记录">
        <el-table-column label="计划时间（UTC）" min-width="205">
          <template #default="scope">{{ formatUtcDate(scope.row.scheduled_for) }}</template>
        </el-table-column>
        <el-table-column label="类型" width="120">
          <template #default="scope"><el-tag effect="plain">{{ scope.row.trigger_kind }}</el-tag></template>
        </el-table-column>
        <el-table-column label="结果" width="120">
          <template #default="scope">
            <el-tag :type="scope.row.status === 'TRIGGERED' ? 'success' : scope.row.status === 'BLOCKED' ? 'danger' : 'info'">
              {{ scope.row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="Run / 原因" min-width="290">
          <template #default="scope">
            <el-button
              v-if="scope.row.run_id"
              link
              type="primary"
              @click="router.push({ name: 'run-detail', params: { projectId, runId: scope.row.run_id } })"
            >{{ scope.row.run_id }}</el-button>
            <span v-else class="schedule-error">{{ scope.row.error_message || '未创建 Run' }}</span>
          </template>
        </el-table-column>
      </el-table>
    </el-dialog>
  </div>
</template>

<style scoped>
.back-link {
  margin: -8px 0 10px -12px;
  color: #66788a;
}

.settings-heading {
  align-items: center;
}

.project-key {
  color: #168579;
  font: 700 11px "SFMono-Regular", Consolas, monospace;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
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

.metric-card strong {
  color: #17324d;
  font-size: 23px;
}

.metric-card span {
  color: #718096;
  font-size: 12px;
}

.permission-alert {
  margin-bottom: 18px;
}

.settings-surface {
  min-height: 420px;
}

.settings-tabs :deep(.el-tabs__header) {
  margin: 0;
  padding: 0 20px;
}

.section-note {
  margin-left: 10px;
  color: #8a98a6;
  font-size: 12px;
  font-weight: 400;
}

.target-block + .target-block {
  border-top: 12px solid #f4f6f8;
}

.target-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 18px 20px;
  border-top: 1px solid #edf0f2;
  background: #fbfcfd;
}

.target-title,
.target-actions {
  display: flex;
  align-items: center;
  gap: 9px;
}

.target-title strong {
  color: #17324d;
}

.target-title code {
  color: #168579;
}

.pool-select {
  width: 220px;
}

.target-note {
  display: block;
  margin-top: 5px;
  color: #8795a5;
  font-size: 11px;
}

.binding {
  display: block;
  margin: 2px 0;
  color: #4f6478;
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

.schedule-policy-grid {
  grid-template-columns: 1.15fr 1fr 0.85fr;
}

.schedule-error {
  margin-top: 4px;
  color: #c45656;
  font-size: 11px;
  line-height: 1.4;
}

.schedule-summary {
  padding: 11px 13px;
  border-radius: 8px;
  color: #587083;
  background: #f3f7f8;
  font-size: 12px;
  line-height: 1.6;
}

.quota-layout {
  display: grid;
  grid-template-columns: minmax(0, 1.65fr) minmax(280px, 0.75fr);
  gap: 22px;
  padding: 22px;
  border-top: 1px solid #edf0f2;
  background: #f8fafb;
}

.quota-overview {
  display: grid;
  gap: 16px;
}

.quota-meter,
.policy-form {
  padding: 20px;
  border: 1px solid #e1e8ec;
  border-radius: 12px;
  background: #fff;
}

.quota-meter header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 18px;
}

.quota-meter header div {
  display: grid;
  gap: 5px;
}

.quota-meter header strong,
.policy-form h3 {
  color: #17324d;
}

.quota-meter header span,
.policy-form p,
.policy-updated {
  color: #7b8998;
  font-size: 12px;
}

.quota-meter header b {
  color: #167d73;
  font-size: 21px;
  white-space: nowrap;
}

.quota-breakdown {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 14px;
}

.quota-breakdown span {
  padding: 5px 9px;
  border-radius: 999px;
  color: #53687c;
  background: #f1f5f6;
  font-size: 11px;
}

.policy-form h3 {
  margin: 0 0 6px;
}

.policy-form p {
  margin: 0 0 20px;
  line-height: 1.6;
}

.policy-form :deep(.el-input-number) {
  width: 100%;
}

.policy-updated {
  display: block;
  margin-top: 14px;
}

@media (max-width: 1080px) {
  .metric-grid {
    grid-template-columns: repeat(2, 1fr);
  }

  .quota-layout {
    grid-template-columns: 1fr;
  }

  .schedule-policy-grid {
    grid-template-columns: 1fr;
  }

  .target-header,
  .target-actions {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
