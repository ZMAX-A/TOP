<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Key, Monitor, Plus, Refresh, Tickets, User, VideoPause } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'

import { ApiError, api } from '@/api/client'
import type {
  AuditLog,
  ManagedSession,
  Project,
  RunnerPool,
  RunnerWorker,
  SystemRole,
  User as ManagedUser,
  UserStatus,
} from '@/api/types'
import { auth } from '@/auth'
import { formatDate } from '@/presentation'

const route = useRoute()
const router = useRouter()
const allowedTabs = new Set(['users', 'projects', 'runners', 'sessions', 'audit'])
const requestedTab = typeof route.query.view === 'string' ? route.query.view : 'users'
const activeTab = ref(allowedTabs.has(requestedTab) ? requestedTab : 'users')
const loading = ref(false)

const users = ref<ManagedUser[]>([])
const userTotal = ref(0)
const userOffset = ref(0)
const userLimit = 20
const userFilters = reactive<{ query: string; status: '' | UserStatus; system_role: '' | SystemRole }>({
  query: '',
  status: '',
  system_role: '',
})

const projects = ref<Project[]>([])
const sessions = ref<ManagedSession[]>([])
const sessionTotal = ref(0)
const sessionOffset = ref(0)
const activeSessionsOnly = ref(true)
const auditLogs = ref<AuditLog[]>([])
const auditTotal = ref(0)
const auditOffset = ref(0)
const auditAction = ref('')
const runnerPools = ref<RunnerPool[]>([])
const runnerWorkers = ref<RunnerWorker[]>([])

const userCreateVisible = ref(false)
const userCreateBusy = ref(false)
const userForm = reactive({
  username: '',
  display_name: '',
  password: '',
  system_role: 'USER' as SystemRole,
})
const passwordVisible = ref(false)
const passwordBusy = ref(false)
const passwordUser = ref<ManagedUser>()
const replacementPassword = ref('')
const projectCreateVisible = ref(false)
const projectCreateBusy = ref(false)
const projectForm = reactive({ key: '', name: '', description: '' })
const runnerPoolVisible = ref(false)
const runnerPoolBusy = ref(false)
const runnerPoolForm = reactive({
  key: '',
  name: '',
  description: '',
  target_types: ['WEB'] as Array<'WEB' | 'APP' | 'API'>,
  max_concurrency: 1,
})

const activeSessionCount = computed(() => sessions.value.filter((item) => item.active).length)
const healthyRunnerCount = computed(
  () => runnerWorkers.value.filter((item) => item.health === 'ONLINE' && item.status === 'ACTIVE').length,
)

function report(error: unknown, fallback: string): void {
  ElMessage.error(error instanceof ApiError ? error.message : fallback)
}

async function loadUsers(): Promise<void> {
  loading.value = true
  try {
    const page = await api.adminUsers({
      query: userFilters.query || undefined,
      status: userFilters.status || undefined,
      system_role: userFilters.system_role || undefined,
      offset: userOffset.value,
      limit: userLimit,
    })
    users.value = page.items
    userTotal.value = page.total
  } catch (error) {
    report(error, '用户列表加载失败')
  } finally {
    loading.value = false
  }
}

async function loadProjects(): Promise<void> {
  loading.value = true
  try {
    projects.value = await api.projects()
  } catch (error) {
    report(error, '项目列表加载失败')
  } finally {
    loading.value = false
  }
}

async function loadSessions(): Promise<void> {
  loading.value = true
  try {
    const page = await api.adminSessions({
      active_only: activeSessionsOnly.value,
      offset: sessionOffset.value,
      limit: userLimit,
    })
    sessions.value = page.items
    sessionTotal.value = page.total
  } catch (error) {
    report(error, '会话列表加载失败')
  } finally {
    loading.value = false
  }
}

async function loadAudit(): Promise<void> {
  loading.value = true
  try {
    const page = await api.auditLogs({
      action: auditAction.value || undefined,
      offset: auditOffset.value,
      limit: userLimit,
    })
    auditLogs.value = page.items
    auditTotal.value = page.total
  } catch (error) {
    report(error, '审计日志加载失败')
  } finally {
    loading.value = false
  }
}

async function loadRunners(): Promise<void> {
  loading.value = true
  try {
    const [pools, workers] = await Promise.all([
      api.adminRunnerPools(),
      api.adminRunnerWorkers(),
    ])
    runnerPools.value = pools
    runnerWorkers.value = workers
  } catch (error) {
    report(error, 'Runner Pool 加载失败')
  } finally {
    loading.value = false
  }
}

async function loadActiveTab(): Promise<void> {
  if (activeTab.value === 'users') await loadUsers()
  else if (activeTab.value === 'projects') await loadProjects()
  else if (activeTab.value === 'runners') await loadRunners()
  else if (activeTab.value === 'sessions') await loadSessions()
  else await loadAudit()
}

async function updateManagedUser(
  user: ManagedUser,
  payload: { system_role?: SystemRole; status?: UserStatus },
): Promise<void> {
  try {
    const updated = await api.updateUser(user.id, payload)
    const index = users.value.findIndex((item) => item.id === updated.id)
    if (index >= 0) users.value[index] = updated
    ElMessage.success('用户状态已更新')
  } catch (error) {
    report(error, '用户更新失败')
  }
}

function openCreateUser(): void {
  Object.assign(userForm, { username: '', display_name: '', password: '', system_role: 'USER' })
  userCreateVisible.value = true
}

async function createUser(): Promise<void> {
  if (!userForm.username || !userForm.display_name || userForm.password.length < 12) {
    ElMessage.warning('请完整填写用户信息，初始密码至少 12 位')
    return
  }
  userCreateBusy.value = true
  try {
    await api.createUser(userForm)
    userCreateVisible.value = false
    ElMessage.success('用户已创建')
    await loadUsers()
  } catch (error) {
    report(error, '用户创建失败')
  } finally {
    userCreateBusy.value = false
  }
}

function openPasswordReset(user: ManagedUser): void {
  passwordUser.value = user
  replacementPassword.value = ''
  passwordVisible.value = true
}

async function resetPassword(): Promise<void> {
  if (!passwordUser.value || replacementPassword.value.length < 12) {
    ElMessage.warning('新密码至少 12 位')
    return
  }
  passwordBusy.value = true
  try {
    await api.resetUserPassword(passwordUser.value.id, replacementPassword.value)
    passwordVisible.value = false
    ElMessage.success('密码已重置，该用户现有会话已全部吊销')
  } catch (error) {
    report(error, '密码重置失败')
  } finally {
    passwordBusy.value = false
  }
}

async function revokeSession(session: ManagedSession): Promise<void> {
  try {
    await api.revokeSession(session.id)
    ElMessage.success('会话已吊销')
    await loadSessions()
  } catch (error) {
    report(error, '会话吊销失败')
  }
}

function openCreateProject(): void {
  Object.assign(projectForm, { key: '', name: '', description: '' })
  projectCreateVisible.value = true
}

async function createProject(): Promise<void> {
  if (!projectForm.key || !projectForm.name) {
    ElMessage.warning('项目标识和名称不能为空')
    return
  }
  projectCreateBusy.value = true
  try {
    const created = await api.createProject({
      key: projectForm.key,
      name: projectForm.name,
      description: projectForm.description || null,
    })
    projectCreateVisible.value = false
    ElMessage.success('项目已创建，当前管理员已成为项目管理员')
    await router.push({ name: 'project-settings', params: { projectId: created.id } })
  } catch (error) {
    report(error, '项目创建失败')
  } finally {
    projectCreateBusy.value = false
  }
}

function openRunnerPool(): void {
  Object.assign(runnerPoolForm, {
    key: '',
    name: '',
    description: '',
    target_types: ['WEB'],
    max_concurrency: 1,
  })
  runnerPoolVisible.value = true
}

async function createRunnerPool(): Promise<void> {
  if (!runnerPoolForm.key || !runnerPoolForm.name || runnerPoolForm.target_types.length === 0) {
    ElMessage.warning('请完整填写 Runner Pool 信息')
    return
  }
  runnerPoolBusy.value = true
  try {
    await api.createRunnerPool({
      ...runnerPoolForm,
      description: runnerPoolForm.description || null,
    })
    runnerPoolVisible.value = false
    ElMessage.success('Runner Pool 已创建')
    await loadRunners()
  } catch (error) {
    report(error, 'Runner Pool 创建失败')
  } finally {
    runnerPoolBusy.value = false
  }
}

async function updateRunnerPool(pool: RunnerPool, payload: Record<string, unknown>): Promise<void> {
  try {
    await api.updateRunnerPool(pool.id, payload)
    ElMessage.success('Runner Pool 已更新')
    await loadRunners()
  } catch (error) {
    report(error, 'Runner Pool 更新失败')
  }
}

async function updateRunnerWorker(worker: RunnerWorker, status: RunnerWorker['status']): Promise<void> {
  try {
    await api.updateRunnerWorker(worker.id, { status })
    ElMessage.success('Runner Worker 状态已更新')
    await loadRunners()
  } catch (error) {
    report(error, 'Runner Worker 更新失败')
  }
}

function poolCapacityPercent(pool: RunnerPool): number {
  const effective = Math.min(pool.max_concurrency, pool.total_worker_slots)
  return effective ? Math.min(100, Math.round((pool.active_leases / effective) * 100)) : 0
}

async function toggleProject(project: Project): Promise<void> {
  try {
    await api.updateProject(project.id, {
      status: project.status === 'ACTIVE' ? 'ARCHIVED' : 'ACTIVE',
    })
    ElMessage.success(project.status === 'ACTIVE' ? '项目已归档' : '项目已恢复')
    await loadProjects()
  } catch (error) {
    report(error, '项目状态更新失败')
  }
}

function changeUserPage(page: number): void {
  userOffset.value = (page - 1) * userLimit
  void loadUsers()
}

function changeSessionPage(page: number): void {
  sessionOffset.value = (page - 1) * userLimit
  void loadSessions()
}

function changeAuditPage(page: number): void {
  auditOffset.value = (page - 1) * userLimit
  void loadAudit()
}

watch(activeTab, (value) => {
  void router.replace({ query: value === 'users' ? {} : { view: value } })
  void loadActiveTab()
})

onMounted(loadActiveTab)
</script>

<template>
  <div class="page-container">
    <header class="page-heading">
      <div>
        <span class="eyebrow-dark">System administration</span>
        <h1>系统管理中心</h1>
        <p>集中管理平台账号、项目空间、Runner Pool、登录会话和不可变审计记录。</p>
      </div>
      <el-button :icon="Refresh" @click="loadActiveTab">刷新当前视图</el-button>
    </header>

    <div class="metric-grid">
      <article class="metric-card">
        <el-icon><User /></el-icon><div><strong>{{ userTotal }}</strong><span>筛选用户</span></div>
      </article>
      <article class="metric-card">
        <el-icon><Tickets /></el-icon><div><strong>{{ projects.length }}</strong><span>项目空间</span></div>
      </article>
      <article class="metric-card">
        <el-icon><Key /></el-icon><div><strong>{{ activeSessionCount }}</strong><span>当前页活跃会话</span></div>
      </article>
      <article class="metric-card">
        <el-icon><Monitor /></el-icon><div><strong>{{ healthyRunnerCount }}</strong><span>健康 Runner</span></div>
      </article>
    </div>

    <section class="surface admin-surface" v-loading="loading">
      <el-tabs v-model="activeTab" class="admin-tabs">
        <el-tab-pane label="用户管理" name="users">
          <div class="toolbar filter-toolbar">
            <div class="filters">
              <el-input
                v-model="userFilters.query"
                clearable
                placeholder="搜索用户名或显示名"
                @keyup.enter="userOffset = 0; loadUsers()"
              />
              <el-select v-model="userFilters.status" clearable placeholder="账号状态">
                <el-option label="启用" value="ACTIVE" />
                <el-option label="禁用" value="DISABLED" />
              </el-select>
              <el-select v-model="userFilters.system_role" clearable placeholder="系统角色">
                <el-option label="普通用户" value="USER" />
                <el-option label="系统管理员" value="SYSTEM_ADMIN" />
              </el-select>
              <el-button @click="userOffset = 0; loadUsers()">筛选</el-button>
            </div>
            <el-button type="primary" :icon="Plus" @click="openCreateUser">创建用户</el-button>
          </div>
          <el-table :data="users" empty-text="暂无匹配用户">
            <el-table-column label="用户" min-width="220">
              <template #default="scope">
                <strong>{{ scope.row.display_name }}</strong>
                <div class="muted mono">{{ scope.row.username }}</div>
              </template>
            </el-table-column>
            <el-table-column label="系统角色" width="180">
              <template #default="scope">
                <el-select
                  :model-value="scope.row.system_role"
                  :disabled="scope.row.id === auth.state.user?.id"
                  @change="updateManagedUser(scope.row as ManagedUser, { system_role: $event })"
                >
                  <el-option label="普通用户" value="USER" />
                  <el-option label="系统管理员" value="SYSTEM_ADMIN" />
                </el-select>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="145">
              <template #default="scope">
                <el-select
                  :model-value="scope.row.status"
                  :disabled="scope.row.id === auth.state.user?.id"
                  @change="updateManagedUser(scope.row as ManagedUser, { status: $event })"
                >
                  <el-option label="启用" value="ACTIVE" />
                  <el-option label="禁用" value="DISABLED" />
                </el-select>
              </template>
            </el-table-column>
            <el-table-column label="创建时间" width="180">
              <template #default="scope">{{ formatDate(scope.row.created_at) }}</template>
            </el-table-column>
            <el-table-column label="操作" width="120" fixed="right">
              <template #default="scope">
                <el-button link type="primary" @click="openPasswordReset(scope.row as ManagedUser)">重置密码</el-button>
              </template>
            </el-table-column>
          </el-table>
          <el-pagination
            v-if="userTotal > userLimit"
            class="pagination"
            layout="prev, pager, next, total"
            :page-size="userLimit"
            :total="userTotal"
            :current-page="Math.floor(userOffset / userLimit) + 1"
            @current-change="changeUserPage"
          />
        </el-tab-pane>

        <el-tab-pane label="项目管理" name="projects">
          <div class="toolbar">
            <div><strong>全局项目</strong><span class="section-note">创建者自动成为 Project Admin</span></div>
            <el-button type="primary" :icon="Plus" @click="openCreateProject">创建项目</el-button>
          </div>
          <el-table :data="projects" empty-text="暂无项目">
            <el-table-column label="项目" min-width="280">
              <template #default="scope">
                <span
                  class="table-link"
                  @click="router.push({ name: 'project', params: { projectId: scope.row.id } })"
                >{{ scope.row.name }}</span>
                <div class="muted mono">{{ scope.row.key }}</div>
              </template>
            </el-table-column>
            <el-table-column prop="description" label="说明" min-width="300" />
            <el-table-column label="状态" width="120">
              <template #default="scope"><el-tag>{{ scope.row.status }}</el-tag></template>
            </el-table-column>
            <el-table-column label="更新时间" width="180">
              <template #default="scope">{{ formatDate(scope.row.updated_at) }}</template>
            </el-table-column>
            <el-table-column label="操作" width="170" fixed="right">
              <template #default="scope">
                <el-button
                  link
                  type="primary"
                  @click="router.push({ name: 'project-settings', params: { projectId: scope.row.id } })"
                >管理</el-button>
                <el-button link @click="toggleProject(scope.row as Project)">
                  {{ scope.row.status === 'ACTIVE' ? '归档' : '恢复' }}
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <el-tab-pane label="Runner Pool" name="runners">
          <div class="toolbar">
            <div>
              <strong>执行资源池</strong>
              <span class="section-note">健康窗口、能力标签与池级槽位共同决定 Run 是否可派发</span>
            </div>
            <el-button type="primary" :icon="Plus" @click="openRunnerPool">创建资源池</el-button>
          </div>
          <el-table :data="runnerPools" empty-text="暂无 Runner Pool">
            <el-table-column label="资源池" min-width="240">
              <template #default="scope">
                <strong>{{ scope.row.name }}</strong>
                <div class="muted mono">{{ scope.row.key }} · {{ scope.row.queue_name }}</div>
              </template>
            </el-table-column>
            <el-table-column label="目标类型" width="150">
              <template #default="scope">
                <el-tag v-for="item in scope.row.target_types" :key="item" size="small">{{ item }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="实时容量" min-width="230">
              <template #default="scope">
                <div class="capacity-copy">
                  <span>{{ scope.row.active_leases }} 使用中 / {{ Math.min(scope.row.max_concurrency, scope.row.total_worker_slots) }} 有效槽位</span>
                  <span>{{ scope.row.healthy_workers }} 个健康 Worker</span>
                </div>
                <el-progress
                  :percentage="poolCapacityPercent(scope.row as RunnerPool)"
                  :show-text="false"
                  :status="scope.row.available_slots === 0 ? 'exception' : 'success'"
                />
              </template>
            </el-table-column>
            <el-table-column label="池上限" width="130">
              <template #default="scope">
                <el-input-number
                  :model-value="scope.row.max_concurrency"
                  :min="1"
                  :max="500"
                  controls-position="right"
                  @change="updateRunnerPool(scope.row as RunnerPool, { max_concurrency: $event })"
                />
              </template>
            </el-table-column>
            <el-table-column label="状态" width="150">
              <template #default="scope">
                <el-select
                  :model-value="scope.row.status"
                  @change="updateRunnerPool(scope.row as RunnerPool, { status: $event })"
                >
                  <el-option label="接收任务" value="ACTIVE" />
                  <el-option label="排空中" value="DRAINING" />
                  <el-option label="已禁用" value="DISABLED" />
                </el-select>
              </template>
            </el-table-column>
          </el-table>

          <div class="toolbar runner-worker-heading">
            <div>
              <strong>Runner Worker</strong>
              <span class="section-note">超过心跳 TTL 未上报会自动显示为 STALE，并停止贡献槽位</span>
            </div>
          </div>
          <el-table :data="runnerWorkers" empty-text="尚无 Worker 心跳">
            <el-table-column label="Worker" min-width="240">
              <template #default="scope">
                <strong>{{ scope.row.display_name }}</strong>
                <div class="muted mono">{{ scope.row.worker_key }} · v{{ scope.row.runner_version }}</div>
              </template>
            </el-table-column>
            <el-table-column prop="pool_key" label="资源池" width="170" />
            <el-table-column label="能力" min-width="260">
              <template #default="scope">
                <span>{{ scope.row.capabilities.target_types.join(', ') }}</span>
                <div class="muted">浏览器：{{ scope.row.capabilities.browsers.join(', ') || '不适用' }}</div>
                <div class="muted">
                  不可变包：{{ scope.row.capabilities.automation_packages.length }}
                </div>
                <div class="muted">
                  执行隔离：{{ scope.row.capabilities.execution_isolation?.mode || '未声明' }}
                  · {{ scope.row.capabilities.execution_isolation?.credential_scope || '未知凭据范围' }}
                </div>
                <div v-if="scope.row.capabilities.execution_isolation" class="muted">
                  网络：{{ scope.row.capabilities.execution_isolation.network_policy }}
                  · 只读根：{{ scope.row.capabilities.execution_isolation.read_only_root_filesystem ? '是' : '否' }}
                  · 资源限制：{{ scope.row.capabilities.execution_isolation.resource_limits_enforced ? '是' : '否' }}
                </div>
                <div
                  v-if="scope.row.capabilities.execution_isolation?.orchestrator_namespace"
                  class="muted"
                >
                  Namespace：{{ scope.row.capabilities.execution_isolation.orchestrator_namespace }}
                  · ServiceAccount：{{ scope.row.capabilities.execution_isolation.service_account_name }}
                  · Token 自动挂载：{{ scope.row.capabilities.execution_isolation.service_account_token_automounted ? '是' : '否' }}
                </div>
                <div
                  v-for="runtime in scope.row.capabilities.automation_packages"
                  :key="`${runtime.runner_type}:${runtime.image_repository}@${runtime.digest}`"
                  class="muted mono"
                >
                  {{ runtime.image_repository }}@{{ runtime.digest.slice(0, 18) }}…
                </div>
              </template>
            </el-table-column>
            <el-table-column prop="max_slots" label="槽位" width="80" />
            <el-table-column label="健康" width="110">
              <template #default="scope">
                <el-tag :type="scope.row.health === 'ONLINE' ? 'success' : 'danger'">{{ scope.row.health }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="最近心跳" width="180">
              <template #default="scope">{{ formatDate(scope.row.last_heartbeat_at) }}</template>
            </el-table-column>
            <el-table-column label="状态" width="145">
              <template #default="scope">
                <el-select
                  :model-value="scope.row.status"
                  @change="updateRunnerWorker(scope.row as RunnerWorker, $event)"
                >
                  <el-option label="启用" value="ACTIVE" />
                  <el-option label="排空" value="DRAINING" />
                  <el-option label="禁用" value="DISABLED" />
                </el-select>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <el-tab-pane label="会话管理" name="sessions">
          <div class="toolbar filter-toolbar">
            <div>
              <strong>登录会话</strong>
              <span class="section-note">仅展示 Token 摘要对应的元数据，不返回 Token</span>
            </div>
            <el-switch
              v-model="activeSessionsOnly"
              active-text="仅活跃会话"
              @change="sessionOffset = 0; loadSessions()"
            />
          </div>
          <el-table :data="sessions" empty-text="暂无会话">
            <el-table-column label="用户" min-width="190">
              <template #default="scope">
                <strong>{{ scope.row.display_name }}</strong>
                <div class="muted mono">{{ scope.row.username }}</div>
              </template>
            </el-table-column>
            <el-table-column label="Session ID" min-width="260">
              <template #default="scope"><code>{{ scope.row.id }}</code></template>
            </el-table-column>
            <el-table-column label="状态" width="110">
              <template #default="scope">
                <el-tag :type="scope.row.active ? 'success' : 'info'">
                  {{ scope.row.active ? '活跃' : '已失效' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="创建时间" width="180">
              <template #default="scope">{{ formatDate(scope.row.created_at) }}</template>
            </el-table-column>
            <el-table-column label="到期时间" width="180">
              <template #default="scope">{{ formatDate(scope.row.expires_at) }}</template>
            </el-table-column>
            <el-table-column label="操作" width="100" fixed="right">
              <template #default="scope">
                <el-popconfirm
                  v-if="scope.row.active"
                  title="确定吊销这个登录会话？"
                  @confirm="revokeSession(scope.row as ManagedSession)"
                >
                  <template #reference>
                    <el-button link type="danger" :icon="VideoPause">吊销</el-button>
                  </template>
                </el-popconfirm>
              </template>
            </el-table-column>
          </el-table>
          <el-pagination
            v-if="sessionTotal > userLimit"
            class="pagination"
            layout="prev, pager, next, total"
            :page-size="userLimit"
            :total="sessionTotal"
            :current-page="Math.floor(sessionOffset / userLimit) + 1"
            @current-change="changeSessionPage"
          />
        </el-tab-pane>

        <el-tab-pane label="审计日志" name="audit">
          <div class="toolbar filter-toolbar">
            <div class="filters">
              <el-input
                v-model="auditAction"
                clearable
                placeholder="按动作筛选，如 identity."
                @keyup.enter="auditOffset = 0; loadAudit()"
              />
              <el-button @click="auditOffset = 0; loadAudit()">筛选</el-button>
            </div>
            <span class="section-note">记录按时间倒序，只读展示</span>
          </div>
          <el-table :data="auditLogs" empty-text="暂无审计日志">
            <el-table-column label="动作" min-width="220">
              <template #default="scope"><code>{{ scope.row.action }}</code></template>
            </el-table-column>
            <el-table-column label="操作者" width="180">
              <template #default="scope">
                {{ scope.row.actor_display_name || '系统/Runner' }}
                <div class="muted mono">{{ scope.row.actor_username || scope.row.actor_id }}</div>
              </template>
            </el-table-column>
            <el-table-column label="资源" min-width="260">
              <template #default="scope">
                <span>{{ scope.row.resource_type }}</span>
                <div class="muted mono">{{ scope.row.resource_id }}</div>
              </template>
            </el-table-column>
            <el-table-column label="详情" width="100">
              <template #default="scope">
                <el-popover placement="left" :width="420" trigger="click">
                  <pre class="audit-details">{{ JSON.stringify(scope.row.details, null, 2) }}</pre>
                  <template #reference><el-button link>查看</el-button></template>
                </el-popover>
              </template>
            </el-table-column>
            <el-table-column label="时间" width="180">
              <template #default="scope">{{ formatDate(scope.row.created_at) }}</template>
            </el-table-column>
          </el-table>
          <el-pagination
            v-if="auditTotal > userLimit"
            class="pagination"
            layout="prev, pager, next, total"
            :page-size="userLimit"
            :total="auditTotal"
            :current-page="Math.floor(auditOffset / userLimit) + 1"
            @current-change="changeAuditPage"
          />
        </el-tab-pane>
      </el-tabs>
    </section>

    <el-dialog v-model="userCreateVisible" title="创建平台用户" width="520px" destroy-on-close>
      <el-form label-position="top">
        <el-form-item label="用户名" required>
          <el-input v-model="userForm.username" placeholder="英文、数字、点、横线或下划线" />
        </el-form-item>
        <el-form-item label="显示名" required><el-input v-model="userForm.display_name" /></el-form-item>
        <el-form-item label="初始密码" required>
          <el-input v-model="userForm.password" type="password" show-password minlength="12" />
        </el-form-item>
        <el-form-item label="系统角色">
          <el-select v-model="userForm.system_role" style="width: 100%">
            <el-option label="普通用户" value="USER" />
            <el-option label="系统管理员" value="SYSTEM_ADMIN" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="userCreateVisible = false">取消</el-button>
        <el-button type="primary" :loading="userCreateBusy" @click="createUser">创建</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="passwordVisible" title="重置用户密码" width="480px" destroy-on-close>
      <el-alert
        :title="`重置 ${passwordUser?.display_name || ''} 的密码后，其全部登录会话会立即失效。`"
        type="warning"
        :closable="false"
        show-icon
      />
      <el-form label-position="top" class="dialog-form">
        <el-form-item label="新密码" required>
          <el-input v-model="replacementPassword" type="password" show-password minlength="12" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="passwordVisible = false">取消</el-button>
        <el-button type="primary" :loading="passwordBusy" @click="resetPassword">确认重置</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="runnerPoolVisible" title="创建 Runner Pool" width="560px" destroy-on-close>
      <el-form label-position="top">
        <div class="dialog-grid">
          <el-form-item label="资源池标识" required>
            <el-input v-model="runnerPoolForm.key" placeholder="web-chromium" />
          </el-form-item>
          <el-form-item label="资源池名称" required>
            <el-input v-model="runnerPoolForm.name" />
          </el-form-item>
        </div>
        <el-form-item label="支持的目标类型" required>
          <el-select v-model="runnerPoolForm.target_types" multiple style="width: 100%">
            <el-option label="Web" value="WEB" />
            <el-option label="API" value="API" />
            <el-option label="App" value="APP" />
          </el-select>
        </el-form-item>
        <el-form-item label="池级最大并发">
          <el-input-number v-model="runnerPoolForm.max_concurrency" :min="1" :max="500" />
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="runnerPoolForm.description" type="textarea" :rows="3" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="runnerPoolVisible = false">取消</el-button>
        <el-button type="primary" :loading="runnerPoolBusy" @click="createRunnerPool">创建</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="projectCreateVisible" title="创建项目空间" width="560px" destroy-on-close>
      <el-form label-position="top">
        <el-form-item label="项目标识" required>
          <el-input v-model="projectForm.key" placeholder="例如 payments-web" />
        </el-form-item>
        <el-form-item label="项目名称" required><el-input v-model="projectForm.name" /></el-form-item>
        <el-form-item label="说明">
          <el-input v-model="projectForm.description" type="textarea" :rows="3" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="projectCreateVisible = false">取消</el-button>
        <el-button type="primary" :loading="projectCreateBusy" @click="createProject">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.eyebrow-dark {
  color: #168579;
  font-size: 11px;
  font-weight: 750;
  letter-spacing: 0.16em;
  text-transform: uppercase;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
  margin-bottom: 20px;
}

.capacity-copy {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 7px;
  color: #607287;
  font-size: 11px;
}

.runner-worker-heading {
  margin-top: 18px;
  border-top: 1px solid #edf0f2;
}

.dialog-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
}

.metric-card {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 18px 20px;
  border: 1px solid #e5eaee;
  border-radius: 11px;
  background: #fff;
}

.metric-card > .el-icon {
  width: 42px;
  height: 42px;
  border-radius: 10px;
  color: #167d73;
  background: #e9f6f3;
  font-size: 20px;
}

.metric-card strong,
.metric-card span {
  display: block;
}

.metric-card strong {
  color: #17324d;
  font-size: 23px;
}

.metric-card span {
  color: #718096;
  font-size: 12px;
}

.admin-surface {
  min-height: 460px;
}

.admin-tabs :deep(.el-tabs__header) {
  margin: 0;
  padding: 0 20px;
}

.filter-toolbar,
.filters {
  display: flex;
  align-items: center;
  gap: 10px;
}

.filters .el-input {
  width: 250px;
}

.filters .el-select {
  width: 155px;
}

.section-note {
  margin-left: 10px;
  color: #8a98a6;
  font-size: 12px;
  font-weight: 400;
}

.pagination {
  justify-content: flex-end;
  padding: 18px 20px;
}

.dialog-form {
  margin-top: 20px;
}

.audit-details {
  overflow: auto;
  max-height: 420px;
  margin: 0;
  color: #31465a;
  font: 12px/1.6 "SFMono-Regular", Consolas, monospace;
  white-space: pre-wrap;
  word-break: break-all;
}

code {
  color: #4f6478;
  font-size: 11px;
}
</style>
