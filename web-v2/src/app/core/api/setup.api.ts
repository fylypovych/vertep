import { Injectable } from '@angular/core';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { Observable } from 'rxjs';
import { catchError } from 'rxjs/operators';
import { BaseApiService } from './base-api.service';
import { SetupStatus, SetupHealth, SetupCompleteResult, SetupConfigResponse } from '../models';

/**
 * Setup domain client: first-run wizard state/health/completion.
 * All calls require the setup token header.
 */
@Injectable({ providedIn: 'root' })
export class SetupApiService {
  private h = () => this.base.headers();
  constructor(private http: HttpClient, private base: BaseApiService) {}

  private tokenHeaders(token: string): HttpHeaders {
    return this.h().set('X-Vertep-Setup-Token', token);
  }

  status(token: string): Observable<SetupStatus> {
    return this.http.get<SetupStatus>(`${this.base.url}/setup`, { headers: this.tokenHeaders(token) }).pipe(catchError(this.base.handleError));
  }

  health(token: string): Observable<SetupHealth> {
    return this.http.get<SetupHealth>(`${this.base.url}/setup/health`, { headers: this.tokenHeaders(token) }).pipe(catchError(this.base.handleError));
  }

  complete(token: string, payload: SetupCompletePayload): Observable<SetupCompleteResult> {
    return this.http.post<SetupCompleteResult>(`${this.base.url}/setup/complete`, payload, { headers: this.tokenHeaders(token) }).pipe(catchError(this.base.handleError));
  }

  config(token: string): Observable<SetupConfigResponse> {
    return this.http.get<SetupConfigResponse>(`${this.base.url}/setup/config`, { headers: this.tokenHeaders(token) }).pipe(catchError(this.base.handleError));
  }
}

/** Typed payload accepted by POST /api/setup/complete. */
export interface SetupCompletePayload {
  role?: string;
  installation_id?: string;
  config?: Record<string, unknown>;
}
