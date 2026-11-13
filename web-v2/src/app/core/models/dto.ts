/** Shared auth/ack contracts, typed action requests and response DTOs of the remaining endpoints. */
import type { SessionEnvelope } from '../session-identity';
import type { HealthHistoryEntry } from './operations';

// ── Shared auth / ack contracts ─────────────────────────────────
export type SessionResponse = SessionEnvelope;

export interface OperationAck {
  ok?: boolean;
  message?: string;
  operation_id?: string;
  error?: string;
  status?: string;
  confirmation_token?: string;
  snapshot_id?: string;
  sha256?: string;
}

export interface RestoreProgress {
  status: string;
  progress: number;
  message: string;
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
