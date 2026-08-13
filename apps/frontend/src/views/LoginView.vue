<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Lock, User } from '@element-plus/icons-vue'
import type { FormInstance, FormRules } from 'element-plus'
import { ElMessage } from 'element-plus'

import { ApiError } from '@/api/client'
import { auth } from '@/auth'

const route = useRoute()
const router = useRouter()
const formRef = ref<FormInstance>()
const submitting = ref(false)
const form = reactive({ username: '', password: '' })
const rules: FormRules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

async function submit(): Promise<void> {
  if (!(await formRef.value?.validate().catch(() => false))) return
  submitting.value = true
  try {
    await auth.login(form.username, form.password)
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/projects'
    await router.replace(redirect)
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : '登录失败，请稍后重试')
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <main class="login-page">
    <section class="login-story">
      <div class="login-brand">
        <span class="brand-mark">T</span>
        <strong>TestOps Platform</strong>
      </div>
      <div class="story-copy">
        <span class="eyebrow">Governed automation</span>
        <h1>让每一次自动化执行<br />都有版本、有审批、有证据。</h1>
        <p>统一管理项目、标准用例、执行环境和测试报告，同时保护已发布回归基线。</p>
      </div>
      <div class="story-flow" aria-label="平台治理流程">
        <span>草稿</span><i /> <span>验证</span><i /> <span>审批</span><i /> <span>发布</span>
      </div>
    </section>
    <section class="login-panel">
      <div class="login-card">
        <div class="login-card-head">
          <span class="secure-pill">安全会话</span>
          <h2>登录控制台</h2>
          <p>使用平台管理员分配的账号继续</p>
        </div>
        <el-form ref="formRef" :model="form" :rules="rules" size="large" @submit.prevent="submit">
          <el-form-item prop="username">
            <el-input v-model="form.username" autocomplete="username" placeholder="用户名">
              <template #prefix><el-icon><User /></el-icon></template>
            </el-input>
          </el-form-item>
          <el-form-item prop="password">
            <el-input
              v-model="form.password"
              type="password"
              autocomplete="current-password"
              placeholder="密码"
              show-password
              @keyup.enter="submit"
            >
              <template #prefix><el-icon><Lock /></el-icon></template>
            </el-input>
          </el-form-item>
          <el-button native-type="submit" type="primary" size="large" :loading="submitting">
            进入平台
          </el-button>
        </el-form>
        <p class="login-help">会话令牌仅保存在当前浏览器，服务端只保存令牌摘要。</p>
      </div>
    </section>
  </main>
</template>

<style scoped>
.login-page {
  display: grid;
  min-height: 100vh;
  grid-template-columns: minmax(560px, 1.1fr) minmax(500px, 0.9fr);
  background: #fff;
}

.login-story {
  position: relative;
  display: flex;
  overflow: hidden;
  flex-direction: column;
  justify-content: space-between;
  padding: 38px 54px 48px;
  color: #fff;
  background:
    radial-gradient(circle at 78% 18%, rgb(53 208 186 / 24%), transparent 30%),
    linear-gradient(145deg, #102f4d 0%, #0b2137 62%, #081a2b 100%);
}

.login-story::after {
  position: absolute;
  right: -100px;
  bottom: -170px;
  width: 520px;
  height: 520px;
  border: 1px solid rgb(255 255 255 / 8%);
  border-radius: 50%;
  box-shadow:
    0 0 0 70px rgb(255 255 255 / 2%),
    0 0 0 140px rgb(255 255 255 / 2%);
  content: "";
}

.login-brand {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 16px;
  letter-spacing: 0.02em;
}

.story-copy {
  position: relative;
  z-index: 1;
  max-width: 670px;
  margin-top: auto;
  margin-bottom: auto;
}

.eyebrow {
  color: #69dfcd;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.2em;
  text-transform: uppercase;
}

.story-copy h1 {
  margin: 22px 0;
  font-size: clamp(37px, 4vw, 60px);
  font-weight: 720;
  letter-spacing: -0.045em;
  line-height: 1.22;
}

.story-copy p {
  max-width: 580px;
  margin: 0;
  color: #aec2d4;
  font-size: 16px;
  line-height: 1.8;
}

.story-flow {
  position: relative;
  z-index: 1;
  display: flex;
  align-items: center;
  gap: 14px;
  color: #b9cbda;
  font-size: 12px;
  font-weight: 600;
}

.story-flow i {
  width: 34px;
  height: 1px;
  background: #35627e;
}

.login-panel {
  display: grid;
  place-items: center;
  padding: 48px;
  background: #f7f9fa;
}

.login-card {
  width: min(410px, 100%);
  padding: 38px;
  border: 1px solid #e6ebef;
  border-radius: 14px;
  background: #fff;
  box-shadow: 0 24px 60px rgb(16 42 67 / 10%);
}

.login-card-head {
  margin-bottom: 30px;
}

.secure-pill {
  display: inline-block;
  padding: 5px 9px;
  border-radius: 5px;
  color: #14766c;
  background: #e8f7f3;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.06em;
}

.login-card h2 {
  margin: 18px 0 7px;
  color: #102a43;
  font-size: 27px;
}

.login-card-head p,
.login-help {
  margin: 0;
  color: #7b8b9a;
  font-size: 13px;
}

.login-card .el-button {
  width: 100%;
  margin-top: 7px;
}

.login-help {
  margin-top: 24px;
  text-align: center;
  line-height: 1.6;
}
</style>
