<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { RefreshRight } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'

import { ApiError, api } from '@/api/client'
import type { ChangeSummary, Project } from '@/api/types'
import {
  changeStatusLabel,
  changeTagType,
  formatDate,
  shortDigest,
} from '@/presentation'

interface InboxChange extends ChangeSummary {
  project_key: string
  project_name: string
}

const router = useRouter()
const loading = ref(true)
const projects = ref<Project[]>([])
const rows = ref<InboxChange[]>([])
const selectedProject = ref('ALL')
const selectedStatus = ref('ALL')

const statusOptions = [
  { label: '全部状态', value: 'ALL' },
  { label: '待审批', value: 'IN_REVIEW' },
  { label: '草稿', value: 'DRAFT' },
  { label: '需修改', value: 'CHANGES_REQUESTED' },
  { label: '候选版本', value: 'CANDIDATE' },
  { label: '已发布', value: 'PUBLISHED' },
]

const filteredRows = computed(() =>
  rows.value.filter(
    (row) =>
      (selectedProject.value === 'ALL' || row.project_id === selectedProject.value) &&
      (selectedStatus.value === 'ALL' || row.status === selectedStatus.value),
  ),
)

const pendingReviewCount = computed(
  () => rows.value.filter((row) => row.status === 'IN_REVIEW').length,
)
const activeChangeCount = computed(
  () => rows.value.filter((row) => row.status !== 'PUBLISHED').length,
)

async function load(): Promise<void> {
  loading.value = true
  try {
    const projectList = await api.projects()
    projects.value = projectList
    const groups = await Promise.all(
      projectList.map(async (project) => {
        try {
          const changes = await api.changes(project.id)
          return changes.map((change) => ({
            ...change,
            project_key: project.key,
            project_name: project.name,
          }))
        } catch (error) {
          if (error instanceof ApiError && error.status === 403) return []
          throw error
        }
      }),
    )
    rows.value = groups.flat().sort((left, right) =>
      right.updated_at.localeCompare(left.updated_at),
    )
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : '变更工作台加载失败')
  } finally {
    loading.value = false
  }
}

function openChange(row: InboxChange): void {
  void router.push({
    name: 'change-detail',
    params: { projectId: row.project_id, requestId: row.id },
  })
}

onMounted(load)
</script>

<template>
  <div class="page-container">
    <header class="page-heading">
      <div>
        <span class="eyebrow">Governed changes</span>
        <h1>审批与变更</h1>
        <p>跨项目查看用例草稿、审批状态和候选版本发布进度。</p>
      </div>
      <el-button :loading="loading" @click="load">
        <el-icon><RefreshRight /></el-icon>
        刷新
      </el-button>
    </header>

    <section class="summary-grid">
      <article class="summary-card">
        <span>待审批</span>
        <strong>{{ pendingReviewCount }}</strong>
        <small>等待 Reviewer 决策</small>
      </article>
      <article class="summary-card">
        <span>进行中</span>
        <strong>{{ activeChangeCount }}</strong>
        <small>未发布的变更请求</small>
      </article>
      <article class="summary-card">
        <span>可见项目</span>
        <strong>{{ projects.length }}</strong>
        <small>受项目成员权限约束</small>
      </article>
    </section>

    <section v-loading="loading" class="surface inbox-surface">
      <div class="toolbar filters">
        <div>
          <strong>变更请求</strong>
          <span>共 {{ filteredRows.length }} 条</span>
        </div>
        <div class="filter-controls">
          <el-select v-model="selectedProject" aria-label="项目筛选" style="width: 210px">
            <el-option label="全部项目" value="ALL" />
            <el-option
              v-for="project in projects"
              :key="project.id"
              :label="`${project.name} · ${project.key}`"
              :value="project.id"
            />
          </el-select>
          <el-select v-model="selectedStatus" aria-label="状态筛选" style="width: 150px">
            <el-option
              v-for="option in statusOptions"
              :key="option.value"
              :label="option.label"
              :value="option.value"
            />
          </el-select>
        </div>
      </div>

      <el-table
        v-if="filteredRows.length > 0"
        :data="filteredRows"
        row-class-name="clickable-row"
        @row-click="openChange"
      >
        <el-table-column label="项目" min-width="175">
          <template #default="{ row }">
            <strong class="project-name">{{ row.project_name }}</strong>
            <code>{{ row.project_key }}</code>
          </template>
        </el-table-column>
        <el-table-column label="变更" min-width="250">
          <template #default="{ row }">
            <span class="table-link">{{ row.title }}</span>
            <small class="reason">{{ row.reason }}</small>
          </template>
        </el-table-column>
        <el-table-column label="候选版本" width="150">
          <template #default="{ row }">
            <code>{{ row.candidate_version }}</code>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <el-tag :type="changeTagType(row.status)" effect="light">
              {{ changeStatusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="验证" width="145" prop="validation_status" />
        <el-table-column label="摘要" width="170">
          <template #default="{ row }">
            <span class="mono muted">{{ shortDigest(row.candidate_digest) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="更新时间" width="170">
          <template #default="{ row }">{{ formatDate(row.updated_at) }}</template>
        </el-table-column>
      </el-table>
      <el-empty v-else-if="!loading" class="empty-block" description="当前筛选条件下没有变更请求" />
    </section>
  </div>
</template>

<style scoped>
.eyebrow {
  color: #168579;
  font-size: 11px;
  font-weight: 750;
  letter-spacing: 0.16em;
  text-transform: uppercase;
}

.summary-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
  margin-bottom: 20px;
}

.summary-card {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 5px 20px;
  padding: 20px 22px;
  border: 1px solid #e7ebef;
  border-radius: 12px;
  background: #fff;
  box-shadow: 0 2px 10px rgb(16 42 67 / 4%);
}

.summary-card span {
  color: #60758a;
  font-size: 13px;
  font-weight: 650;
}

.summary-card strong {
  grid-row: 1 / 3;
  grid-column: 2;
  color: #102a43;
  font-size: 28px;
}

.summary-card small {
  color: #8a99a8;
}

.inbox-surface {
  min-height: 320px;
  overflow: hidden;
}

.filters > div:first-child {
  display: grid;
  gap: 3px;
}

.filters strong {
  color: #243b53;
  font-size: 15px;
}

.filters span {
  color: #8795a5;
  font-size: 12px;
}

.filter-controls {
  display: flex;
  gap: 10px;
}

.project-name,
.reason,
.el-table code {
  display: block;
}

.project-name {
  margin-bottom: 3px;
  color: #243b53;
  font-size: 13px;
}

.el-table code {
  color: #168579;
  font-size: 11px;
}

.reason {
  max-width: 360px;
  margin-top: 5px;
  overflow: hidden;
  color: #8392a5;
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

:deep(.clickable-row) {
  cursor: pointer;
}
</style>
