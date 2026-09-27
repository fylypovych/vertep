import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { catchError, map } from 'rxjs/operators';
import { BaseApiService } from './base-api.service';
import { basicCredentials, requireSessionIdentity, resolvePasswordPolicy } from '../session-identity';
import { SessionResponse, UserProfile, ChangePasswordRequest, ChangePasswordResponse } from '../models';

/**
 * Session/Auth domain client: login, logout, current session/profile,
 * password change, and (system roles live on the system domain).
 */
@Injectable({ providedIn: 'root' })
export class SessionApiService {
  private h = () => this.base.headers();
  constructor(private http: HttpClient, private base: BaseApiService) {}

  get(): Observable<SessionResponse> {
    return this.http.get<SessionResponse>(`${this.base.url}/session`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }

  create(payload: { login: string; password: string }): Observable<SessionResponse> {
    // Backend очікує Basic Authorization (authorization header), а не JSON body.
    const creds = basicCredentials(payload.login, payload.password);
    return this.http.post<SessionResponse>(`${this.base.url}/session`, null, {
      headers: this.h().set('Authorization', `Basic ${creds}`),
    }).pipe(catchError(this.base.handleError));
  }

  delete(): Observable<SessionResponse> {
    return this.http.delete<SessionResponse>(`${this.base.url}/session`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }

  profile(): Observable<UserProfile> {
    return this.http.get<SessionResponse>(`${this.base.url}/session`, { headers: this.h() }).pipe(
      // Issue #75 S1: the same identity contract as the AuthApiService facade.
      // A missing or unknown role is rejected instead of becoming `admin`.
      map((session) => ({
        ...requireSessionIdentity(session),
        password_policy: resolvePasswordPolicy(session.password_policy),
      })),
      catchError(this.base.handleError),
    );
  }

  changePassword(payload: ChangePasswordRequest): Observable<ChangePasswordResponse> {
    return this.http.put<ChangePasswordResponse>(`${this.base.url}/session/password`, payload, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }
}
