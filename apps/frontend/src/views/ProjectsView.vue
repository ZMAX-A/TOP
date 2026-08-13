<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ArrowRight, Box, Lock, Plus } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'

import { ApiError, api } from '@/api/client'
import type { Project } from '@/api/types'
import { auth } from '@/auth'
import { formatDate } from '@/presentation'

const router = useRouter()
const loading = ref(true)
const projects = ref<Project[]>([])

async function load(): Promise<void> {
  loading.value = true
  try {
    projects.value = await api.projects()
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : '项目列表加载失败')
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="page-container">
    <header class="page-heading">
      <div>
        <span class="eyebrow-dark">Project workspace</span>
        <h1>项目空间</h1>
        <p>仅展示当前账号拥有成员关系的测试项目。</p>
      </div>
      <el-button
        v-if="auth.state.user?.system_role === 'SYSTEM_ADMIN'"
        type="primary"
        @click="router.push({ name: 'admin', query: { view: 'projects' } })"
      >
        <el-icon><Plus /></el-icon>
        新建项目
      </el-button>
    </header>

    <div v-loading="loading" class="project-grid">
      <article
        v-for="project in projects"
        :key="project.id"
        class="project-card"
        tabindex="0"
        @click="router.push({ name: 'project', params: { projectId: project.id } })"
        @keyup.enter="router.push({ name: 'project', params: { projectId: project.id } })"
      >
        <div class="project-card-head">
          <span class="project-icon"><Box /></span>
          <el-tag effect="plain" type="success" size="small">{{ project.status }}</el-tag>
        </div>
        <h2>{{ project.name }}</h2>
        <code>{{ project.key }}</code>
        <p>{{ project.description || '自动化测试治理与可追溯执行空间' }}</p>
        <footer>
          <span>更新于 {{ formatDate(project.updated_at) }}</span>
          <el-icon><ArrowRight /></el-icon>
        </footer>
      </article>
      <el-empty v-if="!loading && projects.length === 0" class="empty-projects">
        <template #description>
          <p>当前账号还没有加入任何项目</p>
          <span><el-icon><Lock /></el-icon> 请联系项目管理员添加成员关系</span>
        </template>
      </el-empty>
    </div>
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

.project-grid {
  display: grid;
  min-height: 250px;
  grid-template-columns: repeat(auto-fill, minmax(310px, 1fr));
  gap: 18px;
}

.project-card {
  min-height: 246px;
  padding: 24px;
  border: 1px solid #e5eaee;
  border-radius: 12px;
  background: #fff;
  box-shadow: 0 3px 12px rgb(16 42 67 / 4%);
  cursor: pointer;
  transition:
    transform 160ms ease,
    border-color 160ms ease,
    box-shadow 160ms ease;
}

.project-card:hover,
.project-card:focus-visible {
  border-color: #8bd3c9;
  outline: none;
  box-shadow: 0 14px 30px rgb(16 42 67 / 9%);
  transform: translateY(-3px);
}

.project-card-head,
.project-card footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.project-icon {
  display: grid;
  width: 42px;
  height: 42px;
  place-items: center;
  border-radius: 10px;
  color: #167d73;
  background: #e9f6f3;
}

.project-icon svg {
  width: 21px;
}

.project-card h2 {
  margin: 22px 0 5px;
  color: #17324d;
  font-size: 19px;
}

.project-card code {
  color: #168579;
  font-size: 11px;
}

.project-card p {
  min-height: 44px;
  margin: 14px 0 22px;
  color: #718096;
  font-size: 13px;
  line-height: 1.65;
}

.project-card footer {
  padding-top: 17px;
  border-top: 1px solid #eef1f3;
  color: #8795a5;
  font-size: 11px;
}

.project-card footer .el-icon {
  color: #168579;
}

.empty-projects {
  grid-column: 1 / -1;
  border: 1px dashed #d9e0e6;
  border-radius: 12px;
  background: #fff;
}

.empty-projects span {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: #8998a7;
  font-size: 12px;
}
</style>
