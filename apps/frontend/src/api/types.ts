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
export type QualityAlertMetric =
  | 'RUN_PASS_RATE'
  | 'CASE_PASS_RATE'
  | 'EXECUTION_RELIABILITY'

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
  run_timeout_seconds: number
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

export interface QualityPolicy {
  project_id: string
  target_pass_rate_percent: number
  window_days: number
  alert_warning_drop_percentage_points: number
  alert_critical_drop_percentage_points: number
  updated_at: string
}

export interface QualityWebhookConfig {
  project_id: string
  enabled: boolean
  endpoint_configured: boolean
  endpoint_display: string | null
  minimum_alert_status: 'WARNING' | 'CRITICAL'
  cooldown_seconds: number
  signing_configured: boolean
  last_evaluated_at: string | null
  next_evaluation_at: string | null
  silenced_until: string | null
  silenced_by: string | null
  silenced_by_display_name: string | null
  silence_reason: string | null
  updated_at: string | null
}

export interface QualityWebhookDelivery {
  id: string
  project_id: string
  event_type: string
  destination_display: string
  status: 'PENDING' | 'DELIVERED' | 'FAILED'
  attempts: number
  response_status: number | null
  last_error: string | null
  replay_of_id: string | null
  replayed_by: string | null
  replayed_by_display_name: string | null
  replay_reason: string | null
  created_at: string
  delivered_at: string | null
}

export interface QualityAlertState {
  project_id: string
  metric: QualityAlertMetric
  current_status: QualityChangeAlertStatus
  active_notification_status: 'WARNING' | 'CRITICAL' | null
  current_percent: number | null
  previous_percent: number | null
  delta_percentage_points: number | null
  notification_sequence: number
  last_evaluated_at: string
  last_transition_at: string
  last_notified_at: string | null
  cooldown_until: string | null
  last_delivery_id: string | null
  acknowledged_at: string | null
  acknowledged_by: string | null
  acknowledged_by_display_name: string | null
  acknowledgement_note: string | null
}

export interface QualityRunSummary {
  total_terminal_runs: number
  conclusive_runs: number
  passed_runs: number
  failed_runs: number
  canceled_runs: number
  timed_out_runs: number
  infra_error_runs: number
  pass_rate_percent: number | null
  execution_reliability_percent: number | null
}

export interface QualityCaseSummary {
  total_terminal_cases: number
  conclusive_cases: number
  passed_cases: number
  failed_cases: number
  skipped_cases: number
  canceled_cases: number
  timed_out_cases: number
  infra_error_cases: number
  pass_rate_percent: number | null
}

export interface QualityTrendPoint {
  bucket_started_at: string
  total_terminal_runs: number
  passed_runs: number
  failed_runs: number
  canceled_runs: number
  timed_out_runs: number
  infra_error_runs: number
  pass_rate_percent: number | null
}

export interface FailureCluster {
  fingerprint: string
  failure_category: string
  message_pattern: string
  occurrences: number
  affected_runs: number
  failed_occurrences: number
  timed_out_occurrences: number
  infra_error_occurrences: number
  case_codes: string[]
  latest_at: string
}

export interface FlakyCase {
  case_id: string
  case_code: string
  conclusive_executions: number
  passed_executions: number
  failed_executions: number
  pass_rate_percent: number
  status_transitions: number
  transition_rate_percent: number
  latest_status: 'PASSED' | 'FAILED'
  latest_completed_at: string
}

export interface FlakyCaseAnalysis {
  minimum_conclusive_executions: number
  minimum_status_transitions: number
  analyzed_executions: number
  detected_cases: number
  data_truncated: boolean
  cases: FlakyCase[]
}

export interface QualityAnalyticsFilters {
  target_id: string | null
  environment_id: string | null
  baseline_id: string | null
}

export interface QualityAnalyticsQuery {
  windowDays?: number
  targetId?: string
  environmentId?: string
  baselineId?: string
}

export type QualityChangeAlertStatus = 'NO_DATA' | 'STABLE' | 'WARNING' | 'CRITICAL'

export interface QualityChangeSignal {
  metric: 'RUN_PASS_RATE' | 'CASE_PASS_RATE' | 'EXECUTION_RELIABILITY'
  current_percent: number | null
  previous_percent: number | null
  delta_percentage_points: number | null
  alert_status: QualityChangeAlertStatus
}

export interface QualityWindowComparison {
  previous_window_started_at: string
  previous_window_ended_at: string
  warning_drop_percentage_points: number
  critical_drop_percentage_points: number
  alert_status: QualityChangeAlertStatus
  signals: QualityChangeSignal[]
}

export interface QualityAnalytics {
  project_id: string
  filters: QualityAnalyticsFilters
  window_days: number
  window_started_at: string
  window_ended_at: string
  generated_at: string
  target_pass_rate_percent: number
  slo_status: 'NO_DATA' | 'MET' | 'BREACHED'
  comparison: QualityWindowComparison
  latest_completed_at: string | null
  runs: QualityRunSummary
  cases: QualityCaseSummary
  trend: QualityTrendPoint[]
  failure_clusters: FailureCluster[]
  failure_data_truncated: boolean
  flaky: FlakyCaseAnalysis
}

export interface RegressionSchedule {
  id: string
  project_id: string
  key: string
  name: string
  description: string | null
  target_id: string
  environment_id: string
  baseline_id: string
  automation_package_id: string
  case_codes: string[]
  cron_expression: string
  timezone: string
  misfire_policy: 'SKIP' | 'FIRE_ONCE'
  misfire_grace_seconds: number
  status: 'ACTIVE' | 'PAUSED' | 'ARCHIVED'
  next_fire_at: string | null
  last_scheduled_for: string | null
  last_triggered_at: string | null
  last_run_id: string | null
  last_error: string | null
  created_by: string
  created_at: string
  updated_at: string
}

export interface RegressionScheduleFiring {
  id: string
  schedule_id: string
  run_id: string | null
  scheduled_for: string
  triggered_at: string | null
  trigger_kind: 'SCHEDULED' | 'MISFIRE' | 'MANUAL'
  status: 'TRIGGERED' | 'SKIPPED' | 'BLOCKED'
  error_message: string | null
  created_at: string
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
  runner_type: 'WEB_PLAYWRIGHT'
  image_repository: string
  status: 'DRAFT' | 'ACTIVE' | 'DEPRECATED' | 'REVOKED'
  supersedes_id: string | null
  validated_run_id: string | null
  activated_by: string | null
  activated_at: string | null
  status_reason: string | null
  created_at: string
  updated_at: string
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
  regression_schedule_id: string | null
  scheduled_for: string | null
  source_run_id: string | null
  retry_mode: 'FULL' | 'FAILED_ONLY' | null
  status: RunStatus
  case_count: number
  timeout_seconds: number
  snapshot_digest: string
  result_digest: string | null
  cancel_requested: boolean
  dispatch_state: 'PENDING' | 'WAITING' | 'DISPATCHED'
  dispatch_wait_reason: string | null
  dispatched_at: string | null
  created_by: string
  created_at: string
  started_at: string | null
  timeout_at: string | null
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
