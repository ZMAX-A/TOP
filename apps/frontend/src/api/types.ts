export type SystemRole = 'USER' | 'SYSTEM_ADMIN'
export type UserStatus = 'ACTIVE' | 'DISABLED'
export type ProjectRole = 'VIEWER' | 'TESTER' | 'REVIEWER' | 'PROJECT_ADMIN'
export type RunStatus =
  | 'QUEUED'
  | 'PREPARING'
  | 'RUNNING'
  | 'PASSED'
  | 'FAILED'
  | 'CANCELED'
  | 'TIMED_OUT'
  | 'INFRA_ERROR'

export interface User {
  id: string
  username: string
  display_name: string
  system_role: SystemRole
  status: UserStatus
  created_at: string
}

export interface LoginSession {
  access_token: string
  token_type: 'bearer'
  expires_at: string
  user: User
}

export interface Project {
  id: string
  key: string
  name: string
  description: string | null
  status: string
  created_at: string
  updated_at: string
}

export interface ExecutionPolicy {
  project_id: string
  max_in_flight_runs: number
  max_daily_runs: number
  in_flight_runs: number
  queued_runs: number
  preparing_runs: number
  running_runs: number
  runs_created_today: number
  remaining_in_flight_runs: number
  remaining_daily_runs: number
  quota_status: 'AVAILABLE' | 'NEAR_LIMIT' | 'BLOCKED'
  daily_window_started_at: string
  generated_at: string
  updated_at: string
}

export interface RunnerPoolCatalog {
  id: string
  key: string
  name: string
  target_types: Array<'WEB' | 'APP' | 'API'>
  status: 'ACTIVE' | 'DRAINING' | 'DISABLED'
  available_slots: number
}

export interface RunnerPool extends RunnerPoolCatalog {
  description: string | null
  queue_name: string
  max_concurrency: number
  healthy_workers: number
  total_worker_slots: number
  active_leases: number
  created_at: string
  updated_at: string
}

export interface RunnerCapabilities {
  target_types: Array<'WEB' | 'APP' | 'API'>
  browsers: string[]
  labels: Record<string, string>
}

export interface RunnerWorker {
  id: string
  pool_id: string
  pool_key: string
  worker_key: string
  display_name: string
  runner_version: string
  max_slots: number
  capabilities: RunnerCapabilities
  status: 'ACTIVE' | 'DRAINING' | 'DISABLED'
  health: 'ONLINE' | 'STALE'
  last_heartbeat_at: string
  created_at: string
  updated_at: string
}

export interface Target {
  id: string
  project_id: string
  key: string
  name: string
  target_type: 'WEB' | 'APP' | 'API'
  browser: string | null
  runner_pool_id: string | null
  status: 'ACTIVE' | 'ARCHIVED'
  created_at: string
  updated_at: string
}

export interface RuntimeVariable {
  name: string
  value: string
}

export interface SecretBinding {
  name: string
  ref: string
}

export interface Environment {
  id: string
  project_id: string
  target_id: string
  runner_pool_id: string | null
  key: string
  name: string
  web_config: Record<string, unknown> | null
  variables: RuntimeVariable[]
  secret_bindings: SecretBinding[]
  config_hash: string
  status: 'ACTIVE' | 'ARCHIVED'
  created_at: string
  updated_at: string
}

export interface ProjectMember {
  id: string
  project_id: string
  user_id: string
  username: string
  display_name: string
  role: ProjectRole
  created_at: string
  updated_at: string
}

export interface ProjectMemberCandidate {
  id: string
  username: string
  display_name: string
}

export interface ManagedSession {
  id: string
  user_id: string
  username: string
  display_name: string
  expires_at: string
  created_at: string
  revoked_at: string | null
  active: boolean
}

export interface AuditLog {
  id: string
  project_id: string | null
  actor_id: string
  actor_username: string | null
  actor_display_name: string | null
  action: string
  resource_type: string
  resource_id: string
  details: Record<string, unknown>
  created_at: string
}

export interface Page<T> {
  items: T[]
  total: number
  offset: number
  limit: number
}

export interface AutomationPackage {
  id: string
  project_id: string
  target_id: string
  name: string
  version: string
  digest: string
}

export interface Baseline {
  baseline_id: string
  project_id: string
  version: string
  digest: string
  case_count: number
  enabled_case_count: number
  source_kind: string
  status: string
  created_at: string
}

export interface CaseDefinition {
  case_id: string
  case_code: string
  module_key: string
  module_name: string
  title: string
  test_point: string
  enabled: boolean
  [key: string]: unknown
}

export interface CaseBaseline {
  baseline_id: string
  project_key: string
  version: string
  cases: CaseDefinition[]
  [key: string]: unknown
}

export type ChangeStatus =
  | 'DRAFT'
  | 'IN_REVIEW'
  | 'CHANGES_REQUESTED'
  | 'CANDIDATE'
  | 'PUBLISHED'

export interface ChangeSummary {
  id: string
  project_id: string
  base_baseline_id: string
  candidate_baseline_id: string
  candidate_version: string
  candidate_digest: string
  title: string
  reason: string
  status: ChangeStatus
  validation_status: string
  validation_run_id: string | null
  created_by: string
  submitted_at: string | null
  reviewed_at: string | null
  published_at: string | null
  published_baseline_id: string | null
  created_at: string
  updated_at: string
  change_count: number
}

export interface ChangeItem {
  id: string
  sequence: number
  change_type: 'ADD' | 'MODIFY' | 'DELETE'
  case_id: string
  case_code: string
  before: Record<string, unknown> | null
  after: Record<string, unknown> | null
  changed_fields: string[]
}

export interface Approval {
  id: string
  reviewer_id: string
  decision: 'APPROVE' | 'REQUEST_CHANGES'
  comment: string | null
  created_at: string
}

export interface ChangeDetail extends ChangeSummary {
  candidate_baseline: CaseBaseline
  changes: ChangeItem[]
  approvals: Approval[]
}

export interface Run {
  id: string
  project_id: string
  target_id: string
  environment_id: string
  baseline_id: string
  automation_package_id: string
  runner_pool_id: string | null
  source_run_id: string | null
  retry_mode: 'FULL' | 'FAILED_ONLY' | null
  status: RunStatus
  case_count: number
  snapshot_digest: string
  result_digest: string | null
  cancel_requested: boolean
  dispatch_state: 'PENDING' | 'WAITING' | 'DISPATCHED'
  dispatch_wait_reason: string | null
  dispatched_at: string | null
  created_by: string
  created_at: string
  started_at: string | null
  finished_at: string | null
  error_message: string | null
}

export interface RunList {
  items: Run[]
  total: number
}

export interface RunBatchCancelResult {
  items: Run[]
  requested: number
  changed: number
}

export interface RunCase {
  id: string
  case_id: string
  case_code: string
  sequence: number
  status: string
  duration_ms: number | null
  failure_category: string | null
  error_message: string | null
  created_at: string
  updated_at: string
}

export interface Artifact {
  artifact_id: string
  run_id: string
  kind: string
  name: string
  uri: string
  digest: string
  size_bytes: number
  created_at: string
}

export interface CaseResult {
  case_id: string
  case_code: string
  status: string
  started_at: string
  finished_at: string
  duration_ms: number
  failure_category?: string
  error_message?: string
  artifact_ids: string[]
}

export interface RunResult {
  schema_version: string
  run_id: string
  status: RunStatus
  started_at: string
  finished_at: string
  runner_version: string
  case_results: CaseResult[]
  artifacts: Artifact[]
}

export interface RunSnapshot {
  run_id: string
  project_id: string
  target_id: string
  environment_id: string
  browser: string | null
  config_hash: string
  case_baseline: {
    baseline_id: string
    version: string
    digest: string
    case_count: number
  }
  automation_package: {
    name: string
    version: string
    digest: string
  }
  [key: string]: unknown
}

export interface RunDetail extends Run {
  snapshot: RunSnapshot
  result: RunResult | null
  cases: RunCase[]
  artifacts: Artifact[]
}

export interface RunEvent {
  id: string
  run_id: string
  sequence: number
  source: string
  event_type: string
  case_code: string | null
  status: string | null
  payload: Record<string, unknown>
  occurred_at: string
  created_at: string
}

export interface ArtifactAccess {
  artifact_id: string
  name: string
  kind: string
  digest: string
  size_bytes: number
  url: string
  expires_at: string
}
