import { Injectable, Inject } from '@angular/core';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { Observable, throwError } from 'rxjs';
import { catchError, map } from 'rxjs/operators';
import {
  Worker,
  Job,
  Character,
  SystemStatus,
  Brand,
  Channel,
  SystemRole,
  SystemRolesResponse,
  RolesUpdateResponse,
  JobCreate,
  JobUpdate,
  ArtifactVerifyResponse,
  ArtifactRecord,
  RegistrationTokenResponse,
  NodeRegisterRequest,
  NodeRegisterResponse,
  NodeActionPayload,
  NodeDetail,
  RollingUpdateRequest,
  RollingStatus,
  UpdateReadiness,
  HealthCheck,
  UpdateStatus,
  SystemState,
  TelegramStatus,
  Alert,
  LogEntry,
  SecretStatus,
  Workflow,
  DeadLetterTask,
  QueueState,
  SetupStatus,
  SetupHealth,
  SetupCompleteResult,
} from './models';

@Injectable()
export class VertepApiService {
  private baseUrl = '/api';

  constructor(@Inject(HttpClient) private http: HttpClient) {}

  private getHeaders(): HttpHeaders {
    let headers = new HttpHeaders({ 'Content-Type': 'application/json' });
    const csrf = document.cookie
      .split('; ')
      .find(x => x.startsWith('vertep_csrf='))
      ?.split('=')[1];
    if (csrf) {
      headers = headers.set('X-CSRF-Token', csrf);
    }
    return headers;
  }

  getStatus(): Observable<SystemStatus> {
    return this.http.get<SystemStatus>(`${this.baseUrl}/status`, { headers: this.getHeaders() }).pipe(
      catchError((err) => throwError(() => err)),
      map((status) => ({
        ...status,
        workers: Array.isArray(status.workers) ? status.workers : [],
      })),
      catchError((err) => throwError(() => err)),
    );
  }

  getWorkers(): Observable<Worker[]> {
    return this.http.get<Worker[]>(`${this.baseUrl}/workers`, { headers: this.getHeaders() }).pipe(
      catchError((err) => throwError(() => err)),
    );
  }

  getJobs(): Observable<Job[]> {
    return this.http.get<Job[]>(`${this.baseUrl}/jobs`, { headers: this.getHeaders() }).pipe(
      catchError((err) => throwError(() => err)),
    );
  }

  getJob(jobId: string): Observable<Job> {
    return this.http.get<Job>(`${this.baseUrl}/jobs/${encodeURIComponent(jobId)}`, { headers: this.getHeaders() }).pipe(
      catchError((err) => throwError(() => err)),
    );
  }

  approveStoryboard(jobId: string, version: number): Observable<Job> {
    return this.http.post<Job>(`${this.baseUrl}/jobs/${encodeURIComponent(jobId)}/storyboards/approve`, { version, actor: 'web-v2' }, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  rejectStoryboard(jobId: string, version: number): Observable<Job> {
    return this.http.post<Job>(`${this.baseUrl}/jobs/${encodeURIComponent(jobId)}/storyboards/reject`, { version, actor: 'web-v2' }, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  regenerateStoryboard(jobId: string, version: number, revision?: string): Observable<Job> {
    return this.http.post<Job>(`${this.baseUrl}/jobs/${encodeURIComponent(jobId)}/storyboards/regenerate`, { version, actor: 'web-v2', revision }, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  approveImageStoryboard(jobId: string, version: number): Observable<Job> {
    return this.http.post<Job>(`${this.baseUrl}/jobs/${encodeURIComponent(jobId)}/storyboards/images/approve`, { version, actor: 'web-v2' }, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  revisionImageStoryboard(jobId: string, version: number, opts: { scene_indexes?: number[]; revision?: string } = {}): Observable<Job> {
    return this.http.post<Job>(`${this.baseUrl}/jobs/${encodeURIComponent(jobId)}/storyboards/images/revision`, { version, actor: 'web-v2', scene_indexes: opts.scene_indexes, revision: opts.revision }, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  regenerateImageStoryboard(jobId: string, version: number, revision?: string): Observable<Job> {
    return this.http.post<Job>(`${this.baseUrl}/jobs/${encodeURIComponent(jobId)}/storyboards/images/regenerate`, { version, actor: 'web-v2', revision }, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  updateJob(jobId: string, payload: JobUpdate): Observable<Job> {
    return this.http.patch<Job>(`${this.baseUrl}/jobs/${encodeURIComponent(jobId)}`, payload, { headers: this.getHeaders() }).pipe(
      catchError((err) => throwError(() => err)),
    );
  }

  getCharacters(): Observable<Character[]> {
    return this.http.get<Character[]>(`${this.baseUrl}/characters`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getCharacter(id: string): Observable<Character> {
    return this.http.get<Character>(`${this.baseUrl}/characters/${encodeURIComponent(id)}`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  createJob(payload: JobCreate): Observable<Job> {
    return this.http.post<Job>(`${this.baseUrl}/jobs`, payload, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  deleteJob(jobId: string): Observable<{ deleted: string }> {
    return this.http.delete<{ deleted: string }>(`${this.baseUrl}/jobs/${encodeURIComponent(jobId)}`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  createCharacter(payload: Character): Observable<Character> {
    return this.http.post<Character>(`${this.baseUrl}/characters`, payload, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  updateCharacter(id: string, payload: Character): Observable<Character> {
    return this.http.put<Character>(`${this.baseUrl}/characters/${encodeURIComponent(id)}`, payload, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  deleteCharacter(id: string): Observable<{ deleted: string }> {
    return this.http.delete<{ deleted: string }>(`${this.baseUrl}/characters/${encodeURIComponent(id)}`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  createRegistrationToken(role: string, ttlSeconds?: number, pushToken?: boolean): Observable<RegistrationTokenResponse> {
    return this.http.post<RegistrationTokenResponse>(`${this.baseUrl}/nodes/registration-tokens`, { role, ttl_seconds: ttlSeconds, push_token: pushToken }, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  registerNode(payload: NodeRegisterRequest): Observable<NodeRegisterResponse> {
    return this.http.post<NodeRegisterResponse>(`${this.baseUrl}/nodes/register`, payload, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getNodes(): Observable<Worker[]> {
    return this.http.get<Worker[]>(`${this.baseUrl}/nodes`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getNode(nodeId: string): Observable<NodeDetail> {
    return this.http.get<NodeDetail>(`${this.baseUrl}/nodes/${encodeURIComponent(nodeId)}`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

   createBrand(payload: { id: string; name: string; metadata?: Record<string, unknown>; publishing?: Record<string, unknown> }): Observable<Brand> {
    return this.http.post<Brand>(`${this.baseUrl}/brands`, payload, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  updateBrand(brandId: string, payload: { name?: string; enabled?: boolean; metadata?: Record<string, unknown>; publishing?: Record<string, unknown> }): Observable<Brand> {
    return this.http.put<Brand>(`${this.baseUrl}/brands/${encodeURIComponent(brandId)}`, payload, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  deleteBrand(brandId: string): Observable<{ deleted: string }> {
    return this.http.delete<{ deleted: string }>(`${this.baseUrl}/brands/${encodeURIComponent(brandId)}`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getChannels(brandId: string): Observable<Channel[]> {
    return this.http.get<Channel[]>(`${this.baseUrl}/brands/${encodeURIComponent(brandId)}/channels`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  workerAction(nodeId: string, action: NodeActionPayload): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(`${this.baseUrl}/nodes/${encodeURIComponent(nodeId)}/actions`, action, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  revokeNode(nodeId: string): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(`${this.baseUrl}/nodes/${encodeURIComponent(nodeId)}/revoke`, {}, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  renewNode(nodeId: string, csr: string): Observable<NodeRegisterResponse> {
    return this.http.post<NodeRegisterResponse>(`${this.baseUrl}/nodes/${encodeURIComponent(nodeId)}/renew`, { csr }, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getBrands(): Observable<Brand[]> {
    return this.http.get<Brand[]>(`${this.baseUrl}/brands`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getWorkflows(): Observable<Workflow[]> {
    return this.http.get<Workflow[]>(`${this.baseUrl}/workflows`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getWorkflow(kind: string, name: string): Observable<Record<string, unknown>> {
    return this.http.get<Record<string, unknown>>(`${this.baseUrl}/workflows/${encodeURIComponent(kind)}/${encodeURIComponent(name)}`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  saveWorkflow(kind: string, name: string, payload: Record<string, unknown>): Observable<Record<string, unknown>> {
    return this.http.put<Record<string, unknown>>(`${this.baseUrl}/workflows/${encodeURIComponent(kind)}/${encodeURIComponent(name)}`, payload, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  deleteWorkflow(kind: string, name: string): Observable<Record<string, unknown>> {
    return this.http.delete<Record<string, unknown>>(`${this.baseUrl}/workflows/${encodeURIComponent(kind)}/${encodeURIComponent(name)}`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getTelegramStatus(): Observable<TelegramStatus> {
    return this.http.get<TelegramStatus>(`${this.baseUrl}/telegram/status`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getTelegramBotInfo(): Observable<Record<string, unknown>> {
    return this.http.get<Record<string, unknown>>(`${this.baseUrl}/telegram/bot-info`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getSystemRoles(): Observable<SystemRolesResponse> {
    return this.http.get<SystemRolesResponse>(`${this.baseUrl}/system/roles`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  updateSystemRoles(roles: string[]): Observable<RolesUpdateResponse> {
    return this.http.post<RolesUpdateResponse>(`${this.baseUrl}/system/roles`, { roles }, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  createSession(payload: { login: string; password: string }): Observable<{ authenticated: boolean }> {
    return this.http.post<{ authenticated: boolean }>(`${this.baseUrl}/session`, payload, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  deleteSession(): Observable<{ authenticated: boolean }> {
    return this.http.delete<{ authenticated: boolean }>(`${this.baseUrl}/session`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getSession(): Observable<{ authenticated: boolean; user?: string; role?: string }> {
    return this.http.get<{ authenticated: boolean; user?: string; role?: string }>(`${this.baseUrl}/session`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getAlerts(): Observable<Alert[]> {
    return this.http.get<Alert[]>(`${this.baseUrl}/alerts`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getLogs(params?: { limit?: number; level?: string; job_id?: string; node_name?: string }): Observable<LogEntry[]> {
    const query = new URLSearchParams();
    if (params?.limit) query.set('limit', String(params.limit));
    if (params?.level) query.set('level', params.level);
    if (params?.job_id) query.set('job_id', params.job_id);
    if (params?.node_name) query.set('node_name', params.node_name);
    const qs = query.toString();
    return this.http.get<LogEntry[]>(`${this.baseUrl}/logs${qs ? `?${qs}` : ''}`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getHealth(): Observable<HealthCheck> {
    return this.http.get<HealthCheck>(`${this.baseUrl}/health`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getHealthHistory(limit = 100): Observable<{ history: Array<Record<string, unknown>> }> {
    return this.http.get<{ history: Array<Record<string, unknown>> }>(`${this.baseUrl}/health/history?limit=${limit}`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getMetrics(): Observable<Record<string, unknown>> {
    return this.http.get<Record<string, unknown>>(`${this.baseUrl}/metrics`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getSecurityCheck(): Observable<{ ok: boolean; weak_or_missing: string[]; recommendation: string }> {
    return this.http.get<{ ok: boolean; weak_or_missing: string[]; recommendation: string }>(`${this.baseUrl}/security/check`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getSecrets(): Observable<SecretStatus> {
    return this.http.get<SecretStatus>(`${this.baseUrl}/settings/secrets`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  updateSecret(name: string, value: string): Observable<SecretStatus> {
    return this.http.put<SecretStatus>(`${this.baseUrl}/settings/secrets/${encodeURIComponent(name)}`, { value }, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  deleteSecret(name: string): Observable<SecretStatus> {
    return this.http.delete<SecretStatus>(`${this.baseUrl}/settings/secrets/${encodeURIComponent(name)}`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getIntegrationStatus(): Observable<Record<string, unknown>> {
    return this.http.get<Record<string, unknown>>(`${this.baseUrl}/integrations`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getModels(): Observable<Record<string, unknown>> {
    return this.http.get<Record<string, unknown>>(`${this.baseUrl}/system/models`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  pullModel(name: string): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(`${this.baseUrl}/system/models/pull`, { name }, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  deleteModel(name: string): Observable<Record<string, unknown>> {
    return this.http.delete<Record<string, unknown>>(`${this.baseUrl}/system/models/${encodeURIComponent(name)}`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getCertificates(): Observable<Record<string, unknown>> {
    return this.http.get<Record<string, unknown>>(`${this.baseUrl}/system/certificates`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  renewCertificate(): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(`${this.baseUrl}/system/certificates/renew`, {}, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getUpdateStatus(): Observable<UpdateStatus> {
    return this.http.get<UpdateStatus>(`${this.baseUrl}/system/update`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  checkUpdate(): Observable<UpdateStatus> {
    return this.http.post<UpdateStatus>(`${this.baseUrl}/system/update/check`, {}, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  installUpdate(): Observable<UpdateStatus> {
    return this.http.post<UpdateStatus>(`${this.baseUrl}/system/update/run`, {}, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getIntegrations(): Observable<Record<string, unknown>> {
    return this.http.get<Record<string, unknown>>(`${this.baseUrl}/integrations`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  restartSystem(): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(`${this.baseUrl}/system/update/restart`, {}, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getUpdateReadiness(): Observable<UpdateReadiness> {
    return this.http.get<UpdateReadiness>(`${this.baseUrl}/system/update/readiness`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getRollingStatus(): Observable<RollingStatus> {
    return this.http.get<RollingStatus>(`${this.baseUrl}/system/update/rolling`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  startRollingUpdate(payload: RollingUpdateRequest): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(`${this.baseUrl}/system/update/rolling`, payload, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  cancelRolling(): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(`${this.baseUrl}/system/update/rolling/cancel`, {}, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  promoteCanary(): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(`${this.baseUrl}/system/update/rolling/promote`, {}, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  rollbackCanary(): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(`${this.baseUrl}/system/update/rolling/rollback`, {}, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getBackups(): Observable<Record<string, unknown>> {
    return this.http.get<Record<string, unknown>>(`${this.baseUrl}/system/backups`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  createBackup(): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(`${this.baseUrl}/system/backups`, {}, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  restoreBackup(snapshotId: string): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(`${this.baseUrl}/system/backups/${encodeURIComponent(snapshotId)}/restore`, {}, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getLicense(): Observable<Record<string, unknown>> {
    return this.http.get<Record<string, unknown>>(`${this.baseUrl}/system/license`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getInstallationManifest(): Observable<Record<string, unknown>> {
    return this.http.get<Record<string, unknown>>(`${this.baseUrl}/system/installation-manifest`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getBrandChannels(brandId: string): Observable<Channel[]> {
    return this.http.get<Channel[]>(`${this.baseUrl}/brands/${encodeURIComponent(brandId)}/channels`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  createChannel(brandId: string, payload: { brand_id: string; channel_type: string; target: string; enabled?: boolean; metadata?: Record<string, unknown> }): Observable<Channel> {
    return this.http.post<Channel>(`${this.baseUrl}/brands/${encodeURIComponent(brandId)}/channels`, payload, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  updateChannel(channelId: string, payload: { target?: string; enabled?: boolean; metadata?: Record<string, unknown> }): Observable<Channel> {
    return this.http.put<Channel>(`${this.baseUrl}/channels/${encodeURIComponent(channelId)}`, payload, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  deleteChannel(channelId: string): Observable<{ deleted: string }> {
    return this.http.delete<{ deleted: string }>(`${this.baseUrl}/channels/${encodeURIComponent(channelId)}`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getChannelTypes(): Observable<string[]> {
    return this.http.get<string[]>(`${this.baseUrl}/channels/types`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getJobAssets(jobId: string): Observable<Array<{ artifact_id: string; name: string; kind: string; size: number; mime_type: string; valid: boolean; url?: string }>> {
    return this.http.get<Array<{ artifact_id: string; name: string; kind: string; size: number; mime_type: string; valid: boolean; url?: string }>>(`${this.baseUrl}/jobs/${encodeURIComponent(jobId)}/assets`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getJobArtifacts(jobId: string): Observable<{ job_id: string; artifacts: ArtifactRecord[] }> {
    return this.http.get<{ job_id: string; artifacts: ArtifactRecord[] }>(`${this.baseUrl}/jobs/${encodeURIComponent(jobId)}/artifacts`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  verifyArtifacts(jobId: string): Observable<ArtifactVerifyResponse> {
    return this.http.post<ArtifactVerifyResponse>(`${this.baseUrl}/jobs/${encodeURIComponent(jobId)}/artifacts/verify`, {}, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  downloadArtifact(jobId: string, artifactId: string): Observable<Blob> {
    return this.http.get(`${this.baseUrl}/jobs/${encodeURIComponent(jobId)}/artifacts/${encodeURIComponent(artifactId)}/download`, { headers: this.getHeaders(), responseType: 'blob' }).pipe(catchError(this.handleError));
  }

  uploadArtifact(jobId: string, folder: 'references' | 'audio', filename: string, file: File): Observable<ArtifactRecord> {
    const form = new FormData();
    form.append('file', file);
    return this.http.put<ArtifactRecord>(`${this.baseUrl}/jobs/${encodeURIComponent(jobId)}/uploads/${encodeURIComponent(folder)}/${encodeURIComponent(filename)}`, form, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  exportJob(jobId: string): Observable<Blob> {
    return this.http.get(`${this.baseUrl}/jobs/${encodeURIComponent(jobId)}/export`, { headers: this.getHeaders(), responseType: 'blob' }).pipe(catchError(this.handleError));
  }

  importProject(file: File): Observable<Job> {
    const form = new FormData();
    form.append('file', file);
    return this.http.post<Job>(`${this.baseUrl}/projects/import`, form, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  pauseJob(jobId: string): Observable<Job> {
    return this.http.post<Job>(`${this.baseUrl}/jobs/${encodeURIComponent(jobId)}/pause`, {}, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  resumeJob(jobId: string): Observable<Job> {
    return this.http.post<Job>(`${this.baseUrl}/jobs/${encodeURIComponent(jobId)}/resume`, {}, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  retryJob(jobId: string): Observable<Job> {
    return this.http.post<Job>(`${this.baseUrl}/jobs/${encodeURIComponent(jobId)}/retry`, {}, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  regenerateJob(jobId: string): Observable<Job> {
    return this.http.post<Job>(`${this.baseUrl}/jobs/${encodeURIComponent(jobId)}/regenerate`, {}, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  cancelJob(jobId: string): Observable<Job> {
    return this.http.post<Job>(`${this.baseUrl}/jobs/${encodeURIComponent(jobId)}/cancel`, {}, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  approveJob(jobId: string): Observable<Job> {
    return this.http.post<Job>(`${this.baseUrl}/jobs/${encodeURIComponent(jobId)}/approve`, {}, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  approveScript(jobId: string, actor = 'web-v2'): Observable<Job> {
    return this.http.post<Job>(`${this.baseUrl}/jobs/${encodeURIComponent(jobId)}/script/approve`, { actor }, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  requestScriptRevision(jobId: string, revision: string, actor = 'web-v2'): Observable<Job> {
    return this.http.post<Job>(`${this.baseUrl}/jobs/${encodeURIComponent(jobId)}/script/revision`, { actor, revision }, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  regenerateScript(jobId: string, revision?: string, actor = 'web-v2'): Observable<Job> {
    return this.http.post<Job>(`${this.baseUrl}/jobs/${encodeURIComponent(jobId)}/script/regenerate`, { actor, revision }, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  publishJob(jobId: string, channels?: string[]): Observable<Job> {
    const qs = channels?.length ? `?channels=${encodeURIComponent(channels.join(','))}` : '';
    return this.http.post<Job>(`${this.baseUrl}/jobs/${encodeURIComponent(jobId)}/publish${qs}`, {}, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getDeadLetterTasks(): Observable<DeadLetterTask[]> {
    return this.http.get<DeadLetterTask[]>(`${this.baseUrl}/tasks/dead-letter`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  retryDeadLetterTask(taskId: string): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(`${this.baseUrl}/tasks/dead-letter/${encodeURIComponent(taskId)}/retry`, {}, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getQueueState(): Observable<QueueState> {
    return this.http.get<QueueState>(`${this.baseUrl}/tasks/queue`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  uploadLogo(file: File): Observable<{ saved: boolean }> {
    return this.http.put<{ saved: boolean }>(`${this.baseUrl}/settings/logo`, file, {
      headers: this.getHeaders().set('Content-Type', file.type),
    }).pipe(catchError(this.handleError));
  }

  deleteLogo(): Observable<{ deleted: boolean }> {
    return this.http.delete<{ deleted: boolean }>(`${this.baseUrl}/settings/logo`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getLogo(): Observable<Blob> {
    return this.http.get(`${this.baseUrl}/settings/logo`, { responseType: 'blob', headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  recoverToNormal(): Observable<SystemState> {
    return this.http.post<SystemState>(`${this.baseUrl}/system/recovery/normal`, {}, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getSetupStatus(token: string): Observable<SetupStatus> {
    return this.http.get<SetupStatus>(`${this.baseUrl}/setup`, { headers: this.getHeaders().set('X-Vertep-Setup-Token', token) }).pipe(catchError(this.handleError));
  }

  getSetupHealth(token: string): Observable<SetupHealth> {
    return this.http.get<SetupHealth>(`${this.baseUrl}/setup/health`, { headers: this.getHeaders().set('X-Vertep-Setup-Token', token) }).pipe(catchError(this.handleError));
  }

  completeSetup(token: string, payload: Record<string, unknown>): Observable<SetupCompleteResult> {
    return this.http.post<SetupCompleteResult>(`${this.baseUrl}/setup/complete`, payload, { headers: this.getHeaders().set('X-Vertep-Setup-Token', token) }).pipe(catchError(this.handleError));
  }

  private handleError(error: unknown) {
    const err = error as { detail?: string; message?: string } | undefined;
    return throwError(() => new Error(err?.detail || err?.message || 'API error'));
  }
}
