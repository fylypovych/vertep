import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { catchError } from 'rxjs/operators';
import { BaseApiService } from './base-api.service';
import {
  Alert,
  LogEntry,
  LogQuery,
  HealthCheck,
  HealthHistoryEntry,
  RuntimeMetrics,
  SecurityCheck,
  BackupListResponse,
  OperationAck,
  SystemState,
} from '../models';

/**
 * Operations domain client: observability (alerts, logs, metrics, health,
 * security) plus backup and recovery operations.
 */
@Injectable({ providedIn: 'root' })
export class OperationsApiService {
  private h = () => this.base.headers();
  constructor(private http: HttpClient, private base: BaseApiService) {}

  alerts(): Observable<Alert[]> {
    return this.http.get<Alert[]>(`${this.base.url}/alerts`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }

  logs(query: LogQuery = {}): Observable<LogEntry[]> {
    const params = new URLSearchParams();
    if (query.limit) params.set('limit', String(query.limit));
    if (query.level) params.set('level', query.level);
    if (query.job_id) params.set('job_id', query.job_id);
    if (query.node_name) params.set('node_name', query.node_name);
    const qs = params.toString();
    return this.http.get<LogEntry[]>(`${this.base.url}/logs${qs ? `?${qs}` : ''}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }

  metrics(): Observable<RuntimeMetrics> {
    return this.http.get<RuntimeMetrics>(`${this.base.url}/metrics`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }

  securityCheck(): Observable<SecurityCheck> {
    return this.http.get<SecurityCheck>(`${this.base.url}/security/check`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }

  health(): Observable<HealthCheck> {
    return this.http.get<HealthCheck>(`${this.base.url}/health`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }

  healthHistory(limit = 100): Observable<{ history: HealthHistoryEntry[] }> {
    return this.http.get<{ history: HealthHistoryEntry[] }>(`${this.base.url}/health/history?limit=${limit}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }

  backups(): Observable<BackupListResponse> {
    return this.http.get<BackupListResponse>(`${this.base.url}/system/backups`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }

  createBackup(): Observable<OperationAck> {
    return this.http.post<OperationAck>(`${this.base.url}/system/backups`, {}, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }

  restoreBackup(snapshotId: string): Observable<OperationAck> {
    return this.http.post<OperationAck>(`${this.base.url}/system/backups/${this.base.enc(snapshotId)}/restore`, {}, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }

  recoverToNormal(): Observable<SystemState> {
    return this.http.post<SystemState>(`${this.base.url}/system/recovery/normal`, {}, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }
}