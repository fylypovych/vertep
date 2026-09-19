import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, catchError, throwError } from 'rxjs';

export interface LogEntry {
  timestamp: string;
  level: string;
  message: string;
  logger?: string;
  source?: string;
  job_id?: string;
  node_name?: string;
  action?: string;
  actor?: string;
  exception?: string;
  details?: Record<string, unknown>;
}

@Injectable({ providedIn: 'root' })
export class LogsApiService {
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

  logs(params?: { limit?: number; level?: string; job_id?: string; node_name?: string }): Observable<LogEntry[]> {
    const query = new URLSearchParams();
    if (params?.limit) query.set('limit', String(params.limit));
    if (params?.level) query.set('level', params.level);
    if (params?.job_id) query.set('job_id', params.job_id);
    if (params?.node_name) query.set('node_name', params.node_name);
    const qs = query.toString();
    return this.http.get<LogEntry[]>(`${this.baseUrl}/logs${qs ? `?${qs}` : ''}`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }
}
