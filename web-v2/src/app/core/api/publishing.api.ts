import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { catchError } from 'rxjs/operators';
import { BaseApiService } from './base-api.service';
import { Job, PublishRequest } from '../models';

/**
 * Publishing domain client: publishing a finished job to channels.
 * Per-channel results are read back from the `Job.publication_results`
 * field of the returned job (no dedicated endpoint exists).
 */
@Injectable({ providedIn: 'root' })
export class PublishingApiService {
  private h = () => this.base.headers();
  constructor(private http: HttpClient, private base: BaseApiService) {}

  publish(jobId: string, request: PublishRequest = {}): Observable<Job> {
    const qs = request.channels?.length ? `?channels=${encodeURIComponent(request.channels.join(','))}` : '';
    return this.http.post<Job>(`${this.base.url}/jobs/${this.base.enc(jobId)}/publish${qs}`, {}, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }
}
