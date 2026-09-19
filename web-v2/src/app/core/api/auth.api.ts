import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, catchError, map, throwError } from 'rxjs';

export interface SessionResponse {
  authenticated: boolean;
  user?: string;
  role?: string;
  login?: string;
  display_name?: string;
}

export interface UserProfile {
  authenticated: boolean;
  user: string;
  role: 'admin' | 'viewer';
  display_name?: string;
  login?: string;
}

export interface ChangePasswordRequest {
  old_password: string;
  new_password: string;
}

export interface ChangePasswordResponse {
  success: boolean;
  message: string;
}

export interface SetupStatus {
  configured: boolean;
  wizard_completed?: boolean;
  admin_user_exists?: boolean;
  version?: string;
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

@Injectable({ providedIn: 'root' })
export class AuthApiService {
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

  private basicAuth(login: string, password: string): string {
    return btoa(`${login}:${password}`);
  }

  getSession(): Observable<{ authenticated: boolean; user?: string; role?: string }> {
    return this.http.get<SessionResponse>(`${this.baseUrl}/session`, { headers: this.getHeaders() }).pipe(
      map(session => {
        if (session?.authenticated === false) return { authenticated: false };
        return { ...session, authenticated: true, user: session.user ?? session.login, role: session.role };
      }),
      catchError(this.handleError),
    );
  }

  createSession(payload: { login: string; password: string }): Observable<{ authenticated: boolean }> {
    return this.http.post<{ authenticated: boolean }>(`${this.baseUrl}/session`, null, {
      headers: { ...this.getHeaders(), Authorization: `Basic ${this.basicAuth(payload.login, payload.password)}` },
    }).pipe(catchError(this.handleError));
  }

  deleteSession(): Observable<{ authenticated: boolean }> {
    return this.http.delete<{ authenticated: boolean }>(`${this.baseUrl}/session`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getUserProfile(): Observable<UserProfile> {
    return this.http.get<SessionResponse>(`${this.baseUrl}/session`, { headers: this.getHeaders() }).pipe(
      map(r => ({
        authenticated: r?.authenticated ?? false,
        user: r.user ?? r.login ?? '',
        role: (r.role ?? '') as 'admin' | 'viewer',
        display_name: r.display_name,
        login: r.login,
      })),
      catchError(this.handleError),
    );
  }

  changePassword(payload: ChangePasswordRequest): Observable<ChangePasswordResponse> {
    return this.http.put<ChangePasswordResponse>(`${this.baseUrl}/session/password`, payload, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getSetupStatus(token: string): Observable<SetupStatus> {
    return this.http.get<SetupStatus>(`${this.baseUrl}/setup/status`, {
      headers: this.getHeaders(),
      params: { token },
    }).pipe(catchError(this.handleError));
  }

  getSetupHealth(token: string): Observable<SetupHealth> {
    return this.http.get<SetupHealth>(`${this.baseUrl}/setup/health`, {
      headers: this.getHeaders(),
      params: { token },
    }).pipe(catchError(this.handleError));
  }

  completeSetup(token: string, payload: Record<string, unknown>): Observable<SetupCompleteResult> {
    return this.http.post<SetupCompleteResult>(`${this.baseUrl}/setup/complete`, payload, {
      headers: this.getHeaders(),
      params: { token },
    }).pipe(catchError(this.handleError));
  }
}
