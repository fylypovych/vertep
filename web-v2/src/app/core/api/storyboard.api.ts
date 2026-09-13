import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { catchError } from 'rxjs/operators';
import { BaseApiService } from './base-api.service';
import { Job, StoryboardVersion, StoryboardActionRequest } from '../models';

/**
 * Storyboard domain client: generation/approval/revision of storyboard
 * versions and their per-scene images for a job.
 */
@Injectable({ providedIn: 'root' })
export class StoryboardApiService {
  private h = () => this.base.headers();
  constructor(private http: HttpClient, private base: BaseApiService) {}

  private action(jobId: string, path: string, body: StoryboardActionRequest): Observable<Job> {
    return this.http.post<Job>(`${this.base.url}/jobs/${this.base.enc(jobId)}${path}`, body, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }

  list(jobId: string): Observable<StoryboardVersion[]> {
    return this.http.get<StoryboardVersion[]>(`${this.base.url}/jobs/${this.base.enc(jobId)}/storyboards`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }

  version(jobId: string, version: number): Observable<StoryboardVersion> {
    return this.http.get<StoryboardVersion>(`${this.base.url}/jobs/${this.base.enc(jobId)}/storyboards/${this.base.enc(String(version))}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }

  generate(jobId: string): Observable<Job> {
    return this.http.post<Job>(`${this.base.url}/jobs/${this.base.enc(jobId)}/storyboards/generate`, {}, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }

  approve(jobId: string, version: number): Observable<Job> {
    return this.action(jobId, '/storyboards/approve', { version, actor: 'web-v2' });
  }

  reject(jobId: string, version: number, revision?: string): Observable<Job> {
    return this.action(jobId, '/storyboards/reject', { version, actor: 'web-v2', revision });
  }

  regenerate(jobId: string, version: number, revision?: string): Observable<Job> {
    return this.action(jobId, '/storyboards/regenerate', { version, actor: 'web-v2', revision });
  }

  approveImages(jobId: string, version: number): Observable<Job> {
    return this.action(jobId, '/storyboards/images/approve', { version, actor: 'web-v2' });
  }

  revisionImages(jobId: string, version: number, opts: { scene_indexes?: number[]; revision?: string } = {}): Observable<Job> {
    return this.action(jobId, '/storyboards/images/revision', { version, actor: 'web-v2', scene_indexes: opts.scene_indexes, revision: opts.revision });
  }

  regenerateImages(jobId: string, version: number, revision?: string): Observable<Job> {
    return this.action(jobId, '/storyboards/images/regenerate', { version, actor: 'web-v2', revision });
  }
}