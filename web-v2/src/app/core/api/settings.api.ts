import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, catchError, throwError } from 'rxjs';

export interface SecretStatus {
  secrets: Record<string, boolean>;
  values_exposed?: boolean;
}

export interface IntegrationStatusResponse {
  ollama: { status: string; http_status?: number; error?: string };
  comfyui: { status: string; http_status?: number; error?: string };
  publisher?: Record<string, { configured: boolean }>;
}

export interface TelegramStatus {
  configured: boolean;
  bot_username?: string;
  polling_enabled?: boolean;
  webhook_url?: string;
  allowed_chat_ids?: string;
  admin_chat_ids?: string;
}

export interface TelegramBotInfo {
  bot_username?: string;
  bot_id?: number;
  first_name?: string;
}

export interface ModelsResponse {
  models: Array<{ name: string; size: number; modified_at: string; digest: string }>;
  nodes?: ModelNodeInfo[];
}

export interface ModelNodeInfo {
  node_name: string;
  role?: string;
  status?: string;
  desired_state?: string | null;
  models?: string[];
  model_count?: number;
  catalog_at?: string | null;
  catalog_age_seconds?: number | null;
  stale: boolean;
  ready: boolean;
  pending_command: boolean;
}

/** A tag of the Ollama library confirmed against the registry (core/ollama_catalog.json). */
export interface ModelCatalogEntry {
  name: string;
  family: string;
  parameters: string;
  download_bytes: number;
}

export interface ModelCatalog {
  source: string;
  verified_at: string;
  models: ModelCatalogEntry[];
}

/** A TTS runtime voice: ``id`` is sent to synthesis, ``name`` is shown (#455). */
export interface VoiceOption { id: string; name: string }

export interface VoiceCatalog {
  voices: VoiceOption[];
  default_voice: string;
  speed_range: { min: number; max: number };
  speed_unit?: string;
}

export interface ModelPullResponse {
  operation_id: string;
  status: string;
  progress: number;
  current_phase: string;
  error: string | null;
  result: any;
}

export interface ModelDeleteResponse {
  status: string;
}

export interface ProviderMatrixEntry {
  backend: string;
  options?: string[];
  env?: string;
  configured?: boolean;
  [key: string]: unknown;
}

export interface ProviderMatrixResponse {
  matrix: Record<string, ProviderMatrixEntry>;
}

export interface ProviderSwitchResponse {
  /** RUNTIME_DEPLOYING: the local runtime was queued for the deployment executor first. */
  state?: string;
  local_runtime?: LocalEngineRuntime;
  worker_activation?: { status: string; nodes?: string[] };
  slot: string;
  backend: string;
  changed: boolean;
  env?: string;
  matrix: Record<string, ProviderMatrixEntry>;
  effective_engine?: EffectiveEngineConfig;
}

export interface EngineOption {
  id: string;
  label: string;
}

export interface EngineField {
  name: string;
  env: string | null;
  value: string | null;
  configured: boolean;
  secret: boolean;
}

export interface EffectiveEngineConfig {
  worker_activation?: { status: string; slot?: string };
  selected: string;
  effective: string;
  agree: boolean;
  label: string;
  config_revision: string;
  endpoint: string | null;
  secret: string | null;
  fields: EngineField[];
  required_fields?: Record<string, EngineField[]>;
  upstream_reference: string | null;
  ready: boolean;
  reason: string | null;
  probed: boolean;
  options: EngineOption[];
  /** The MoneyPrinterTurbo runtime shipped with Vertep (#456). */
  local_runtime?: LocalEngineRuntime;
  runtime_mode?: 'local' | 'external' | null;
  system_state: string;
  change_allowed: boolean;
  values_exposed: boolean;
}

export interface LocalEngineRuntime {
  supported: boolean;
  runtime?: string;
  endpoint?: string;
  in_plan?: boolean;
  deploying?: boolean;
  available?: boolean;
  reason?: string | null;
  deployment_state?: string | null;
  deployment_error?: string | null;
}

export interface OperationState {
  operation_id: string;
  status: string;
  progress: number;
  current_phase: string;
  model?: string;
  message?: string | null;
  error: string | null;
  result: any;
}

export interface WorkflowModeState {
  mode: 'static' | 'dynamic';
  default: 'static';
  dynamic_available: boolean;
  active_dynamic_jobs: number;
}

export interface PublishingSettings { public_url: string; telegram_channel_id: string; }

@Injectable({ providedIn: 'root' })
export class SettingsApiService {
  private baseUrl = '/api';

  constructor(private http: HttpClient) {}

  private getHeaders(): Record<string, string> {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    const csrf = document.cookie
      .split('; ')
      .find(x => x.startsWith('vertep_csrf='))
      ?.split('=')[1];
    if (csrf) {
      headers['X-CSRF-Token'] = csrf;
    }
    return headers;
  }

  private handleError(err: unknown) {
    return throwError(() => err);
  }

  secrets(): Observable<SecretStatus> {
    return this.http.get<SecretStatus>(`${this.baseUrl}/settings/secrets`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  updateSecret(name: string, value: string): Observable<SecretStatus> {
    return this.http.put<SecretStatus>(`${this.baseUrl}/settings/secrets/${encodeURIComponent(name)}`, { value }, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  deleteSecret(name: string): Observable<SecretStatus> {
    return this.http.delete<SecretStatus>(`${this.baseUrl}/settings/secrets/${encodeURIComponent(name)}`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  integrations(): Observable<IntegrationStatusResponse> {
    return this.http.get<IntegrationStatusResponse>(`${this.baseUrl}/integrations`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  reconnectPublisher(channel: string): Observable<{ channel: string; reconnected: boolean; ready: boolean; configured?: boolean; missing_scopes?: string[]; error?: string }> {
    return this.http.post<{ channel: string; reconnected: boolean; ready: boolean; configured?: boolean; missing_scopes?: string[]; error?: string }>(`${this.baseUrl}/settings/publisher/${encodeURIComponent(channel)}/reconnect`, {}, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  telegramStatus(): Observable<TelegramStatus> {
    return this.http.get<TelegramStatus>(`${this.baseUrl}/telegram/status`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  telegramBotInfo(): Observable<TelegramBotInfo> {
    return this.http.get<TelegramBotInfo>(`${this.baseUrl}/telegram/bot-info`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  models(): Observable<ModelsResponse> {
    return this.http.get<ModelsResponse>(`${this.baseUrl}/system/models`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  /** Ollama library tags available for download, separate from installed models. */
  modelCatalog(): Observable<ModelCatalog> {
    return this.http.get<ModelCatalog>(`${this.baseUrl}/system/models/catalog`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  modelNodes(): Observable<{ nodes: ModelNodeInfo[] }> {
    return this.http.get<{ nodes: ModelNodeInfo[] }>(`${this.baseUrl}/system/models/nodes`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  pullModel(name: string, node?: string): Observable<ModelPullResponse> {
    const body: Record<string, string> = { name };
    if (node) body['node'] = node;
    return this.http.post<ModelPullResponse>(`${this.baseUrl}/system/models/pull`, body, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  cancelPull(operationId: string): Observable<OperationState> {
    return this.http.post<OperationState>(`${this.baseUrl}/system/models/pull/${encodeURIComponent(operationId)}/cancel`, {}, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  operation(operationId: string): Observable<OperationState> {
    return this.http.get<OperationState>(`${this.baseUrl}/operations/${encodeURIComponent(operationId)}`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  deleteModel(name: string, node?: string): Observable<ModelDeleteResponse> {
    const params: Record<string, string> = node ? { node } : {};
    return this.http.delete<ModelDeleteResponse>(`${this.baseUrl}/system/models/${encodeURIComponent(name)}`, { params, headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  voices(): Observable<VoiceCatalog> {
    return this.http.get<VoiceCatalog>(`${this.baseUrl}/models/voices`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  previewVoice(payload: { text: string; voice?: string; speed?: number }): Observable<Blob> {
    return this.http.post(`${this.baseUrl}/models/voices/preview`, payload, {
      headers: this.getHeaders(), responseType: 'blob',
    }).pipe(catchError(this.handleError));
  }

  providers(): Observable<ProviderMatrixResponse> {
    return this.http.get<ProviderMatrixResponse>(`${this.baseUrl}/settings/providers`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  switchProvider(slot: string, backend: string, actor?: string): Observable<ProviderSwitchResponse> {
    return this.http.post<ProviderSwitchResponse>(
      `${this.baseUrl}/settings/providers/${encodeURIComponent(slot)}`,
      { backend, ...(actor ? { actor } : {}) },
      { headers: this.getHeaders() },
    ).pipe(catchError(this.handleError));
  }

  dispatchSettings(): Observable<{ worker_turn_timeout_seconds: number }> {
    return this.http
      .get<{ worker_turn_timeout_seconds: number }>(`${this.baseUrl}/settings/dispatch`, { headers: this.getHeaders() })
      .pipe(catchError(this.handleError));
  }

  saveDispatchSettings(seconds: number): Observable<{ worker_turn_timeout_seconds: number }> {
    return this.http
      .put<{ worker_turn_timeout_seconds: number }>(`${this.baseUrl}/settings/dispatch`, { worker_turn_timeout_seconds: seconds }, { headers: this.getHeaders() })
      .pipe(catchError(this.handleError));
  }

  publishingSettings(): Observable<PublishingSettings> {
    return this.http
      .get<PublishingSettings>(`${this.baseUrl}/settings/publishing`, { headers: this.getHeaders() })
      .pipe(catchError(this.handleError));
  }

  savePublishingSettings(value: PublishingSettings): Observable<PublishingSettings> {
    return this.http
      .put<PublishingSettings>(`${this.baseUrl}/settings/publishing`, value, { headers: this.getHeaders() })
      .pipe(catchError(this.handleError));
  }

  workflowMode(): Observable<WorkflowModeState> {
    return this.http
      .get<WorkflowModeState>(`${this.baseUrl}/settings/workflow-mode`, { headers: this.getHeaders() })
      .pipe(catchError(this.handleError));
  }

  setWorkflowMode(mode: 'static' | 'dynamic'): Observable<WorkflowModeState> {
    return this.http
      .post<WorkflowModeState>(`${this.baseUrl}/settings/workflow-mode`, { mode }, { headers: this.getHeaders() })
      .pipe(catchError(this.handleError));
  }

  videoEngine(probe = false): Observable<EffectiveEngineConfig> {
    return this.http
      .get<EffectiveEngineConfig>(`${this.baseUrl}/settings/video-engine`, {
        headers: this.getHeaders(),
        params: { probe },
      })
      .pipe(catchError(this.handleError));
  }

  switchVideoEngine(backend: string, endpoint?: string, actor?: string, runtime?: 'local'): Observable<ProviderSwitchResponse> {
    return this.http.post<ProviderSwitchResponse>(
      `${this.baseUrl}/settings/providers/video_engine`,
      {
        backend,
        ...(endpoint ? { endpoint } : {}),
        ...(actor ? { actor } : {}),
        ...(runtime ? { runtime } : {}),
      },
      { headers: this.getHeaders() },
    ).pipe(catchError(this.handleError));
  }
}
