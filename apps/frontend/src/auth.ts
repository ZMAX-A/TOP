import { computed, reactive } from 'vue'

import { api, clearSessionToken, saveSessionToken, sessionToken } from '@/api/client'
import type { User } from '@/api/types'

const state = reactive<{
  user: User | null
  restoring: boolean
}>({
  user: null,
  restoring: false,
})

export const auth = {
  state,
  authenticated: computed(() => state.user !== null),
  async restore(): Promise<boolean> {
    if (!sessionToken()) return false
    state.restoring = true
    try {
      state.user = await api.me()
      return true
    } catch {
      clearSessionToken()
      state.user = null
      return false
    } finally {
      state.restoring = false
    }
  },
  async login(username: string, password: string): Promise<void> {
    const session = await api.login(username, password)
    saveSessionToken(session.access_token)
    state.user = session.user
  },
  async logout(): Promise<void> {
    try {
      if (sessionToken()) await api.logout()
    } finally {
      clearSessionToken()
      state.user = null
    }
  },
}
