import { Injectable } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Observable, catchError, map, throwError } from 'rxjs';
import { basicCredentials, requireSessionIdentity, resolvePasswordPolicy, resolveSessionIdentity } from '../session-identity';
import type {
  ChangePasswordRequest,
  ChangePasswordResponse,
  SessionEnvelope,
  UpdateProfileRequest,
  UserProfile,
} from '../models';

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
    // Issue #75 S3: the UI shows the server's own reason, not a generic HTTP line.
    if (err instanceof HttpErrorResponse) {
      const detail = (err.error as { detail?: unknown } | null)?.detail;
      const message = typeof detail === 'string' && detail.trim() ? detail : undefined;
      return throwError(() => new Error(message ?? err.message));
    }
    return throwError(() => err);
  }

  private basicAuth(login: string, password: string): string {
    return basicCredentials(login, password);
  }

  getSession(): Observable<{ authenticated: boolean; user?: string; role?: string }> {
    return this.http.get<SessionEnvelope>(`${this.baseUrl}/session`, { headers: this.getHeaders() }).pipe(
      // Issue #75 S1: only a complete, server-validated identity counts as signed in.
      map((session) => {
        const identity = resolveSessionIdentity(session);
        return identity
          ? { authenticated: true, user: identity.user, role: identity.role }
          : { authenticated: false };
      }),
      catchError(this.handleError),
    );
  }

  createSession(payload: { login: string; password: string }): Observable<{ authenticated: boolean }> {
    return this.http.post<SessionEnvelope>(`${this.baseUrl}/session`, null, {
      headers: { ...this.getHeaders(), Authorization: `Basic ${this.basicAuth(payload.login, payload.password)}` },
    }).pipe(
      map((session) => ({ authenticated: resolveSessionIdentity(session) !== null })),
      catchError(this.handleError),
    );
  }

  deleteSession(): Observable<{ authenticated: boolean }> {
    return this.http.delete<{ authenticated: boolean }>(`${this.baseUrl}/session`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getUserProfile(): Observable<UserProfile> {
    return this.http.get<SessionEnvelope>(`${this.baseUrl}/session`, { headers: this.getHeaders() }).pipe(
      // Throws for a missing/unknown role so guards, header and profile agree.
      map((session) => {
        const identity = requireSessionIdentity(session);
        return {
          ...identity,
          password_policy: resolvePasswordPolicy(session.password_policy),
        };
      }),
      catchError(this.handleError),
    );
  }

  updateProfile(payload: UpdateProfileRequest): Observable<UserProfile> {
    return this.http.put<SessionEnvelope>(`${this.baseUrl}/session/profile`, payload, { headers: this.getHeaders() }).pipe(
      map((session) => ({
        ...requireSessionIdentity({ authenticated: true, ...session }),
        password_policy: resolvePasswordPolicy(session.password_policy),
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
