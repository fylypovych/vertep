import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { catchError } from 'rxjs/operators';
import { BaseApiService } from './base-api.service';
import { Job, JobCreate, JobUpdate, ArtifactRecord, ArtifactVerifyResponse, DeadLetterTask, QueueState, ScheduledJob } from '../models';

@Injectable()
export class JobsApiService {
  private h = () => this.base.headers();
  constructor(private http: HttpClient, private base: BaseApiService) {}

  list(): Observable<Job[]> {
    return this.http.get<Job[]>(`${this.base.url}/jobs`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }
  get(jobId: string): Observable<Job> {
    return this.http.get<Job>(`${this.base.url}/jobs/${this.base.enc(jobId)}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }
  create(payload: JobCreate): Observable<Job> {
    return this.http.post<Job>(`${this.base.url}/jobs`, payload, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }
  update(jobId: string, payload: JobUpdate): Observable<Job> {
    return this.http.patch<Job>(`${this.base.url}/jobs/${this.base.enc(jobId)}`, payload, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }
  delete(jobId: string): Observable<{ deleted: string }> {
    return this.http.delete<{ deleted: string }>(`${this.base.url}/jobs/${this.base.enc(jobId)}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }
  pause(jobId: string): Observable<Job> {
    return this.http.post<Job>(`${this.base.url}/jobs/${this.base.enc(jobId)}/pause`, {}, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }
  resume(jobId: string): Observable<Job> {
    return this.http.post<Job>(`${this.base.url}/jobs/${this.base.enc(jobId)}/resume`, {}, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }
  retry(jobId: string): Observable<Job> {
    return this.http.post<Job>(`${this.base.url}/jobs/${this.base.enc(jobId)}/retry`, {}, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }
  regenerate(jobId: string): Observable<Job> {
    return this.http.post<Job>(`${this.base.url}/jobs/${this.base.enc(jobId)}/regenerate`, {}, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }
  cancel(jobId: string): Observable<Job> {
    return this.http.post<Job>(`${this.base.url}/jobs/${this.base.enc(jobId)}/cancel`, {}, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }
  approve(jobId: string): Observable<Job> {
    return this.http.post<Job>(`${this.base.url}/jobs/${this.base.enc(jobId)}/approve`, {}, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }
  publish(jobId: string, channels?: string[]): Observable<Job> {
    const qs = channels?.length ? `?channels=${encodeURIComponent(channels.join(','))}` : '';
    return this.http.post<Job>(`${this.base.url}/jobs/${this.base.enc(jobId)}/publish${qs}`, {}, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }
  artifacts(jobId: string): Observable<{ job_id: string; artifacts: ArtifactRecord[] }> {
    return this.http.get<{ job_id: string; artifacts: ArtifactRecord[] }>(`${this.base.url}/jobs/${this.base.enc(jobId)}/artifacts`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }
  verifyArtifacts(jobId: string): Observable<ArtifactVerifyResponse> {
    return this.http.post<ArtifactVerifyResponse>(`${this.base.url}/jobs/${this.base.enc(jobId)}/artifacts/verify`, {}, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }
  downloadArtifact(jobId: string, artifactId: string): Observable<Blob> {
    return this.http.get(`${this.base.url}/jobs/${this.base.enc(jobId)}/artifacts/${this.base.enc(artifactId)}/download`, { headers: this.h(), responseType: 'blob' }).pipe(catchError(this.base.handleError));
  }
  uploadArtifact(jobId: string, folder: string, filename: string, file: File): Observable<ArtifactRecord> {
    const form = new FormData(); form.append('file', file);
    return this.http.put<ArtifactRecord>(`${this.base.url}/jobs/${this.base.enc(jobId)}/uploads/${this.base.enc(folder)}/${this.base.enc(filename)}`, form, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }
  exportJob(jobId: string): Observable<Blob> {
    return this.http.get(`${this.base.url}/jobs/${this.base.enc(jobId)}/export`, { headers: this.h(), responseType: 'blob' }).pipe(catchError(this.base.handleError));
  }
  importProject(file: File): Observable<Job> {
    const form = new FormData(); form.append('file', file);
    return this.http.post<Job>(`${this.base.url}/projects/import`, form, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }
  deadLetter(): Observable<DeadLetterTask[]> {
    return this.http.get<DeadLetterTask[]>(`${this.base.url}/tasks/dead-letter`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }
  retryDeadLetter(taskId: string): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(`${this.base.url}/tasks/dead-letter/${this.base.enc(taskId)}/retry`, {}, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }
  queueState(): Observable<QueueState> {
    return this.http.get<QueueState>(`${this.base.url}/tasks/queue`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }
}
