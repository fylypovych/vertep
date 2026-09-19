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
}

export interface ModelPullResponse {
  status: string;
  digest?: string;
}

export interface ModelDeleteResponse {
  status: string;
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

  pullModel(name: string): Observable<ModelPullResponse> {
    return this.http.post<ModelPullResponse>(`${this.baseUrl}/system/models/pull`, { name }, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  deleteModel(name: string): Observable<ModelDeleteResponse> {
    return this.http.delete<ModelDeleteResponse>(`${this.baseUrl}/system/models/${encodeURIComponent(name)}`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }
}
