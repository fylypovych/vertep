import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { catchError } from 'rxjs/operators';
import { BaseApiService } from './base-api.service';
import { SystemStatus, HealthCheck, UpdateStatus, SystemState, SystemRolesResponse, SecretStatus, TelegramStatus, RollingUpdateRequest } from '../models';

@Injectable({ providedIn: 'root' })
export class SystemApiService {
  private h = () => this.base.headers();
  constructor(private http: HttpClient, private base: BaseApiService) {}
  status = (): Observable<SystemStatus> => this.http.get<SystemStatus>(`${this.base.url}/status`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  health = (): Observable<HealthCheck> => this.http.get<HealthCheck>(`${this.base.url}/health`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  healthHistory = (limit = 100): Observable<{ history: Array<Record<string, unknown>> }> => this.http.get<{ history: Array<Record<string, unknown>> }>(`${this.base.url}/health/history?limit=${limit}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  metrics = (): Observable<Record<string, unknown>> => this.http.get<Record<string, unknown>>(`${this.base.url}/metrics`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  securityCheck = (): Observable<{ ok: boolean; weak_or_missing: string[]; recommendation: string }> => this.http.get<{ ok: boolean; weak_or_missing: string[]; recommendation: string }>(`${this.base.url}/security/check`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  updateStatus = (): Observable<UpdateStatus> => this.http.get<UpdateStatus>(`${this.base.url}/system/update`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  checkUpdate = (): Observable<Record<string, unknown>> => this.http.post<Record<string, unknown>>(`${this.base.url}/system/update/check`, {}, { headers: this.h() }).pipe(catchError(this.base.handleError));
  installUpdate = (): Observable<Record<string, unknown>> => this.http.post<Record<string, unknown>>(`${this.base.url}/system/update/run`, {}, { headers: this.h() }).pipe(catchError(this.base.handleError));
  restart = (): Observable<Record<string, unknown>> => this.http.post<Record<string, unknown>>(`${this.base.url}/system/update/restart`, {}, { headers: this.h() }).pipe(catchError(this.base.handleError));
  readiness = (): Observable<import('../models').UpdateReadiness> => this.http.get<import('../models').UpdateReadiness>(`${this.base.url}/system/update/readiness`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  rollingStatus = (): Observable<import('../models').RollingStatus> => this.http.get<import('../models').RollingStatus>(`${this.base.url}/system/update/rolling`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  startRolling = (payload: RollingUpdateRequest): Observable<import('../models').RollingStatus> => this.http.post<import('../models').RollingStatus>(`${this.base.url}/system/update/rolling`, payload, { headers: this.h() }).pipe(catchError(this.base.handleError));
  cancelRolling = (): Observable<Record<string, unknown>> => this.http.post<Record<string, unknown>>(`${this.base.url}/system/update/rolling/cancel`, {}, { headers: this.h() }).pipe(catchError(this.base.handleError));
  promoteCanary = (): Observable<Record<string, unknown>> => this.http.post<Record<string, unknown>>(`${this.base.url}/system/update/rolling/promote`, {}, { headers: this.h() }).pipe(catchError(this.base.handleError));
  rollbackCanary = (): Observable<Record<string, unknown>> => this.http.post<Record<string, unknown>>(`${this.base.url}/system/update/rolling/rollback`, {}, { headers: this.h() }).pipe(catchError(this.base.handleError));
}
