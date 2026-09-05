export interface Worker {
  node_id: string;
  node_name: string;
  role: string;
  status: string;
  capabilities: string[];
  load?: number;
  vram_mb?: number;
  ram_mb?: number;
  uptime?: string;
  gpu_name?: string;
}

export interface Job {
  id: string;
  status: string;
  title?: string;
  created_at?: string;
  updated_at?: string;
}

export interface Character {
  id: string;
  name: string;
  language: string;
  enabled: boolean;
  system_prompt?: string;
  voice?: { provider: string; voice: string };
  visual?: { style: string; aspect_ratio: string };
  generation?: { workflow: string; min_vram_mb: number };
  publishing?: { enabled: boolean };
}

export interface SystemStatus {
  core: string;
  postgres: string;
  redis: string;
  storage: string;
  system?: { state: string; reason?: string };
  telegram?: { status: string; bot_username?: string };
  queue?: { depth: number; inflight: number; dead_letter: number };
  scheduler?: { pending: number; next_run?: string };
  orchestration?: { active_jobs: number; active_scenes: number };
  resources?: { cpu: number; ram: number; disk: number };
}
