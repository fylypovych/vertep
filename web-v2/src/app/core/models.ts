export interface Worker {
  node_id: string;
  node_name: string;
  role: string;
  status: string;
  capabilities: string[];
  vram_mb?: number;
  ram_mb?: number;
  free_vram_mb?: number;
  gpu_name?: string;
  gpu_load?: number;
  cpu_load?: number;
  temperature?: number;
  version?: string;
  runtime_version?: string;
  current_job?: string;
  current_task?: string;
  supported_tasks?: string[];
  supported_workflows?: string[];
  runtime_status?: string;
  tested_capabilities?: string[];
  disk_free_mb?: number;
  modules?: string[];
  services?: string[];
  capability_backends?: Record<string, string>;
  certificate_serial?: string;
  certificate_expires_at?: string;
  registered_at?: string;
  revoked_at?: string | null;
  update_state?: NodeUpdateState;
}

export interface NodeUpdateState {
  desired_state?: string;
  update_target_version?: string;
  rollback_target_version?: string;
  self_test_requested_at?: string | null;
}

export interface WorkerHardware {
  gpu_arch?: string;
  gpu_count?: number;
  cpu_count?: number;
  ram_total_mb?: number;
  hostname?: string;
  os?: string;
  [key: string]: unknown;
}

export interface WorkerRuntime {
  last_seen?: string;
  status?: string;
  gpu_name?: string;
  vram_mb?: number;
  free_vram_mb?: number;
  gpu_load?: number;
  cpu_load?: number;
  temperature?: number;
  ram_mb?: number;
  disk_free_mb?: number;
  current_task?: string;
  current_job?: string;
  self_test?: SelfTestResult;
  capabilities?: string[];
  tested_capabilities?: string[];
  version?: string;
  runtime_version?: string;
  [key: string]: unknown;
}

export interface NodeDetail extends Worker {
  hardware: WorkerHardware;
  certificate_serial?: string;
  certificate_expires_at?: string;
  credential_generation?: number;
  registered_at?: string;
  revoked_at?: string | null;
  runtime?: WorkerRuntime;
  self_test?: SelfTestResult;
  update_state: NodeUpdateState;
}

export interface AttemptRecord {
  attempt: number;
  status: string;
  started_at: string;
  completed_at?: string;
  node_name?: string;
  error?: string;
}

export interface StageRecord {
  name: string;
  status: string;
  attempts: AttemptRecord[];
  started_at?: string;
  completed_at?: string;
}

export interface SceneRecord {
  scene_id: string;
  index: number;
  prompt: string;
  video_prompt?: string;
  voiceover?: string;
  duration?: number;
  status: string;
  task_id?: string;
  assigned_worker?: string;
  artifact_ids: string[];
  attempts: AttemptRecord[];
}

export interface ArtifactRecord {
  artifact_id: string;
  name: string;
  kind: string;
  size: number;
  mime_type: string;
  valid: boolean;
  path?: string;
  url?: string;
  filename?: string;
  created_at?: string;
  sha256?: string;
  scene_id?: string;
  task_id?: string;
  node_name?: string;
  workflow?: string;
}

export interface Job {
  job_id: string;
  topic: string;
  character_id: string;
  priority: number;
  status: string;
  created_at: string;
  source: string;
  retries: number;
  approved: boolean;
  approved_channels: string[];
  approval_status: string;
  published_to: string[];
  publication_results: Record<string, PublicationResult>;
  task_type: string;
  min_vram_mb: number;
  max_retries: number;
  brand_id: string;
  aspect_ratio: string;
  output_preset: string;
  version: number;
  stages: Record<string, StageRecord>;
  scenes: SceneRecord[];
  artifacts: ArtifactRecord[];
  active_task_ids: Record<string, string>;
  completed_task_ids: string[];
  script?: Record<string, unknown>;
  events: string[];
  event_log?: JobEvent[];
  output_path?: string;
  assigned_worker?: string;
  workflow?: string;
  active_task_id?: string;
  scheduled_for?: string;
  storyboards?: StoryboardVersion[];
  active_storyboard_version?: number;
  active_image_version?: number;
}

export interface JobEvent {
  timestamp: string;
  type: string;
  message: string;
  state?: string;
  attempt?: number;
  node?: string;
  task_id?: string;
  artifact_id?: string;
  error?: string;
}

export interface JobListQuery {
  statusGroup?: string;
  search?: string;
  page?: number;
  perPage?: number;
}

export interface JobPage {
  items: Job[];
  total: number;
  page: number;
  per_page: number;
  pages: number;
  has_more: boolean;
}

export interface StoryboardScene {
  index: number;
  prompt: string;
  video_prompt: string;
  voiceover: string;
  duration: number;
  scene_id?: string;
  artifact_id?: string;
  image_prompt?: string;
  image_artifact_id?: string;
  image_version?: number;
}

export interface StoryboardVersion {
  version: number;
  title: string;
  description: string;
  hashtags: string[];
  scenes: StoryboardScene[];
  status: 'pending_approval' | 'approved' | 'rejected' | 'superseded';
  revision_request?: string;
  created_at: string;
  decided_at?: string;
  decided_by?: string;
  image_version: number;
  image_status: 'pending' | 'generating' | 'ready' | 'approved' | 'superseded';
}

export interface CharacterVoice {
  provider?: string;
  voice?: string;
  language?: string;
  model?: string;
  engine?: string;
  speed?: number;
  [key: string]: unknown;
}

export interface CharacterVisual {
  style?: string;
  aspect_ratio?: string;
  output_preset?: string;
  [key: string]: unknown;
}

export interface CharacterGeneration {
  workflow?: string;
  min_vram_mb?: number;
  max_retries?: number;
  [key: string]: unknown;
}

export interface CharacterPublishing {
  enabled?: boolean;
  channels?: string;
  [key: string]: unknown;
}

export interface CharacterForm {
  id?: string;
  name: string;
  language: string;
  enabled: boolean;
  system_prompt: string;
  voice: CharacterVoice;
  visual: CharacterVisual;
  generation: CharacterGeneration;
  publishing: CharacterPublishing;
  workflow?: string;
}

export interface Character {
  id?: string;
  name: string;
  language: string;
  enabled: boolean;
  system_prompt: string;
  voice: CharacterVoice;
  visual: CharacterVisual;
  generation: CharacterGeneration;
  publishing: CharacterPublishing;
  workflow?: string;
}

export type ChannelType = string;

export interface Brand {
  id: string;
  name: string;
  enabled: boolean;
  metadata: Record<string, unknown>;
  publishing: Record<string, unknown>;
}

export interface Channel {
  channel_id: string;
  brand_id: string;
  channel_type: string;
  target: string;
  enabled: boolean;
  created_at: string;
  metadata: Record<string, unknown>;
}

export interface SystemRole {
  id: string;
  label: string;
  count?: number;
  services?: string[];
  capabilities?: string[];
  deployment_status?: string;
}

export interface RolesDeploymentState {
  state?: string;
  error?: string;
  started_at?: string;
  completed_at?: string;
  services?: string[];
  health?: Record<string, string>;
  [key: string]: unknown;
}

export interface RolesUpdateResponse {
  state: string;
  active_roles?: string[];
  requested_roles?: string[];
  message?: string;
}

export interface SystemRolesResponse {
  node_role: string;
  active_roles: string[];
  available_roles: SystemRole[];
  deployment?: RolesDeploymentState;
  queued?: boolean;
}

export interface ProviderSlot {
  backend: string;
  options?: string[];
  env?: string;
  configured?: boolean;
  platforms?: Record<string, { configured: boolean }>;
}

export interface SystemStatus {
  core: string;
  postgres: string;
  redis: string;
  storage: string;
  version?: string;
  system?: { state: string; reason?: string };
  telegram?: { status: string; bot_username?: string };
  queue?: { depth: number; inflight: number; dead_letter: number };
  scheduler?: { pending: number; next_run?: string };
  orchestration?: { active_jobs: number; active_scenes: number };
  resources?: { cpu: number; ram: number; disk: number };
  providers?: Record<string, ProviderSlot>;
  ollama?: string;
  update?: Record<string, unknown>;
  workers?: Worker[];
  checks?: Record<string, [boolean, string]>;
}

export interface JobCreate {
  topic: string;
  character_id?: string;
  priority?: number;
  source?: string;
  task_type?: string;
  min_vram_mb?: number;
  brand_id?: string;
  workflow?: string;
  aspect_ratio?: string;
  output_preset?: string;
  scheduled_for?: string;
}

export interface JobUpdate {
  expected_version?: number;
  script?: Record<string, unknown>;
  prompt?: string;
  character_id?: string;
  priority?: number;
  workflow?: string;
  topic?: string;
}

export interface Workflow {
  kind: string;
  name: string;
}

export interface Alert {
  severity: 'error' | 'warning' | 'info';
  type: string;
  message?: string;
  job_id?: string;
  node_name?: string;
  task_id?: string;
  operation_id?: string;
  updated_at?: string;
  state?: string;
  details?: Record<string, unknown>;
}

export interface LogEntry {
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
  publisher?: Record<string, { configured: boolean }>;
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
  state: 'IDLE' | 'PENDING' | 'RUNNING' | 'FAILED' | 'ROLLED_BACK';
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
  checks: Record<string, [boolean, string]>;
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
  checks: Record<string, [boolean, string]>;
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
  role: 'admin' | 'viewer';
}

export interface ChangePasswordRequest {
  old_password: string;
  new_password: string;
}

export interface ChangePasswordResponse {
  ok: boolean;
  message: string;
}

export interface WorkflowDocument {
  nodes: Record<string, { class_type: string; inputs: Record<string, unknown> }>;
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

export interface SecurityCheck {
  ok: boolean;
  weak_or_missing: string[];
  recommendation: string;
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

// ── Shared auth / ack contracts ─────────────────────────────────
export interface SessionResponse {
  authenticated: boolean;
  user?: string;
  role?: 'admin' | 'viewer';
}

export interface OperationAck {
  ok?: boolean;
  message?: string;
  operation_id?: string;
  error?: string;
}

export interface LogQuery {
  limit?: number;
  level?: string;
  job_id?: string;
  node_name?: string;
}

// ── Domain action request contracts (typed, no loose maps) ───────
export interface StoryboardActionRequest {
  version: number;
  image_version?: number;
  actor?: string;
  revision?: string;
  scene_indexes?: number[];
}

export interface PublishRequest {
  channels?: string[];
}

export interface WizardState {
  role: string;
  core_url?: string;
  core_certificate?: string;
  registration_token?: string;
}

// ── Typed DTOs for remaining Record<string, unknown> endpoints ───
export interface MetricsResponse {
  jobs_total: number;
  jobs_by_status: Record<string, number>;
  queue_ready: number;
  queue_inflight: number;
  queue_dead_letter: number;
  jobs_scheduled: number;
  scenes_by_status: Record<string, number>;
  workers_online: number;
}

export interface WorkerHealthItem {
  node_id: string;
  node_name: string;
  checks: Record<string, unknown>;
}

export interface WorkersHealthResponse {
  status: string;
  workers: WorkerHealthItem[];
}

export interface NodeSystemStatusResponse {
  node: string;
  status: string;
}

export interface HealthHistoryEntry {
  timestamp: string;
  role: string;
  status: string;
  checks: Record<string, [boolean, string]>;
}

export interface HealthHistoryResponse {
  history: HealthHistoryEntry[];
}

export interface SecurityCheckResponse {
  ok: boolean;
  weak_or_missing: string[];
  recommendation: string;
}

export interface IntegrationInfo {
  name: string;
  status: string;
  configured?: boolean;
}

export interface IntegrationStatusResponse {
  ollama: { status: string; http_status?: number; error?: string };
  comfyui: { status: string; http_status?: number; error?: string };
  publisher?: Record<string, { configured: boolean }>;
}

export interface BackupSnapshotItem {
  snapshot_id: string;
  created_at?: string;
  size_bytes?: number;
  state?: string;
  description?: string;
}

export interface BackupListResponseDTO {
  backups: BackupSnapshotItem[];
}

export interface BackupCreateResponse {
  job: string;
  detail?: string;
}

export interface BackupRestoreResponse {
  snapshot_id: string;
  status: string;
}

export interface ModelInfoDTO {
  name: string;
  size?: number;
  details?: Record<string, unknown>;
}

export interface ModelsResponse {
  models: ModelInfoDTO[] | string;
}

export interface ModelPullResponse {
  model: string;
  detail?: string;
}

export interface ModelDeleteResponse {
  model_name: string;
  status: string;
}

export interface CertificateInfoDTO {
  status: string;
  present?: boolean;
  expires_at?: string;
  days_remaining?: number;
}

export interface CertificatesResponse {
  certificates: Record<string, CertificateInfoDTO> | string;
}

export interface CertificateRenewResponse {
  certificate: string;
  detail?: string;
}

export interface SystemStateDTO {
  state: string;
  updated_at?: string;
  reason?: string;
  operation_id?: string;
}

export interface LicenseResponse {
  license: string | Record<string, unknown>;
}

export interface RollingCancelResponse {
  status: string;
}

export interface RollingPromoteResponse {
  status: string;
}

export interface RollingRollbackResponse {
  status: string;
}

export interface NodeActionResponse {
  accepted?: boolean;
  workers?: number;
  desired_state?: string;
  self_test_requested_at?: string | null;
  update_target_version?: string | null;
  rollback_target_version?: string | null;
}

export interface NodeRevokeResponse {
  node_id: string;
  status: string;
  revoked_at?: string;
}

export interface BrandCreateDTO {
  id: string;
  name: string;
  metadata?: Record<string, unknown>;
  publishing?: Record<string, unknown>;
}

export interface BrandUpdateDTO {
  name?: string;
  enabled?: boolean;
  metadata?: Record<string, unknown>;
  publishing?: Record<string, unknown>;
}

export interface ChannelCreateDTO {
  brand_id: string;
  channel_type: string;
  target: string;
  enabled?: boolean;
  metadata?: Record<string, unknown>;
}

export interface ChannelUpdateDTO {
  target?: string;
  enabled?: boolean;
  metadata?: Record<string, unknown>;
}

export interface WorkflowSaveResponse {
  saved: boolean;
  kind: string;
  name: string;
}

export interface WorkflowDeleteResponse {
  deleted: boolean;
  kind: string;
  name: string;
}

export interface SetupConfigResponse {
  config?: Record<string, unknown>;
}

export interface RouteMetadata {
  title?: string;
  icon?: string;
  adminOnly?: boolean;
  roles?: string[]; // ['admin', 'viewer'] or similar
  exact?: boolean;
  [key: string]: unknown; // Allow custom metadata
}
