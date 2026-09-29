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
  slot: string;
  backend: string;
  changed: boolean;
  env?: string;
  matrix: Record<string, ProviderMatrixEntry>;
}

export interface OperationState {
  operation_id: string;
  status: string;
  progress: number;
  current_phase: string;
  message?: string | null;
  error: string | null;
  result: any;
}

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

  telegramStatus(): Observable<TelegramStatus> {
    return this.http.get<TelegramStatus>(`${this.baseUrl}/telegram/status`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  telegramBotInfo(): Observable<TelegramBotInfo> {
    return this.http.get<TelegramBotInfo>(`${this.baseUrl}/telegram/bot-info`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  models(): Observable<ModelsResponse> {
    return this.http.get<ModelsResponse>(`${this.baseUrl}/system/models`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
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

  voices(): Observable<{ voices: string[] }> {
    return this.http.get<{ voices: string[] }>(`${this.baseUrl}/models/voices`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
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
}
