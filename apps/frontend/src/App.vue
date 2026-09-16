<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  Bell,
  Collection,
  Delete,
  Expand,
  Fold,
  Lock,
  MagicStick,
  Operation,
  Plus,
  Setting,
  SwitchButton,
  UserFilled,
} from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'

import { api, ApiError } from '@/api/client'
import type { RunEmailDelivery, RunEmailStatus } from '@/api/types'
import { auth } from '@/auth'

const route = useRoute()
const router = useRouter()
const isLogin = computed(() => route.name === 'login')
const isAiAssistant = computed(() => route.name === 'ai-assistant')
const isProjectWorkspace = computed(() => route.path.startsWith('/projects/'))
const sidebarCollapsed = ref(window.localStorage.getItem('top.sidebar.collapsed') === '1')
const emailDialogVisible = ref(false)
const activeSidebarItem = computed(() => {
  if (emailDialogVisible.value) return 'email-notifications'
  if (route.path.startsWith('/projects')) return '/projects'
  if (route.path.startsWith('/changes')) return '/changes'
  if (route.path.startsWith('/ai')) return '/ai'
  if (route.path.startsWith('/admin')) return '/admin'
  return route.path
})
const emailLoading = ref(false)
const emailSaving = ref(false)
const emailTesting = ref(false)
const smtpConfigured = ref(false)
const lastEmailDelivery = ref<RunEmailDelivery | null>(null)
const emailForm = reactive<{
  emails: string[]
  selected: boolean[]
  enabled: boolean
  statuses: RunEmailStatus[]
}>({
  emails: [''],
  selected: [true],
  enabled: false,
  statuses: ['PASSED', 'FAILED', 'CANCELED', 'TIMED_OUT', 'INFRA_ERROR'],
})
const emailStatusOptions: { value: RunEmailStatus; label: string }[] = [
  { value: 'PASSED', label: '通过' },
  { value: 'FAILED', label: '失败' },
  { value: 'INFRA_ERROR', label: '基础设施异常' },
  { value: 'TIMED_OUT', label: '超时' },
  { value: 'CANCELED', label: '已取消' },
]
const recipientEmails = computed(() =>
  emailForm.emails.map((email) => email.trim()).filter((email) => email.length > 0),
)
const selectedRecipientEmails = computed(() =>
  emailForm.emails
    .map((email, index) => (emailForm.selected[index] ? email.trim() : ''))
    .filter((email) => email.length > 0),
)
const emailReady = computed(() => {
  const recipients = recipientEmails.value
  const uniqueRecipients = new Set(recipients.map((email) => email.toLocaleLowerCase()))
  return (
    recipients.length > 0 &&
    recipients.length <= 5 &&
    recipients.length === uniqueRecipients.size &&
    recipients.every((email) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) &&
    selectedRecipientEmails.value.length > 0 &&
    emailForm.statuses.length > 0
  )
})

function toggleSidebar(): void {
  sidebarCollapsed.value = !sidebarCollapsed.value
  window.localStorage.setItem('top.sidebar.collapsed', sidebarCollapsed.value ? '1' : '0')
}

function handleSidebarSelect(index: string): void {
  if (index === 'email-notifications') {
    void openEmailSettings()
    return
  }
  if (route.path !== index) void router.push(index)
}

async function logout(): Promise<void> {
  await auth.logout()
  await router.replace({ name: 'login' })
}

function emailError(error: unknown): string {
  return error instanceof ApiError ? error.message : '邮件通知操作失败，请稍后重试。'
}

function applyEmailPreference(preference: Awaited<ReturnType<typeof api.runEmailPreference>>): void {
  const emails = preference.emails.length
    ? [...preference.emails]
    : preference.email
      ? [preference.email]
      : ['']
  const selectedKeys = new Set(preference.selected_emails.map((email) => email.toLocaleLowerCase()))
  emailForm.emails = emails
  emailForm.selected = emails.map((email) =>
    email ? selectedKeys.has(email.toLocaleLowerCase()) : true,
  )
  emailForm.enabled = preference.enabled
  emailForm.statuses = [...preference.statuses]
  smtpConfigured.value = preference.smtp_configured
  lastEmailDelivery.value = preference.last_delivery
}

function addRecipientEmail(): void {
  if (emailForm.emails.length < 5) {
    emailForm.emails.push('')
    emailForm.selected.push(true)
  }
}

function removeRecipientEmail(index: number): void {
  if (emailForm.emails.length === 1) {
    emailForm.emails[0] = ''
    emailForm.selected[0] = true
    return
  }
  emailForm.emails.splice(index, 1)
  emailForm.selected.splice(index, 1)
}

async function openEmailSettings(): Promise<void> {
  emailDialogVisible.value = true
  emailLoading.value = true
  try {
    applyEmailPreference(await api.runEmailPreference())
  } catch (error) {
    ElMessage.error(emailError(error))
  } finally {
    emailLoading.value = false
  }
}

async function saveEmailSettings(showSuccess = true): Promise<boolean> {
  if (!emailReady.value && emailForm.enabled) return false
  emailSaving.value = true
  try {
    const preference = await api.updateRunEmailPreference({
      emails: recipientEmails.value,
      selected_emails: selectedRecipientEmails.value,
      enabled: emailForm.enabled,
      statuses: emailForm.statuses,
    })
    applyEmailPreference(preference)
    if (showSuccess) ElMessage.success('邮件通知设置已保存')
    return true
  } catch (error) {
    ElMessage.error(emailError(error))
    return false
  } finally {
    emailSaving.value = false
  }
}

async function testEmailSettings(): Promise<void> {
  if (!smtpConfigured.value || !emailReady.value) return
  emailTesting.value = true
  try {
    if (!(await saveEmailSettings(false))) return
    lastEmailDelivery.value = await api.testRunEmailNotification()
    ElMessage.success(`测试邮件已进入 ${selectedRecipientEmails.value.length} 个收件邮箱的发送队列`)
  } catch (error) {
    ElMessage.error(emailError(error))
  } finally {
    emailTesting.value = false
  }
}

function emailDeliveryLabel(delivery: RunEmailDelivery): string {
  const labels: Record<RunEmailDelivery['status'], string> = {
    PENDING: '等待发送',
    DELIVERED: '发送成功',
    FAILED: '发送失败',
    CANCELED: '已取消',
  }
  return labels[delivery.status]
}
</script>

<template>
  <el-config-provider :locale="zhCn">
    <router-view v-if="isLogin" />
    <el-container
      v-else
      class="app-shell"
      :class="{ 'sidebar-collapsed': sidebarCollapsed }"
    >
      <el-aside :width="sidebarCollapsed ? '76px' : '240px'" class="sidebar">
        <div class="brand">
          <span class="brand-mark">T</span>
          <span v-show="!sidebarCollapsed" class="brand-copy">
            <strong>TestOps</strong>
            <small>Platform</small>
          </span>
          <button
            class="sidebar-toggle"
            type="button"
            :aria-label="sidebarCollapsed ? '展开导航' : '收起导航'"
            :title="sidebarCollapsed ? '展开导航' : '收起导航'"
            @click="toggleSidebar"
          >
            <el-icon><Expand v-if="sidebarCollapsed" /><Fold v-else /></el-icon>
          </button>
        </div>
        <el-menu
          :default-active="activeSidebarItem"
          :collapse="sidebarCollapsed"
          :collapse-transition="false"
          class="navigation"
          @select="handleSidebarSelect"
        >
          <el-menu-item index="/projects">
            <el-icon><Collection /></el-icon>
            <template #title>项目空间</template>
          </el-menu-item>
          <el-menu-item index="/changes">
            <el-icon><Operation /></el-icon>
            <template #title>审批与变更</template>
          </el-menu-item>
          <el-menu-item index="/ai" class="ai-navigation-item">
            <el-icon><MagicStick /></el-icon>
            <template #title>
              <span class="ai-navigation-label">
                AI 助手
                <small>Copilot</small>
              </span>
            </template>
          </el-menu-item>
          <el-menu-item index="email-notifications">
            <el-icon><Bell /></el-icon>
            <template #title>邮件通知设置</template>
          </el-menu-item>
          <el-menu-item v-if="auth.state.user?.system_role === 'SYSTEM_ADMIN'" index="/admin">
            <el-icon><Setting /></el-icon>
            <template #title>系统管理</template>
          </el-menu-item>
        </el-menu>
        <div class="sidebar-foot">
          <div class="security-note" :class="{ compact: sidebarCollapsed }">
            <el-icon><Lock /></el-icon>
            <span v-show="!sidebarCollapsed">Released 基线受保护</span>
          </div>
        </div>
      </el-aside>
      <el-container :class="{ 'content-shell--fixed': isAiAssistant }">
        <el-header class="topbar">
          <div class="control-status">
            <span class="environment-dot" />
            <span>
              <strong>控制面在线</strong>
              <small>TestOps Control Plane</small>
            </span>
          </div>
          <el-dropdown trigger="click">
            <button class="user-menu" type="button">
              <el-icon><UserFilled /></el-icon>
              <span>{{ auth.state.user?.display_name }}</span>
              <small>{{ auth.state.user?.system_role === 'SYSTEM_ADMIN' ? '系统管理员' : '成员' }}</small>
            </button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item @click="logout">
                  <el-icon><SwitchButton /></el-icon>
                  退出登录
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </el-header>
        <el-main
          class="page-area"
          :class="{ 'page-area--document-scroll': isProjectWorkspace }"
        >
          <router-view />
        </el-main>
      </el-container>
    </el-container>

    <el-dialog
      v-model="emailDialogVisible"
      title="测试结果邮件通知"
      width="560px"
      class="email-notification-dialog"
      destroy-on-close
    >
      <div v-loading="emailLoading" class="email-notification-content">
        <div class="email-notification-intro">
          <span class="email-notification-icon"><el-icon><Bell /></el-icon></span>
          <div>
            <strong>Run 完成后自动通知我</strong>
            <p>最多设置 5 个收件邮箱，由每位用户独立配置。</p>
          </div>
          <el-tag :type="smtpConfigured ? 'success' : 'warning'" effect="plain">
            {{ smtpConfigured ? '邮件服务可用' : '邮件服务未配置' }}
          </el-tag>
        </div>

        <el-alert
          v-if="!smtpConfigured"
          title="平台管理员需要先配置 SMTP 发件服务；您可以先保存收件邮箱。"
          type="warning"
          :closable="false"
          show-icon
        />

        <el-form label-position="top" class="email-notification-form">
          <el-form-item label="收件邮箱（最多 5 个，可选择发送）" required>
            <div class="email-recipient-list">
              <div class="email-recipient-summary">
                已配置 {{ recipientEmails.length }} 个，已选择 {{ selectedRecipientEmails.length }} 个
              </div>
              <div
                v-for="(_, index) in emailForm.emails"
                :key="index"
                class="email-recipient-row"
              >
                <span class="email-recipient-number">{{ index + 1 }}</span>
                <el-input
                  v-model="emailForm.emails[index]"
                  type="email"
                  maxlength="320"
                  :placeholder="index === 0 ? 'name@example.com' : '添加另一个收件邮箱'"
                  clearable
                />
                <el-checkbox
                  v-model="emailForm.selected[index]"
                  class="email-recipient-selection"
                  :disabled="!emailForm.emails[index]?.trim()"
                >
                  发送
                </el-checkbox>
                <el-button
                  class="email-recipient-remove"
                  :aria-label="`删除第 ${index + 1} 个收件邮箱`"
                  circle
                  @click="removeRecipientEmail(index)"
                >
                  <el-icon><Delete /></el-icon>
                </el-button>
              </div>
              <el-button
                v-if="emailForm.emails.length < 5"
                class="email-recipient-add"
                plain
                @click="addRecipientEmail"
              >
                <el-icon><Plus /></el-icon>
                添加收件邮箱（{{ emailForm.emails.length }}/5）
              </el-button>
            </div>
          </el-form-item>
          <el-form-item label="需要通知的运行结果">
            <el-checkbox-group v-model="emailForm.statuses" class="email-status-options">
              <el-checkbox
                v-for="option in emailStatusOptions"
                :key="option.value"
                :value="option.value"
              >
                {{ option.label }}
              </el-checkbox>
            </el-checkbox-group>
          </el-form-item>
          <div class="email-notification-switch">
            <div>
              <strong>接收我发起的 Run 结果</strong>
              <span>关闭后不发送；开启后仅通知当前账号发起的 Run</span>
            </div>
            <el-switch
              v-model="emailForm.enabled"
              :disabled="!smtpConfigured && !emailForm.enabled"
            />
          </div>
        </el-form>

        <div v-if="lastEmailDelivery" class="email-delivery-state">
          <span>最近一次：{{ emailDeliveryLabel(lastEmailDelivery) }}</span>
          <span>{{ lastEmailDelivery.destination_display }}</span>
          <span v-if="lastEmailDelivery.last_error">{{ lastEmailDelivery.last_error }}</span>
        </div>
      </div>
      <template #footer>
        <el-button @click="emailDialogVisible = false">取消</el-button>
        <el-button
          :loading="emailTesting"
          :disabled="!smtpConfigured || !emailReady"
          @click="testEmailSettings"
        >
          发送测试邮件
        </el-button>
        <el-button
          type="primary"
          :loading="emailSaving"
          :disabled="emailForm.enabled && !emailReady"
          @click="saveEmailSettings()"
        >
          保存设置
        </el-button>
      </template>
    </el-dialog>
  </el-config-provider>
</template>
