/** Domain models: nodes, jobs, storyboards, characters, brands, channels, roles, system status and workflows. */
import type { PublicationResult, SelfTestResult } from './operations';

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
  compatibility_issue?: string | null;
  runtime_status?: string;
  tested_capabilities?: string[];
  self_test_capabilities?: string[];
  last_self_test_at?: string | null;
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

export interface VideoVersionRecord {
  version: number;
  path: string;
  sha256: string;
  approved: boolean;
  approved_by?: string | null;
  approved_at?: string | null;
  revision_note?: string | null;
  created_at?: string;
}

export interface VideoRevisionRecord {
  version: number;
  text: string;
  actor: string;
  created_at?: string;
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
  video_versions?: VideoVersionRecord[];
  active_video_version?: number;
  video_revisions?: VideoRevisionRecord[];
  video_revision_upstream?: string | null;
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
  modules?: string[];
  deployment_status?: string;
  runtime_status?: string;
  runtime_evidence?: RoleRuntimeEvidence;
}

export interface RoleRuntimeEvidence {
    source?: string;
    nodes?: { node_id?: string; runtime_status?: string; heartbeat?: string; live?: boolean }[];
    local?: {
      checked_at?: string;
      checks_ok?: boolean;
      failed_checks?: string[];
      services_total?: number | null;
      services_missing?: string[];
      services_unhealthy?: string[];
    } | null;
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
  role_runtime_status?: Record<string, string>;
  node_role_evidence?: RoleRuntimeEvidence;
  deployment?: RolesDeploymentState;
  queued?: boolean;
  requested_roles?: string[] | null;
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
  system?: { state: string; reason?: string; operation_id?: string };
  telegram?: { status: string; bot_username?: string };
  queue?: { depth: number; inflight: number; dead_letter: number };
  scheduler?: { pending: number; next_run?: string };
  orchestration?: { active_jobs: number; active_scenes: number };
  resources?: {
    cpu: number; ram: number; disk: number;
    cpu_cores?: number; cpu_used_cores?: number;
    ram_used_bytes?: number; ram_total_bytes?: number;
    disk_used_bytes?: number; disk_total_bytes?: number;
  };
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
  type?: string;
  path?: string;
  valid?: boolean;
  errors?: string[];
  warnings?: string[];
  schema?: {
    node_count: number;
    node_types: string[];
    has_placeholders: string[];
    estimated_vram_mb?: number;
  };
}

export interface WorkflowValidation {
  valid: boolean;
  errors: string[];
  warnings: string[];
  schema: {
    node_count: number;
    node_types: string[];
    has_placeholders: string[];
    estimated_vram_mb?: number;
  };
}

export interface WorkflowVersion {
  version: number;
  archived_at?: string;
  deleted?: boolean;
  size_bytes?: number;
}

export interface WorkflowUsage {
  workflow: string;
  jobs: Array<{ job_id: string; topic: string; status: string }>;
  characters: Array<{ character_id: string; field: string }>;
  total_jobs: number;
  total_characters: number;
}

export interface WorkflowDocument {
  workflow: Record<string, unknown>;
  validation: WorkflowValidation;
  meta?: Record<string, unknown>;
}

export interface WorkflowFormSchema {
  kind: string;
  name: string;
  schema: {
    placeholders: string[];
    node_types: string[];
    editable_inputs: Array<{ node_id: string; class_type: string; input_name: string; current_value: unknown; type?: 'str' | 'int' | 'float' | 'bool' }>;
  };
}

export interface WorkflowDocument {
  nodes: Record<string, { class_type: string; inputs: Record<string, unknown> }>;
}
