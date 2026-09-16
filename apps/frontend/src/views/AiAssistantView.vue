<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ChatDotRound,
  Connection,
  Delete,
  MagicStick,
  Plus,
  Promotion,
  Refresh,
  UserFilled,
  VideoPlay,
} from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { ApiError, api } from '@/api/client'
import type {
  AiConversationMessage,
  AiConversationSummary,
  AiModelOption,
  AiProjectChatSection,
  Project,
  Run,
} from '@/api/types'
import { formatDate, runTagType } from '@/presentation'

interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  sections?: AiProjectChatSection[]
  evidenceRefs?: string[]
  suggestions?: string[]
  metadata?: {
    model: string
    latency_ms: number
    evidence_truncated: boolean
  }
  conversation?: boolean
  contextKey?: string
  scopeLabel?: string
  status?: 'PENDING' | 'COMPLETED' | 'FAILED' | null
}

const route = useRoute()
const router = useRouter()
const projects = ref<Project[]>([])
const runs = ref<Run[]>([])
const modelOptions = ref<AiModelOption[]>([])
const projectId = ref('')
const runId = ref('')
const modelProviderKey = ref('')
const question = ref('')
const loading = ref(true)
const runsLoading = ref(false)
const sending = ref(false)
const transcript = ref<HTMLElement>()
const messages = ref<ChatMessage[]>([])
const conversations = ref<AiConversationSummary[]>([])
const conversationId = ref('')
const conversationLoading = ref(false)
let pendingRefreshTimer: ReturnType<typeof setTimeout> | undefined

const selectedProject = computed(() => projects.value.find((item) => item.id === projectId.value))
const selectedRun = computed(() => runs.value.find((item) => item.id === runId.value))
const selectedModel = computed(() =>
  modelOptions.value.find((item) => item.key === modelProviderKey.value),
)
const currentContextKey = computed(() => `${projectId.value}:${runId.value || 'GLOBAL'}`)
const currentScopeLabel = computed(() =>
  selectedRun.value ? `Run ${selectedRun.value.id.slice(0, 8)}` : '最近 10 次运行',
)
const scopeDetail = computed(() =>
  selectedRun.value
    ? `${selectedRun.value.case_count} 条用例 · ${selectedRun.value.status}`
    : `最近 ${Math.min(runs.value.length, 10)} 次测试运行`,
)
const hasPendingAnswer = computed(() =>
  messages.value.some((item) => item.role === 'user' && item.status === 'PENDING'),
)
const starterQuestions = computed(() => {
  if (selectedRun.value) {
    return [
      '总结所选 Run 的执行情况',
      '分析所选 Run 的主要失败原因',
      '判断更像产品、脚本还是环境问题',
      '给出所选 Run 的下一步排查清单',
      'Runner Pool 为什么会让这个 Run 等待？',
    ]
  }
  return [
    '总结最近 10 次测试运行的状态分布',
    'Runner Pool 是什么，在哪里配置？',
    '用例基线和自动化包版本有什么区别？',
    '完整重跑和仅异常重跑有什么区别？',
    '平台的角色和权限是怎样划分的？',
    '质量分析、SLO 和 Flaky Case 分别是什么？',
  ]
})

function messageId(): string {
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function ensureConversation(): void {
  if (messages.value.length) return
  const scope = selectedRun.value
    ? `我已加载 Run ${selectedRun.value.id.slice(0, 8)} 的只读证据。`
    : '当前是项目全局范围，仅按最近 10 条 Run 记录分析；每条 Run 代表一次平台测试运行，不是单条用例执行。'
  messages.value = [
    {
      id: messageId(),
      role: 'assistant',
      content: `你好，我是 TOP AI 助手。${scope}我只提供统计、原因研判和排查建议，不会修改用例、测试结果或触发复跑。`,
      contextKey: currentContextKey.value,
      scopeLabel: currentScopeLabel.value,
    },
  ]
}

function syncRoute() {
  return router.replace({
    name: 'ai-assistant',
    query: {
      ...(projectId.value ? { projectId: projectId.value } : {}),
      ...(runId.value ? { runId: runId.value } : {}),
      ...(modelProviderKey.value ? { model: modelProviderKey.value } : {}),
      ...(conversationId.value ? { conversationId: conversationId.value } : {}),
    },
  })
}

async function scrollToBottom(): Promise<void> {
  await nextTick()
  if (transcript.value) transcript.value.scrollTop = transcript.value.scrollHeight
}

function restoredMessage(message: AiConversationMessage): ChatMessage {
  return {
    id: message.id,
    role: message.role,
    content: message.content,
    sections: message.sections,
    evidenceRefs: message.evidence_refs,
    suggestions: message.suggested_questions,
    metadata: message.model
      ? {
          model: message.model,
          latency_ms: message.latency_ms ?? 0,
          evidence_truncated: message.evidence_truncated,
        }
      : undefined,
    conversation: true,
    contextKey: currentContextKey.value,
    scopeLabel: currentScopeLabel.value,
    status: message.status,
  }
}

function stopPendingRefresh(): void {
  if (pendingRefreshTimer !== undefined) {
    clearTimeout(pendingRefreshTimer)
    pendingRefreshTimer = undefined
  }
}

function schedulePendingRefresh(): void {
  stopPendingRefresh()
  if (!conversationId.value || !hasPendingAnswer.value || sending.value) return
  pendingRefreshTimer = setTimeout(() => void refreshPendingConversation(), 2_000)
}

async function refreshPendingConversation(): Promise<void> {
  pendingRefreshTimer = undefined
  const watchedConversationId = conversationId.value
  const watchedProjectId = projectId.value
  const watchedRunId = runId.value
  if (!watchedConversationId || !watchedProjectId) return
  try {
    const detail = await api.aiConversation(
      watchedProjectId,
      watchedConversationId,
      watchedRunId,
    )
    if (
      conversationId.value !== watchedConversationId ||
      projectId.value !== watchedProjectId ||
      runId.value !== watchedRunId
    ) return
    messages.value = detail.messages.map(restoredMessage)
    await refreshConversationList()
    await scrollToBottom()
  } catch {
    // A transient poll error must not erase the persisted question.
  } finally {
    schedulePendingRefresh()
  }
}

async function openConversation(id: string): Promise<void> {
  if (!projectId.value || !id) return
  stopPendingRefresh()
  conversationLoading.value = true
  try {
    const detail = await api.aiConversation(projectId.value, id, runId.value)
    conversationId.value = detail.conversation.id
    messages.value = detail.messages.map(restoredMessage)
    ensureConversation()
    await syncRoute()
    await scrollToBottom()
    schedulePendingRefresh()
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : '对话记录加载失败')
  } finally {
    conversationLoading.value = false
  }
}

async function refreshConversationList(): Promise<void> {
  if (!projectId.value) return
  const result = await api.aiConversations(projectId.value, runId.value)
  conversations.value = result.items
}

async function loadConversations(preferredConversationId = ''): Promise<void> {
  conversationLoading.value = true
  conversations.value = []
  conversationId.value = ''
  messages.value = []
  try {
    await refreshConversationList()
    const preferred = conversations.value.find((item) => item.id === preferredConversationId)
    const target = preferred ?? conversations.value[0]
    if (target) {
      await openConversation(target.id)
    } else {
      ensureConversation()
      syncRoute()
    }
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : '对话记录加载失败')
    ensureConversation()
  } finally {
    conversationLoading.value = false
  }
}

async function loadRuns(preferredRunId = '', preferredConversationId = ''): Promise<void> {
  runs.value = []
  runId.value = ''
  if (!projectId.value) {
    ensureConversation()
    return
  }
  runsLoading.value = true
  try {
    const result = await api.runs(projectId.value, { limit: 50 })
    runs.value = result.items
    const preferred = runs.value.find((item) => item.id === preferredRunId)
    runId.value = preferred?.id ?? ''
    await loadConversations(preferredConversationId)
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : '运行记录加载失败')
    ensureConversation()
  } finally {
    runsLoading.value = false
  }
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const [availableProjects, models] = await Promise.all([api.projects(), api.aiModels()])
    projects.value = availableProjects
    modelOptions.value = models.items
    const requestedModel = typeof route.query.model === 'string' ? route.query.model : ''
    modelProviderKey.value =
      modelOptions.value.find((item) => item.key === requestedModel)?.key ??
      modelOptions.value.find((item) => item.is_default)?.key ??
      modelOptions.value[0]?.key ??
      ''
    const requestedProject = typeof route.query.projectId === 'string' ? route.query.projectId : ''
    projectId.value =
      projects.value.find((item) => item.id === requestedProject)?.id ??
      projects.value[0]?.id ??
      ''
    const requestedRun = typeof route.query.runId === 'string' ? route.query.runId : ''
    const requestedConversation =
      typeof route.query.conversationId === 'string' ? route.query.conversationId : ''
    await loadRuns(requestedRun, requestedConversation)
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : 'AI 助手初始化失败')
    ensureConversation()
  } finally {
    loading.value = false
  }
}

async function changeProject(): Promise<void> {
  await loadRuns()
}

async function changeRun(): Promise<void> {
  await loadConversations()
}

function changeModel(): void {
  syncRoute()
}

async function sendQuestion(value?: string): Promise<void> {
  const content = (value ?? question.value).trim()
  if (!content || !projectId.value || sending.value || hasPendingAnswer.value) return
  const requestProjectId = projectId.value
  const requestRunId = runId.value
  const requestContextKey = currentContextKey.value
  const requestScopeLabel = currentScopeLabel.value
  const requestConversationId = conversationId.value
  const optimisticMessageId = messageId()
  const submittedMessage: ChatMessage = {
    id: optimisticMessageId,
    role: 'user',
    content,
    conversation: true,
    contextKey: requestContextKey,
    scopeLabel: requestScopeLabel,
    status: 'PENDING',
  }
  messages.value.push(submittedMessage)
  question.value = ''
  sending.value = true
  await scrollToBottom()
  try {
    const prepared = await api.prepareProjectAiQuestion(requestProjectId, {
      message: content,
      run_id: requestRunId || null,
      conversation_id: requestConversationId || null,
      model_provider_key: modelProviderKey.value || null,
    })
    conversationId.value = prepared.conversation_id
    submittedMessage.id = prepared.message_id
    await syncRoute()
    void refreshConversationList().catch(() => undefined)
  } catch (error) {
    submittedMessage.status = 'FAILED'
    messages.value.push({
      id: messageId(),
      role: 'assistant',
      content: error instanceof ApiError ? error.message : 'AI 暂时无法回答，请稍后重试。',
      contextKey: requestContextKey,
      scopeLabel: requestScopeLabel,
    })
  } finally {
    sending.value = false
    await scrollToBottom()
    schedulePendingRefresh()
  }
}

function startNewConversation(): void {
  stopPendingRefresh()
  conversationId.value = ''
  messages.value = []
  question.value = ''
  ensureConversation()
  syncRoute()
  void scrollToBottom()
}

async function deleteConversation(item: AiConversationSummary): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确定删除对话“${item.title}”吗？删除后无法恢复。`,
      '删除对话',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
    await api.deleteAiConversation(projectId.value, item.id)
    ElMessage.success('对话已删除')
    if (conversationId.value === item.id) {
      await loadConversations()
    } else {
      await refreshConversationList()
    }
  } catch (error) {
    if (error === 'cancel' || error === 'close') return
    ElMessage.error(error instanceof ApiError ? error.message : '删除对话失败')
  }
}

async function openSelectedRun(): Promise<void> {
  if (!runId.value || !projectId.value) return
  await router.push({
    name: 'run-detail',
    params: { projectId: projectId.value, runId: runId.value },
  })
}

onMounted(load)
onBeforeUnmount(stopPendingRefresh)
</script>

<template>
  <div v-loading="loading" class="page-container ai-page">
    <header class="page-heading ai-page-heading">
      <div class="ai-title-block">
        <span class="ai-title-mark"><el-icon><MagicStick /></el-icon></span>
        <div>
          <div class="ai-page-kicker">TestOps Copilot</div>
          <h1>AI 助手</h1>
          <p>了解平台功能、查看测试统计、分析失败原因，并基于可信证据继续追问。</p>
        </div>
      </div>
      <div class="ai-capability-tags">
        <el-tag type="info" effect="plain" round>
          <el-icon><ChatDotRound /></el-icon> 平台知识
        </el-tag>
        <el-tag type="success" effect="plain" round>
          <el-icon><Connection /></el-icon> 只读模式
        </el-tag>
      </div>
    </header>

    <section class="surface ai-context-bar">
      <div class="context-field">
        <label>项目</label>
        <el-select
          v-model="projectId"
          placeholder="选择项目"
          filterable
          :disabled="sending"
          @change="changeProject"
        >
          <el-option
            v-for="project in projects"
            :key="project.id"
            :label="`${project.name} · ${project.key}`"
            :value="project.id"
          />
        </el-select>
      </div>
      <div class="context-field run-field">
        <label>分析范围</label>
        <el-select
          v-model="runId"
          :loading="runsLoading"
          placeholder="选择分析范围"
          filterable
          :disabled="sending"
          @change="changeRun"
        >
          <el-option label="项目全局 · 最近 10 次测试运行" value="">
            <div class="run-option global-option">
              <el-tag type="primary" size="small">全局</el-tag>
              <span>最近 10 次运行</span>
              <small>1 条 Run = 1 次测试运行</small>
            </div>
          </el-option>
          <el-option
            v-for="item in runs"
            :key="item.id"
            :value="item.id"
            :label="`${item.status} · ${item.id.slice(0, 8)} · ${formatDate(item.created_at)}`"
          >
            <div class="run-option">
              <el-tag :type="runTagType(item.status)" size="small">{{ item.status }}</el-tag>
              <span class="mono">{{ item.id.slice(0, 8) }}</span>
              <small>{{ formatDate(item.created_at) }}</small>
            </div>
          </el-option>
        </el-select>
      </div>
      <div class="context-field model-field">
        <label>AI 模型</label>
        <el-select
          v-model="modelProviderKey"
          placeholder="选择模型"
          :disabled="sending || !modelOptions.length"
          @change="changeModel"
        >
          <el-option
            v-for="item in modelOptions"
            :key="item.key"
            :value="item.key"
            :label="`${item.name} · ${item.model}`"
          >
            <div class="model-option">
              <span>{{ item.name }}</span>
              <small>{{ item.model }}</small>
              <el-tag v-if="item.is_default" size="small" type="success">默认</el-tag>
            </div>
          </el-option>
        </el-select>
      </div>
      <div v-if="selectedRun" class="selected-run-summary">
        <span>{{ selectedRun.case_count }} 条用例</span>
        <el-tag :type="runTagType(selectedRun.status)" effect="light">
          {{ selectedRun.status }}
        </el-tag>
        <el-button text type="primary" :icon="VideoPlay" @click="openSelectedRun">
          查看 Run
        </el-button>
      </div>
      <div v-else class="selected-run-summary global-scope-summary">
        <el-tag type="primary" effect="light">项目全局</el-tag>
        <span>最近 10 次测试运行记录</span>
      </div>
      <el-button :icon="Refresh" @click="load">刷新上下文</el-button>
    </section>

    <section class="surface chat-shell">
      <div v-loading="conversationLoading" class="chat-main">
        <div ref="transcript" class="chat-transcript">
          <div
            v-for="message in messages"
            :key="message.id"
            class="chat-row"
            :class="message.role"
          >
            <div class="chat-avatar">
              <el-icon><MagicStick v-if="message.role === 'assistant'" /><UserFilled v-else /></el-icon>
            </div>
            <div class="chat-message-wrap">
              <div class="chat-author">
                <span>{{ message.role === 'assistant' ? 'TOP AI' : '你' }}</span>
                <small v-if="message.scopeLabel">{{ message.scopeLabel }}</small>
                <small v-if="message.role === 'user' && message.status" class="message-state">
                  {{ message.status === 'PENDING' ? '已保存 · 等待回答' : message.status === 'FAILED' ? '已保存 · 回答失败' : '已完成' }}
                </small>
              </div>
              <div class="chat-bubble" :class="{ 'structured-answer': message.sections?.length }">
                <span v-if="message.sections?.length" class="chat-answer-label">核心结论</span>
                <p class="chat-answer-summary">{{ message.content }}</p>
                <div v-if="message.sections?.length" class="chat-answer-sections">
                  <section
                    v-for="(section, sectionIndex) in message.sections"
                    :key="`${message.id}-${sectionIndex}`"
                    class="chat-answer-section"
                  >
                    <h4>
                      <span>{{ sectionIndex + 1 }}</span>
                      {{ section.title }}
                    </h4>
                    <ul>
                      <li v-for="(item, itemIndex) in section.items" :key="itemIndex">
                        {{ item }}
                      </li>
                    </ul>
                  </section>
                </div>
              </div>
              <div v-if="message.evidenceRefs?.length" class="chat-evidence">
                <span>证据</span>
                <code v-for="evidenceRef in message.evidenceRefs" :key="evidenceRef">
                  {{ evidenceRef }}
                </code>
              </div>
              <div v-if="message.metadata" class="chat-meta">
                <span>{{ message.metadata.model }}</span>
                <span>{{ message.metadata.latency_ms }} ms</span>
                <span v-if="message.metadata.evidence_truncated">证据已按安全上限截断</span>
                <strong>不会改变测试结果</strong>
              </div>
              <div v-if="message.suggestions?.length" class="response-suggestions">
                <el-button
                  v-for="suggestion in message.suggestions"
                  :key="suggestion"
                  round
                  size="small"
                  :disabled="sending || hasPendingAnswer"
                  @click="sendQuestion(suggestion)"
                >{{ suggestion }}</el-button>
              </div>
            </div>
          </div>

          <div v-if="sending || hasPendingAnswer" class="chat-row assistant">
            <div class="chat-avatar"><el-icon><MagicStick /></el-icon></div>
            <div class="chat-message-wrap">
              <div class="chat-author">TOP AI</div>
              <div class="chat-bubble typing-bubble">
                <i /><i /><i /><span>{{ sending ? '正在分析平台证据…' : '问题已保存，正在等待 AI 回答…' }}</span>
              </div>
            </div>
          </div>
        </div>

        <div class="chat-composer">
          <div class="composer-box">
            <el-input
              v-model="question"
              type="textarea"
              :rows="2"
              maxlength="2000"
              resize="none"
              placeholder="询问平台功能、测试统计或失败原因（Enter 发送，Shift+Enter 换行）"
              :disabled="sending || hasPendingAnswer || !projectId"
              @keydown.enter.exact.prevent="sendQuestion()"
            />
            <div class="composer-actions">
              <span>问题和平台证据会先经过敏感信息遮蔽</span>
              <el-button
                type="primary"
                :icon="Promotion"
                :loading="sending"
                :disabled="!question.trim() || hasPendingAnswer || !projectId"
                @click="sendQuestion()"
              >发送</el-button>
            </div>
          </div>
        </div>
      </div>

      <aside class="ai-sidebar">
        <section class="sidebar-section scope-section">
          <div class="scope-card">
            <span class="scope-card-icon"><el-icon><MagicStick /></el-icon></span>
            <div>
              <small>当前分析上下文</small>
              <strong>{{ selectedProject?.name ?? '请选择项目' }}</strong>
              <p>{{ currentScopeLabel }} · {{ scopeDetail }}</p>
              <p class="scope-model">模型：{{ selectedModel?.name ?? '未配置' }}</p>
            </div>
          </div>
        </section>
        <section class="sidebar-section quick-section">
          <div class="sidebar-heading">
            <div>
              <small>AI 工具</small>
              <strong>快捷提问</strong>
            </div>
          </div>
          <button
            v-for="starter in starterQuestions"
            :key="starter"
            class="quick-prompt"
            :disabled="sending || hasPendingAnswer || !projectId"
            @click="sendQuestion(starter)"
          >
            <el-icon><ChatDotRound /></el-icon>
            <span>{{ starter }}</span>
          </button>
        </section>

        <section class="sidebar-section history-section">
          <div class="sidebar-heading">
            <div>
              <small>当前范围</small>
              <strong>对话记录</strong>
            </div>
            <el-button text type="primary" :icon="Plus" @click="startNewConversation">
              新对话
            </el-button>
          </div>
          <div v-if="!conversations.length" class="conversation-empty">
            暂无已保存对话
          </div>
          <div
            v-for="item in conversations"
            :key="item.id"
            class="conversation-item"
            :class="{ active: item.id === conversationId }"
            role="button"
            tabindex="0"
            @click="openConversation(item.id)"
            @keydown.enter="openConversation(item.id)"
          >
            <div>
              <strong>{{ item.title }}</strong>
              <small>{{ item.message_count }} 条消息 · {{ formatDate(item.updated_at) }}</small>
            </div>
            <el-button
              text
              circle
              :icon="Delete"
              aria-label="删除对话"
              @click.stop="deleteConversation(item)"
            />
          </div>
        </section>
      </aside>
    </section>
  </div>
</template>

<style scoped>
.ai-page {
  display: flex;
  height: 100%;
  width: 100%;
  max-width: none;
  min-height: 0;
  flex-direction: column;
  overflow: hidden;
}

.ai-page-heading {
  align-items: center;
  margin-bottom: 14px;
}

.ai-title-block {
  display: flex;
  align-items: center;
  gap: 13px;
}

.ai-title-mark {
  display: grid;
  width: 42px;
  height: 42px;
  flex: 0 0 42px;
  place-items: center;
  border: 1px solid rgb(106 80 189 / 16%);
  border-radius: var(--top-radius-lg);
  color: #fff;
  background: linear-gradient(145deg, var(--top-ai-600), #8368d1);
  box-shadow: 0 10px 28px rgb(89 65 169 / 20%);
}

.ai-page-heading h1 {
  margin: 2px 0 1px;
  color: var(--top-text-strong);
  font-size: 25px;
  letter-spacing: -0.025em;
}

.ai-page-heading p {
  color: var(--top-text-muted);
  font-size: 11px;
}

.ai-page-kicker {
  color: var(--top-ai-600);
  font-size: 9px;
  font-weight: 800;
  letter-spacing: 0.16em;
  text-transform: uppercase;
}

.ai-page-heading .el-tag {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.ai-capability-tags {
  display: flex;
  align-items: center;
  gap: 8px;
}

.ai-context-bar {
  display: flex;
  align-items: flex-end;
  gap: 12px;
  padding: 10px 12px;
  border-color: var(--top-border);
  background: rgb(255 255 255 / 92%);
  box-shadow: var(--top-shadow-1);
  backdrop-filter: blur(14px);
}

.context-field {
  display: grid;
  width: 250px;
  gap: 4px;
}

.context-field.run-field {
  width: min(440px, 34vw);
}

.context-field.model-field {
  width: min(300px, 24vw);
}

.context-field label {
  color: var(--top-text-muted);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.03em;
}

.run-option {
  display: flex;
  align-items: center;
  gap: 10px;
}

.run-option small {
  margin-left: auto;
  color: #8796a5;
}

.model-option {
  display: flex;
  align-items: center;
  gap: 8px;
}

.model-option small {
  margin-left: auto;
  color: var(--top-text-muted);
}

.global-option span {
  color: #365a85;
  font-weight: 650;
}

.selected-run-summary {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-left: auto;
  color: #63778a;
  font-size: 12px;
}

.global-scope-summary span {
  color: #4e6c8a;
}

.chat-shell {
  display: grid;
  min-height: 0;
  flex: 1;
  grid-template-columns: minmax(0, 1fr) 280px;
  margin-top: 12px;
  overflow: hidden;
  border-color: var(--top-border);
  border-radius: var(--top-radius-xl);
  background:
    radial-gradient(circle at 5% 0%, rgb(106 80 189 / 5%), transparent 24rem),
    var(--top-surface);
  box-shadow: var(--top-shadow-2);
}

.chat-main {
  display: grid;
  min-width: 0;
  min-height: 0;
  grid-template-rows: minmax(0, 1fr) auto;
  overflow: hidden;
}

.chat-transcript {
  min-height: 0;
  padding: 22px clamp(20px, 3vw, 44px);
  overflow-y: auto;
  overscroll-behavior-y: contain;
  scrollbar-gutter: stable;
}

.chat-row {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  max-width: 1120px;
  margin: 0 auto 20px;
}

.chat-row.user {
  flex-direction: row-reverse;
}

.chat-avatar {
  display: grid;
  width: 34px;
  height: 34px;
  flex: 0 0 34px;
  place-items: center;
  border-radius: var(--top-radius-md);
  background: linear-gradient(145deg, var(--top-ai-600), #846bd0);
  color: #fff;
  box-shadow: 0 8px 20px rgb(89 65 169 / 18%);
}

.chat-row.user .chat-avatar {
  background: var(--top-accent-600);
  box-shadow: 0 8px 20px rgb(15 130 119 / 16%);
}

.chat-message-wrap {
  max-width: min(940px, calc(100% - 54px));
}

.chat-row.user .chat-message-wrap {
  display: grid;
  justify-items: end;
}

.chat-author {
  display: flex;
  align-items: center;
  gap: 7px;
  margin: 0 4px 6px;
  color: var(--top-text-muted);
  font-size: 11px;
  font-weight: 700;
}

.chat-author small {
  padding: 2px 6px;
  border-radius: 5px;
  background: var(--top-surface-muted);
  color: #677f96;
  font-size: 9px;
  font-weight: 650;
}

.chat-bubble {
  padding: 15px 17px;
  border: 1px solid var(--top-border);
  border-radius: 5px var(--top-radius-lg) var(--top-radius-lg);
  background: var(--top-surface);
  color: var(--top-text);
  font-size: 13px;
  line-height: 1.8;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
  box-shadow: var(--top-shadow-1);
}

.chat-row.assistant .chat-bubble {
  position: relative;
  border-left: 3px solid rgb(106 80 189 / 65%);
}

.chat-answer-summary {
  margin: 0;
}

.chat-answer-label {
  display: inline-block;
  margin-bottom: 5px;
  color: var(--top-ai-600);
  font-size: 10px;
  font-weight: 750;
  letter-spacing: 0.08em;
}

.structured-answer .chat-answer-summary {
  color: var(--top-text-strong);
  font-size: 14px;
  font-weight: 700;
  line-height: 1.7;
}

.chat-answer-sections {
  display: grid;
  gap: 10px;
  margin-top: 13px;
  padding-top: 13px;
  border-top: 1px solid #e7edf4;
}

.chat-answer-section {
  padding: 12px 13px;
  border: 1px solid var(--top-border);
  border-radius: var(--top-radius-md);
  background: linear-gradient(145deg, #fafbfe, var(--top-surface-subtle));
}

.chat-answer-section h4 {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 0 7px;
  color: #29455f;
  font-size: 12px;
  line-height: 1.4;
}

.chat-answer-section h4 span {
  display: inline-grid;
  flex: 0 0 20px;
  width: 20px;
  height: 20px;
  place-items: center;
  border-radius: var(--top-radius-sm);
  background: var(--top-ai-100);
  color: var(--top-ai-700);
  font-size: 10px;
}

.chat-answer-section ul {
  display: grid;
  gap: 6px;
  margin: 0;
  padding-left: 18px;
}

.chat-answer-section li {
  padding-left: 2px;
  color: #40576d;
  line-height: 1.65;
  white-space: normal;
}

.chat-answer-section li::marker {
  color: #6e91b4;
}

.chat-row.user .chat-bubble {
  border-color: var(--top-accent-600);
  border-radius: var(--top-radius-lg) 5px var(--top-radius-lg) var(--top-radius-lg);
  background: linear-gradient(145deg, var(--top-accent-600), var(--top-accent-700));
  color: #fff;
}

.chat-evidence,
.chat-meta,
.response-suggestions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 7px;
  margin-top: 8px;
}

.chat-evidence span,
.chat-meta {
  color: #8b99a8;
  font-size: 10px;
}

.chat-evidence code {
  padding: 3px 7px;
  border: 1px solid #dde7f1;
  border-radius: var(--top-radius-sm);
  background: #f4f7fb;
  color: #4d6b91;
  font-size: 10px;
}

.chat-meta strong {
  color: #54776f;
}

.typing-bubble {
  display: flex;
  align-items: center;
  gap: 5px;
  color: #72859a;
}

.typing-bubble i {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  animation: typing 1s infinite alternate;
  background: #607fb6;
}

.typing-bubble i:nth-child(2) {
  animation-delay: 0.2s;
}

.typing-bubble i:nth-child(3) {
  animation-delay: 0.4s;
}

.typing-bubble span {
  margin-left: 5px;
  font-size: 11px;
}

.chat-composer {
  padding: 12px clamp(20px, 3vw, 44px) 14px;
  border-top: 1px solid rgb(225 232 239 / 78%);
  background: rgb(255 255 255 / 84%);
  backdrop-filter: blur(16px) saturate(145%);
}

.response-suggestions .el-button + .el-button {
  margin-left: 0;
}

.composer-box {
  max-width: 1120px;
  margin: 0 auto;
  padding: 10px 12px 10px 16px;
  border: 1px solid var(--top-border-strong);
  border-radius: var(--top-radius-lg);
  background: var(--top-surface);
  box-shadow: 0 12px 32px rgb(16 42 67 / 9%);
  transition:
    border-color var(--top-duration-fast) ease,
    box-shadow var(--top-duration-fast) ease;
}

.composer-box:focus-within {
  border-color: rgb(15 130 119 / 54%);
  box-shadow: 0 0 0 3px rgb(15 130 119 / 8%), 0 14px 34px rgb(16 42 67 / 10%);
}

.composer-box :deep(.el-textarea__inner) {
  padding: 4px 0;
  border: 0;
  box-shadow: none;
}

.composer-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding-top: 8px;
  border-top: 1px solid #edf1f5;
}

.composer-actions span {
  color: #8b99a7;
  font-size: 10px;
}

.ai-sidebar {
  display: flex;
  min-height: 0;
  flex-direction: column;
  border-left: 1px solid var(--top-border);
  background:
    linear-gradient(180deg, rgb(238 234 253 / 42%) 0, transparent 12rem),
    var(--top-surface-subtle);
}

.sidebar-section {
  padding: 16px;
}

.scope-section {
  padding-bottom: 4px;
}

.scope-card {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 11px;
  border: 1px solid rgb(106 80 189 / 13%);
  border-radius: var(--top-radius-md);
  background: rgb(255 255 255 / 78%);
  box-shadow: 0 8px 22px rgb(89 65 169 / 6%);
}

.scope-card-icon {
  display: grid;
  width: 32px;
  height: 32px;
  flex: 0 0 32px;
  place-items: center;
  border-radius: 9px;
  color: var(--top-ai-600);
  background: var(--top-ai-100);
}

.scope-card > div {
  display: grid;
  min-width: 0;
  gap: 2px;
}

.scope-card small {
  color: var(--top-ai-600);
  font-size: 8px;
  font-weight: 800;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.scope-card strong,
.scope-card p {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.scope-card strong {
  color: var(--top-text-strong);
  font-size: 12px;
}

.scope-card p {
  margin: 0;
  color: var(--top-text-muted);
  font-size: 9px;
}

.quick-section {
  border-bottom: 1px solid var(--top-border);
}

.sidebar-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 12px;
}

.sidebar-heading > div {
  display: grid;
  gap: 2px;
}

.sidebar-heading small {
  color: #8a9aab;
  font-size: 9px;
  font-weight: 750;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.sidebar-heading strong {
  color: #253f59;
  font-size: 13px;
}

.quick-prompt {
  display: flex;
  width: 100%;
  align-items: flex-start;
  gap: 8px;
  margin-top: 7px;
  padding: 9px 10px;
  border: 1px solid var(--top-border);
  border-radius: var(--top-radius-md);
  background: rgb(255 255 255 / 88%);
  color: var(--top-text);
  font: inherit;
  font-size: 11px;
  line-height: 1.45;
  text-align: left;
  cursor: pointer;
  transition:
    border-color var(--top-duration-fast) ease,
    background var(--top-duration-fast) ease,
    color var(--top-duration-fast) ease,
    transform var(--top-duration-fast) ease;
}

.quick-prompt:hover:not(:disabled) {
  border-color: rgb(106 80 189 / 30%);
  background: #fff;
  color: var(--top-ai-700);
  transform: translateY(-1px);
}

.quick-prompt:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

.quick-prompt .el-icon {
  flex: 0 0 auto;
  margin-top: 2px;
  color: var(--top-ai-600);
}

.history-section {
  min-height: 0;
  flex: 1;
  overflow-y: auto;
}

.conversation-empty {
  padding: 18px 8px;
  color: #98a5b3;
  font-size: 11px;
  text-align: center;
}

.conversation-item {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 7px;
  padding: 9px 7px 9px 10px;
  border: 1px solid transparent;
  border-radius: var(--top-radius-md);
  color: #52677b;
  cursor: pointer;
}

.conversation-item:hover,
.conversation-item.active {
  border-color: var(--top-border);
  background: var(--top-surface);
}

.conversation-item.active {
  box-shadow: inset 3px 0 var(--top-ai-600), var(--top-shadow-1);
}

.conversation-item > div:first-child {
  display: grid;
  min-width: 0;
  flex: 1;
  gap: 3px;
}

.conversation-item strong {
  overflow: hidden;
  color: #344e66;
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.conversation-item small {
  overflow: hidden;
  color: #93a0ad;
  font-size: 9px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

@keyframes typing {
  from { opacity: 0.35; transform: translateY(1px); }
  to { opacity: 1; transform: translateY(-1px); }
}

@media (min-width: 1500px) {
  .structured-answer .chat-answer-sections {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 1050px) {
  .ai-context-bar {
    align-items: stretch;
    flex-wrap: wrap;
  }

  .context-field,
  .context-field.run-field,
  .context-field.model-field {
    width: calc(50% - 8px);
  }

  .selected-run-summary {
    margin-left: 0;
  }

  .chat-shell {
    grid-template-columns: minmax(0, 1fr) 240px;
  }
}

@media (max-width: 800px) {
  .context-field,
  .context-field.run-field,
  .context-field.model-field {
    width: 100%;
  }

  .selected-run-summary {
    width: 100%;
    flex-wrap: wrap;
  }

  .chat-transcript,
  .chat-composer {
    padding-right: 16px;
    padding-left: 16px;
  }

  .chat-shell {
    grid-template-columns: 1fr;
    grid-template-rows: minmax(0, 1fr) auto;
  }

  .ai-sidebar {
    max-height: 210px;
    border-top: 1px solid #e2eaf3;
    border-left: 0;
    overflow-y: auto;
  }

  .quick-section {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 7px;
  }

  .quick-section .sidebar-heading {
    grid-column: 1 / -1;
    margin-bottom: 2px;
  }

  .quick-prompt {
    margin-top: 0;
  }

  .history-section {
    display: none;
  }
}
</style>
