/** Operations models: alerts, logs, publications, secrets, backups, updates, queue, Telegram, health, security and setup. */
import type { PasswordPolicy } from '../session-identity';

export interface Alert {
  id?: string;
  severity: 'error' | 'warning' | 'info';
  type: string;
  message?: string;
  job_id?: string;
  node_name?: string;
  task_id?: string;
  operation_id?: string;
  updated_at?: string;
  created_at?: string;
  acknowledged_by?: string;
  resolved_at?: string;
  state?: string;
  details?: Record<string, unknown>;
}

export interface LogEntry {
  cursor?: string;
  level: string;
  message: string;
  timestamp?: string;
  logger?: string;
  job_id?: string;
  node_name?: string;
  action?: string;
  actor?: string;
  exception?: string;
  details?: Record<string, unknown>;
}

export interface PublicationResult {
  channel: string;
  status: string;
  url?: string;
  id?: string;
  error?: string;
  upload?: { mode?: string; bytes?: number; parts?: number };
  target?: string;
}

export interface SecretStatus {
  secrets: Record<string, boolean>;
  values_exposed: boolean;
}

export interface IntegrationStatus {
  ollama: { status: string; http_status?: number; error?: string };
  comfyui: { status: string; http_status?: number; error?: string };
  publisher?: Record<string, {
    configured: boolean;
    ready?: boolean;
    required_scopes?: string[];
    missing_scopes?: string[];
  }>;
}

export interface ModelInfo {
  name: string;
  size?: number;
  details?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface BackupInfo {
  snapshot_id: string;
  created_at?: string;
  size_bytes?: number;
  state?: string;
  description?: string;
  inventory?: Array<{ label: string; path: string }>;
  retention_days?: number;
  max_snapshots?: number;
  remote_copy?: boolean;
  [key: string]: unknown;
}

export interface BackupListResponse {
  snapshots: BackupInfo[];
}

export interface UpdateReadiness {
  ready: boolean;
  active_jobs: string[];
  busy_workers: string[];
  queue_paused: boolean;
  inflight: number;
  drain_operation_id: string;
  acknowledged_workers: string[];
  unacknowledged_workers: string[];
}

export interface RollingStatus {
  current_batch?: number;
  total_batches?: number;
  canary?: { promoted?: boolean; rolled_back?: boolean };
  operation_id?: string;
  state?: string;
  [key: string]: unknown;
}

export interface QueueTask {
  job_id: string;
  task: string;
  priority: number;
  min_vram_mb: number;
  workflow?: string;
  topic: string;
  script?: Record<string, unknown>;
  task_id: string;
  scene_id: string;
  not_before?: string;
  enqueued_at: string;
}

export interface DeadLetterTask extends QueueTask {
  error: string;
  failed_at: string;
}

export interface NodeActionPayload {
  action: 'drain' | 'resume' | 'quarantine' | 'unquarantine' | 'self-test' | 'disable' | 'enable' | 'restart' | 'logs' | 'update';
  reason?: string;
}

export interface RollingUpdateRequest {
  target_version: string;
  node_ids?: string[];
  order?: 'workers-first' | 'core-first' | 'custom';
  update_timeout_seconds?: number;
  canary?: boolean;
}

export interface TelegramStatus {
  configured: boolean;
  webhook_url?: string;
  public_url?: string;
  webhook_secret_configured?: boolean;
  allowed_chat_ids?: string;
  admin_chat_ids?: string;
  polling_enabled?: boolean;
  polling_status?: string;
  bot_username?: string;
  last_update_id?: number;
  last_message_at?: string;
}

export interface TelegramBotInfo {
  first_name?: string;
  last_name?: string;
  username?: string;
  id?: number;
  is_bot?: boolean;
}

export interface UpdateStatus {
  state: 'IDLE' | 'PENDING' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'ROLLED_BACK';
  phase?: string;
  action?: string;
  message: string;
  updated_at?: string;
  current_version: string;
  available_version?: string;
  update_available?: boolean;
  request_id?: string;
  progress: number;
  log: string[];
  enabled: boolean;
  pending: number;
}

export interface SystemState {
  state: 'NORMAL' | 'MAINTENANCE' | 'UPDATING' | 'RECOVERING' | 'READ_ONLY' | 'EMERGENCY';
  updated_at?: string;
  reason?: string;
  operation_id?: string;
}

export interface HealthCheck {
  status: string;
  service: string;
  jobs: number;
  checks: Record<string, unknown>;
}

export interface RegistrationTokenResponse {
  token: string;
  role: string;
  expires_at: string;
  push_token: boolean;
}

export interface NodeRegisterRequest {
  registration_token: string;
  node_id: string;
  capabilities: string[];
  hardware: Record<string, unknown>;
  version: string;
  csr: string;
}

export interface NodeRegisterResponse {
  worker_id: string;
  role: string;
  status: string;
  jwt: string;
  worker_secret: string;
  certificate: string;
  core_certificate: string;
  configuration: {
    capabilities: string[];
    heartbeat_seconds: number;
  };
}

export interface ArtifactVerifyResponse {
  job_id: string;
  valid: boolean;
  results: Array<{
    artifact_id: string;
    valid: boolean;
    error?: string;
  }>;
}

export interface QueueTaskSummary {
  task_id: string;
  job_id: string;
  task: string;
  priority: number;
  scene_id?: string;
  enqueued_at?: number;
  workflow?: string;
}

export interface QueueState {
  ready: QueueTaskSummary[];
  inflight: QueueTaskSummary[];
}

export interface HealthHistoryEntry {
  timestamp: string;
  role: string;
  status: string;
  checks: Record<string, unknown>;
}

export interface ScheduledJob {
  job_id: string;
  topic: string;
  character_id: string;
  scheduled_for: string;
  status: string;
  created_at: string;
  priority: number;
}

export interface RuntimeMetrics {
  gpu_available: boolean;
  gpu_name?: string;
  gpu_profile?: string;
  gpu_architecture?: string;
  compute_capability?: string;
  driver_version?: string;
  vram_total_mb?: number;
  vram_free_mb?: number;
  ram_total_mb?: number;
  ram_free_mb?: number;
  cpu_count?: number;
  cpu_load?: number;
  disk_free_mb?: number;
  temperature?: number;
}

export interface SelfTestResult {
  status: string;
  passed?: boolean;
  duration_seconds?: number;
  error?: string;
  details?: Record<string, unknown>;
}

export interface UserProfile {
  user: string;
  login: string;
  role: 'admin' | 'viewer';
  display_name: string | null;
  email: string | null;
  password_policy: PasswordPolicy;
}

export interface UpdateProfileRequest {
  display_name?: string;
  email?: string;
}

export interface ChangePasswordRequest {
  old_password: string;
  new_password: string;
}

export interface ChangePasswordResponse {
  ok: boolean;
  message: string;
  password_policy?: PasswordPolicy;
}

export interface InstallationManifest {
  installation_id: string;
  version: string;
  role: string;
  node_id?: string;
  created_at: string;
  hardware?: Record<string, unknown>;
}

export interface CertificateStatus {
  serial: string;
  issuer: string;
  subject: string;
  not_before: string;
  not_after: string;
  fingerprint: string;
}

export interface SecuritySecretStore {
  status: string;
  sealed: boolean | null;
  detail: string;
  unsealable?: boolean | null;
  passphrase_configured: boolean;
  sealing_required: boolean;
}

export interface SecurityCertificateStatus {
  present: boolean;
  status: string;
  sha256?: string;
  size_bytes?: number;
  expires_at?: string;
  days_remaining?: number;
  subject?: string;
}

export interface SecurityIntegrationStatus {
  name: string;
  status: string;
}

/** The administrator account as authentication uses it (#457); never a password or hash. */
export interface AdminCredentials {
  state: 'configured' | 'invalid' | 'missing';
  sources: Array<'first_run' | 'user_store' | 'environment'>;
  problems: string[];
}

export interface SecurityEffectiveConfig {
  config_root?: string;
  admin_password_set?: boolean;
  admin_credentials?: AdminCredentials;
  node_api_token_set?: boolean;
  postgres_password_set?: boolean;
  secret_store_passphrase_set?: boolean;
  session_secret_set?: boolean;
  llm_provider?: string;
  tts_provider?: string;
  publisher_mock?: boolean;
}

export interface SecurityCheck {
  ok: boolean;
  weak_or_missing: string[];
  recommendation: string;
  checks?: {
    secrets_store: SecuritySecretStore;
    certificates: Record<string, SecurityCertificateStatus>;
    integrations: SecurityIntegrationStatus[];
    effective_config?: SecurityEffectiveConfig;
  };
}

export interface BackupSnapshot {
  snapshot_id: string;
  created_at?: string;
  size_bytes?: number;
  state?: string;
  description?: string;
  type?: string;
}

export interface UpdateOperation {
  state: string;
  phase?: string;
  message?: string;
  started_at?: string;
  completed_at?: string;
  error?: string;
}

export interface SetupStatus {
  selected_additional_roles?: string[];
  configured: boolean;
  installation: string | null;
  hardware: Record<string, unknown>;
  backends: string[];
  selected_role: string | null;
  roles: Record<string, { label: string; modules: string[]; capabilities: string[] }>;
}

export interface SetupHealth {
  ready: boolean;
  checks: Record<string, string>;
}

export interface SetupCompleteResult {
  installation_id: string;
  core_url?: string;
  core_certificate?: string;
  registration_token?: string;
}

export interface HealthHistoryEntry {
  timestamp: string;
  role: string;
  status: string;
  checks: Record<string, unknown>;
}
