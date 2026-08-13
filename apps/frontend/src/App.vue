<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Collection, Fold, Operation, Setting, UserFilled } from '@element-plus/icons-vue'
import zhCn from 'element-plus/es/locale/lang/zh-cn'

import { auth } from '@/auth'

const route = useRoute()
const router = useRouter()
const isLogin = computed(() => route.name === 'login')

async function logout(): Promise<void> {
  await auth.logout()
  await router.replace({ name: 'login' })
}
</script>

<template>
  <el-config-provider :locale="zhCn">
    <router-view v-if="isLogin" />
    <el-container v-else class="app-shell">
      <el-aside width="248px" class="sidebar">
      <div class="brand">
        <span class="brand-mark">T</span>
        <span>
          <strong>TestOps</strong>
          <small>Platform</small>
        </span>
      </div>
      <el-menu router :default-active="route.path" class="navigation">
        <el-menu-item index="/projects">
          <el-icon><Collection /></el-icon>
          <span>项目空间</span>
        </el-menu-item>
        <el-menu-item index="/changes">
          <el-icon><Operation /></el-icon>
          <span>审批与变更</span>
        </el-menu-item>
        <el-menu-item v-if="auth.state.user?.system_role === 'SYSTEM_ADMIN'" index="/admin">
          <el-icon><Setting /></el-icon>
          <span>系统管理</span>
        </el-menu-item>
      </el-menu>
      <div class="sidebar-foot">
        <div class="security-note">
          <el-icon><Fold /></el-icon>
          <span>Released 基线受保护</span>
        </div>
      </div>
      </el-aside>
      <el-container>
      <el-header class="topbar">
        <div>
          <span class="environment-dot" />
          <span class="environment-label">控制面在线</span>
        </div>
        <el-dropdown trigger="click">
          <button class="user-menu" type="button">
            <el-icon><UserFilled /></el-icon>
            <span>{{ auth.state.user?.display_name }}</span>
            <small>{{ auth.state.user?.system_role === 'SYSTEM_ADMIN' ? '系统管理员' : '成员' }}</small>
          </button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item @click="logout">退出登录</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </el-header>
      <el-main class="page-area">
        <router-view />
      </el-main>
      </el-container>
    </el-container>
  </el-config-provider>
</template>
