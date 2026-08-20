import type {
  AutomationPackage,
  ArtifactAccess,
  AuditLog,
  Baseline,
  CaseBaseline,
  ChangeDetail,
  ChangeSummary,
  Environment,
  ExecutionPolicy,
  LoginSession,
  ManagedSession,
  Page,
  Project,
  ProjectMember,
  ProjectMemberCandidate,
  QualityAlertMetric,
  QualityAlertState,
  QualityAnalytics,
  QualityAnalyticsQuery,
  QualityPolicy,
  QualityWebhookConfig,
  QualityWebhookDelivery,
  RegressionSchedule,
  RegressionScheduleFiring,
  Run,
  RunBatchCancelResult,
  RunDetail,
  RunEvent,
  RunList,
  RunnerPool,
  RunnerPoolCatalog,
  RunnerWorker,
  Target,
  User,
  UserStatus,
  SystemRole,
} from './types'

const API_BASE = (import.meta.env.VITE_API_URL ?? '').replace(/\/$/, '')
const TOKEN_KEY = 'testops.session-token'

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message)
  }
}

export function sessionToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function saveSessionToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token)
}

export function clearSessionToken(): void {
  localStorage.removeItem(TOKEN_KEY)
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers)
  if (options.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  const token = sessionToken()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const response = await fetch(`${API_BASE}${path}`, { ...options, headers })
  if (response.status === 204) return undefined as T
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    const detail = payload?.detail
    const message = Array.isArray(detail)
      ? detail.map((item: { msg?: string }) => item.msg ?? '请求校验失败').join('；')
      : detail ?? `请求失败（${response.status}）`
    if (response.status === 401) clearSessionToken()
    throw new ApiError(response.status, message)
  }
  return payload as T
}

async function responseError(response: Response): Promise<ApiError> {
  const payload = await response.json().catch(() => null)
  const detail = payload?.detail
  const message = Array.isArray(detail)
    ? detail.map((item: { msg?: string }) => item.msg ?? '请求校验失败').join('；')
    : detail ?? `请求失败（${response.status}）`
  if (response.status === 401) clearSessionToken()
  return new ApiError(response.status, message)
}

function json(method: string, body?: unknown, headers?: HeadersInit): RequestInit {
  return {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  }
}

function withQuery(
  path: string,
  values: Record<string, string | number | boolean | string[] | undefined>,
): string {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(values)) {
    if (Array.isArray(value)) {
      for (const item of value) query.append(key, item)
    } else if (value !== undefined && value !== '') {
      query.set(key, String(value))
    }
  }
  const serialized = query.toString()
  return serialized ? `${path}?${serialized}` : path
}

export const api = {
  login: (username: string, password: string) =>
    request<LoginSession>('/api/v1/auth/login', json('POST', { username, password })),
  me: () => request<User>('/api/v1/auth/me'),
  logout: () => request<void>('/api/v1/auth/logout', { method: 'POST' }),
  createUser: (payload: unknown) => request<User>('/api/v1/users', json('POST', payload)),
  adminUsers: (filters: {
    query?: string
    status?: UserStatus
    system_role?: SystemRole
    offset?: number
    limit?: number
  }) => request<Page<User>>(withQuery('/api/v1/admin/users', filters)),
  updateUser: (userId: string, payload: unknown) =>
    request<User>(`/api/v1/admin/users/${userId}`, json('PATCH', payload)),
  resetUserPassword: (userId: string, password: string) =>
    request<User>(
      `/api/v1/admin/users/${userId}/password-reset`,
      json('POST', { password }),
    ),
  adminSessions: (filters: {
    user_id?: string
    active_only?: boolean
    offset?: number
    limit?: number
  }) => request<Page<ManagedSession>>(withQuery('/api/v1/admin/sessions', filters)),
  revokeSession: (sessionId: string) =>
    request<ManagedSession>(`/api/v1/admin/sessions/${sessionId}/revoke`, {
      method: 'POST',
    }),
  auditLogs: (filters: {
    project_id?: string
    actor_id?: string
    action?: string
    resource_type?: string
    offset?: number
    limit?: number
  }) => request<Page<AuditLog>>(withQuery('/api/v1/admin/audit-logs', filters)),
  runnerPoolCatalog: () => request<RunnerPoolCatalog[]>('/api/v1/runner-pools/catalog'),
  adminRunnerPools: () => request<RunnerPool[]>('/api/v1/admin/runner-pools'),
  createRunnerPool: (payload: unknown) =>
    request<RunnerPool>('/api/v1/admin/runner-pools', json('POST', payload)),
  updateRunnerPool: (poolId: string, payload: unknown) =>
    request<RunnerPool>(`/api/v1/admin/runner-pools/${poolId}`, json('PATCH', payload)),
  adminRunnerWorkers: (poolId?: string) =>
    request<RunnerWorker[]>(withQuery('/api/v1/admin/runner-workers', { pool_id: poolId })),
  updateRunnerWorker: (workerId: string, payload: unknown) =>
    request<RunnerWorker>(
      `/api/v1/admin/runner-workers/${workerId}`,
      json('PATCH', payload),
    ),
  projects: () => request<Project[]>('/api/v1/projects'),
  createProject: (payload: unknown) =>
    request<Project>('/api/v1/projects', json('POST', payload)),
  updateProject: (projectId: string, payload: unknown) =>
    request<Project>(`/api/v1/projects/${projectId}`, json('PATCH', payload)),
  executionPolicy: (projectId: string) =>
    request<ExecutionPolicy>(`/api/v1/projects/${projectId}/execution-policy`),
  updateExecutionPolicy: (projectId: string, payload: unknown) =>
    request<ExecutionPolicy>(
      `/api/v1/projects/${projectId}/execution-policy`,
      json('PATCH', payload),
    ),
  qualityPolicy: (projectId: string) =>
    request<QualityPolicy>(`/api/v1/projects/${projectId}/quality-policy`),
  updateQualityPolicy: (projectId: string, payload: unknown) =>
    request<QualityPolicy>(
      `/api/v1/projects/${projectId}/quality-policy`,
      json('PATCH', payload),
    ),
  qualityWebhook: (projectId: string) =>
    request<QualityWebhookConfig>(`/api/v1/projects/${projectId}/quality/webhook`),
  updateQualityWebhook: (projectId: string, payload: unknown) =>
    request<QualityWebhookConfig>(
      `/api/v1/projects/${projectId}/quality/webhook`,
      json('PATCH', payload),
    ),
  silenceQualityAlerts: (projectId: string, payload: unknown) =>
    request<QualityWebhookConfig>(
      `/api/v1/projects/${projectId}/quality/webhook/silence`,
      json('PUT', payload),
    ),
  clearQualityAlertSilence: (projectId: string) =>
    request<QualityWebhookConfig>(
      `/api/v1/projects/${projectId}/quality/webhook/silence`,
      { method: 'DELETE' },
    ),
  testQualityWebhook: (projectId: string) =>
    request<QualityWebhookDelivery>(
      `/api/v1/projects/${projectId}/quality/webhook/test`,
      json('POST'),
    ),
  qualityWebhookDeliveries: (projectId: string, limit = 20) =>
    request<QualityWebhookDelivery[]>(
      withQuery(`/api/v1/projects/${projectId}/quality/webhook/deliveries`, { limit }),
    ),
  replayQualityWebhookDelivery: (projectId: string, deliveryId: string, reason: string) =>
    request<QualityWebhookDelivery>(
      `/api/v1/projects/${projectId}/quality/webhook/deliveries/${deliveryId}/replay`,
      json('POST', { reason }),
    ),
  qualityWebhookStates: (projectId: string) =>
    request<QualityAlertState[]>(`/api/v1/projects/${projectId}/quality/webhook/states`),
  acknowledgeQualityAlert: (projectId: string, metric: QualityAlertMetric, note: string) =>
    request<QualityAlertState>(
      `/api/v1/projects/${projectId}/quality/webhook/states/${metric}/acknowledgement`,
      json('PUT', { note }),
    ),
  clearQualityAlertAcknowledgement: (projectId: string, metric: QualityAlertMetric) =>
    request<QualityAlertState>(
      `/api/v1/projects/${projectId}/quality/webhook/states/${metric}/acknowledgement`,
      { method: 'DELETE' },
    ),
  qualityAnalytics: (projectId: string, query: QualityAnalyticsQuery = {}) =>
    request<QualityAnalytics>(
      withQuery(`/api/v1/projects/${projectId}/quality/analytics`, {
        window_days: query.windowDays,
        target_id: query.targetId,
        environment_id: query.environmentId,
        baseline_id: query.baselineId,
      }),
    ),
  regressionSchedules: (projectId: string) =>
    request<RegressionSchedule[]>(`/api/v1/projects/${projectId}/regression-schedules`),
  createRegressionSchedule: (projectId: string, payload: unknown) =>
    request<RegressionSchedule>(
      `/api/v1/projects/${projectId}/regression-schedules`,
      json('POST', payload),
    ),
  updateRegressionSchedule: (projectId: string, scheduleId: string, payload: unknown) =>
    request<RegressionSchedule>(
      `/api/v1/projects/${projectId}/regression-schedules/${scheduleId}`,
      json('PATCH', payload),
    ),
  regressionScheduleFirings: (projectId: string, scheduleId: string) =>
    request<RegressionScheduleFiring[]>(
      `/api/v1/projects/${projectId}/regression-schedules/${scheduleId}/firings`,
    ),
  triggerRegressionSchedule: (
    projectId: string,
    scheduleId: string,
    idempotencyKey: string,
  ) =>
    request<Run>(
      `/api/v1/projects/${projectId}/regression-schedules/${scheduleId}/trigger`,
      json('POST', undefined, { 'Idempotency-Key': idempotencyKey }),
    ),
  projectMembers: (projectId: string) =>
    request<ProjectMember[]>(`/api/v1/projects/${projectId}/members`),
  memberCandidates: (projectId: string, query = '') =>
    request<ProjectMemberCandidate[]>(
      withQuery(`/api/v1/projects/${projectId}/member-candidates`, { query, limit: 100 }),
    ),
  upsertProjectMember: (projectId: string, payload: unknown) =>
    request<ProjectMember>(`/api/v1/projects/${projectId}/members`, json('PUT', payload)),
  removeProjectMember: (projectId: string, userId: string) =>
    request<void>(`/api/v1/projects/${projectId}/members/${userId}`, { method: 'DELETE' }),
  targets: (projectId: string) => request<Target[]>(`/api/v1/projects/${projectId}/targets`),
  createTarget: (projectId: string, payload: unknown) =>
    request<Target>(`/api/v1/projects/${projectId}/targets`, json('POST', payload)),
  updateTarget: (projectId: string, targetId: string, payload: unknown) =>
    request<Target>(
      `/api/v1/projects/${projectId}/targets/${targetId}`,
      json('PATCH', payload),
    ),
  environments: (projectId: string, targetId: string) =>
    request<Environment[]>(
      `/api/v1/projects/${projectId}/targets/${targetId}/environments`,
    ),
  createEnvironment: (projectId: string, targetId: string, payload: unknown) =>
    request<Environment>(
      `/api/v1/projects/${projectId}/targets/${targetId}/environments`,
      json('POST', payload),
    ),
  updateEnvironment: (
    projectId: string,
    targetId: string,
    environmentId: string,
    payload: unknown,
  ) =>
    request<Environment>(
      `/api/v1/projects/${projectId}/targets/${targetId}/environments/${environmentId}`,
      json('PATCH', payload),
    ),
  packages: (projectId: string, targetId: string) =>
    request<AutomationPackage[]>(
      `/api/v1/projects/${projectId}/targets/${targetId}/automation-packages`,
    ),
  package: (projectId: string, targetId: string, packageId: string) =>
    request<AutomationPackage>(
      `/api/v1/projects/${projectId}/targets/${targetId}/automation-packages/${packageId}`,
    ),
  createPackageDraft: (projectId: string, targetId: string, payload: unknown) =>
    request<AutomationPackage>(
      `/api/v1/projects/${projectId}/targets/${targetId}/automation-packages/drafts`,
      json('POST', payload),
    ),
  createPackageValidationRun: (
    projectId: string,
    targetId: string,
    packageId: string,
    idempotencyKey: string,
    payload: unknown,
  ) =>
    request<Run>(
      `/api/v1/projects/${projectId}/targets/${targetId}/automation-packages/${packageId}/validation-runs`,
      json('POST', payload, { 'Idempotency-Key': idempotencyKey }),
    ),
  activatePackage: (
    projectId: string,
    targetId: string,
    packageId: string,
    validationRunId: string,
  ) =>
    request<AutomationPackage>(
      `/api/v1/projects/${projectId}/targets/${targetId}/automation-packages/${packageId}/activate`,
      json('POST', { validation_run_id: validationRunId }),
    ),
  deprecatePackage: (
    projectId: string,
    targetId: string,
    packageId: string,
    reason: string,
  ) =>
    request<AutomationPackage>(
      `/api/v1/projects/${projectId}/targets/${targetId}/automation-packages/${packageId}/deprecate`,
      json('POST', { reason }),
    ),
  revokePackage: (
    projectId: string,
    targetId: string,
    packageId: string,
    reason: string,
  ) =>
    request<AutomationPackage>(
      `/api/v1/projects/${projectId}/targets/${targetId}/automation-packages/${packageId}/revoke`,
      json('POST', { reason }),
    ),
  baselines: (projectId: string) =>
    request<Baseline[]>(`/api/v1/projects/${projectId}/baselines`),
  baseline: (projectId: string, baselineId: string) =>
    request<CaseBaseline>(`/api/v1/projects/${projectId}/baselines/${baselineId}`),
  changes: (projectId: string) =>
    request<ChangeSummary[]>(`/api/v1/projects/${projectId}/change-requests`),
  change: (projectId: string, requestId: string) =>
    request<ChangeDetail>(`/api/v1/projects/${projectId}/change-requests/${requestId}`),
  createChange: (projectId: string, payload: unknown) =>
    request<ChangeDetail>(
      `/api/v1/projects/${projectId}/change-requests`,
      json('POST', payload),
    ),
  submitChange: (projectId: string, requestId: string) =>
    request<ChangeDetail>(
      `/api/v1/projects/${projectId}/change-requests/${requestId}/submit`,
      { method: 'POST' },
    ),
  decideChange: (projectId: string, requestId: string, payload: unknown) =>
    request<ChangeDetail>(
      `/api/v1/projects/${projectId}/change-requests/${requestId}/decision`,
      json('POST', payload),
    ),
  startValidation: (
    projectId: string,
    requestId: string,
    payload: unknown,
    idempotencyKey: string,
  ) =>
    request<Run | null>(
      `/api/v1/projects/${projectId}/change-requests/${requestId}/validation-runs`,
      json('POST', payload, { 'Idempotency-Key': idempotencyKey }),
    ),
  startRegression: (
    projectId: string,
    requestId: string,
    payload: unknown,
    idempotencyKey: string,
  ) =>
    request<Run>(
      `/api/v1/projects/${projectId}/change-requests/${requestId}/regression-runs`,
      json('POST', payload, { 'Idempotency-Key': idempotencyKey }),
    ),
  publishChange: (projectId: string, requestId: string, regressionRunId: string) =>
    request<ChangeDetail>(
      `/api/v1/projects/${projectId}/change-requests/${requestId}/publish`,
      json('POST', { regression_run_id: regressionRunId, confirmation: 'PUBLISH' }),
    ),
  runs: (
    projectId: string,
    filters: {
      status?: string[]
      target_id?: string
      environment_id?: string
      created_by?: string
      source_run_id?: string
      case_code?: string
      created_from?: string
      created_to?: string
      offset?: number
      limit?: number
    } = {},
  ) => request<RunList>(withQuery(`/api/v1/projects/${projectId}/runs`, filters)),
  cancelRun: (runId: string) =>
    request<Run>(`/api/v1/runs/${runId}/cancel`, { method: 'POST' }),
  batchCancelRuns: (projectId: string, runIds: string[]) =>
    request<RunBatchCancelResult>(
      `/api/v1/projects/${projectId}/runs/batch-cancel`,
      json('POST', { run_ids: runIds }),
    ),
  rerunRun: (runId: string, mode: 'FULL' | 'FAILED_ONLY', idempotencyKey: string) =>
    request<Run>(
      `/api/v1/runs/${runId}/rerun`,
      json('POST', { mode }, { 'Idempotency-Key': idempotencyKey }),
    ),
  run: (runId: string) => request<RunDetail>(`/api/v1/runs/${runId}`),
  runEvents: (runId: string, afterSequence = 0) =>
    request<RunEvent[]>(
      `/api/v1/runs/${runId}/events?after_sequence=${afterSequence}&limit=500`,
    ),
  artifactAccess: (runId: string, artifactId: string) =>
    request<ArtifactAccess>(`/api/v1/runs/${runId}/artifacts/${artifactId}/access`),
}

export async function streamRunEvents(
  runId: string,
  afterSequence: number,
  onEvent: (event: RunEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  const token = sessionToken()
  const headers = new Headers({ Accept: 'text/event-stream' })
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const response = await fetch(
    `${API_BASE}/api/v1/runs/${runId}/events/stream?after_sequence=${afterSequence}`,
    { headers, signal },
  )
  if (!response.ok) throw await responseError(response)
  if (!response.body) throw new ApiError(502, '实时事件响应没有数据流')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { value, done } = await reader.read()
    buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, '\n')
    let boundary = buffer.indexOf('\n\n')
    while (boundary >= 0) {
      const block = buffer.slice(0, boundary)
      buffer = buffer.slice(boundary + 2)
      const data = block
        .split('\n')
        .filter((line) => line.startsWith('data:'))
        .map((line) => line.slice(5).trimStart())
        .join('\n')
      if (data) onEvent(JSON.parse(data) as RunEvent)
      boundary = buffer.indexOf('\n\n')
    }
    if (done) break
  }
}
