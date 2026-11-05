import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { catchError } from 'rxjs/operators';
import { BaseApiService } from './base-api.service';
import { SystemStatus, HealthCheck, UpdateStatus, SystemState, SystemRolesResponse, SecretStatus, TelegramStatus, RollingUpdateRequest, MetricsResponse, OperationAck, SystemStateDTO, HealthHistoryResponse, SecurityCheckResponse } from '../models';

export interface PostSetupState { pending: boolean; telegram: boolean; backup_key: boolean; }

@Injectable({ providedIn: 'root' })
export class SystemApiService {
  private h = () => this.base.headers();
  constructor(private http: HttpClient, private base: BaseApiService) {}
  postSetup = (): Observable<PostSetupState> => this.http.get<PostSetupState>(`${this.base.url}/system/post-setup`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  backupKey = (): Observable<Blob> => this.http.post(`${this.base.url}/system/post-setup/backup-key`, null, { headers: this.h(), responseType: 'blob' }).pipe(catchError(this.base.handleError));
  acknowledgeBackupKey = (): Observable<PostSetupState> => this.http.post<PostSetupState>(`${this.base.url}/system/post-setup/backup-key/saved`, null, { headers: this.h() }).pipe(catchError(this.base.handleError));
  saveTelegramAccess = (allowed: string, admins: string): Observable<unknown> => this.http.post(`${this.base.url}/telegram/setup`, { allowed_chat_ids: allowed, admin_chat_ids: admins }, { headers: this.h() }).pipe(catchError(this.base.handleError));
  status = (): Observable<SystemStatus> => this.http.get<SystemStatus>(`${this.base.url}/status`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  health = (): Observable<HealthCheck> => this.http.get<HealthCheck>(`${this.base.url}/health`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  healthHistory = (limit = 100): Observable<HealthHistoryResponse> => this.http.get<HealthHistoryResponse>(`${this.base.url}/health/history?limit=${limit}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  metrics = (): Observable<MetricsResponse> => this.http.get<MetricsResponse>(`${this.base.url}/metrics`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  securityCheck = (): Observable<SecurityCheckResponse> => this.http.get<SecurityCheckResponse>(`${this.base.url}/security/check`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  updateStatus = (): Observable<UpdateStatus> => this.http.get<UpdateStatus>(`${this.base.url}/system/update`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  checkUpdate = (): Observable<OperationAck> => this.http.post<OperationAck>(`${this.base.url}/system/update/check`, {}, { headers: this.h() }).pipe(catchError(this.base.handleError));
  installUpdate = (): Observable<OperationAck> => this.http.post<OperationAck>(`${this.base.url}/system/update/run`, {}, { headers: this.h() }).pipe(catchError(this.base.handleError));
  restart = (): Observable<SystemStateDTO> => this.http.post<SystemStateDTO>(`${this.base.url}/system/update/restart`, {}, { headers: this.h() }).pipe(catchError(this.base.handleError));
  readiness = (): Observable<import('../models').UpdateReadiness> => this.http.get<import('../models').UpdateReadiness>(`${this.base.url}/system/update/readiness`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  rollingStatus = (): Observable<import('../models').RollingStatus> => this.http.get<import('../models').RollingStatus>(`${this.base.url}/system/update/rolling`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  startRolling = (payload: RollingUpdateRequest): Observable<import('../models').RollingStatus> => this.http.post<import('../models').RollingStatus>(`${this.base.url}/system/update/rolling`, payload, { headers: this.h() }).pipe(catchError(this.base.handleError));
  cancelRolling = (): Observable<OperationAck> => this.http.post<OperationAck>(`${this.base.url}/system/update/rolling/cancel`, {}, { headers: this.h() }).pipe(catchError(this.base.handleError));
  promoteCanary = (): Observable<OperationAck> => this.http.post<OperationAck>(`${this.base.url}/system/update/rolling/promote`, {}, { headers: this.h() }).pipe(catchError(this.base.handleError));
  rollbackCanary = (): Observable<OperationAck> => this.http.post<OperationAck>(`${this.base.url}/system/update/rolling/rollback`, {}, { headers: this.h() }).pipe(catchError(this.base.handleError));
  recoverToNormal = (): Observable<SystemState> => this.http.post<SystemState>(`${this.base.url}/system/recovery/normal`, {}, { headers: this.h() }).pipe(catchError(this.base.handleError));
}
