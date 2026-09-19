import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { catchError } from 'rxjs/operators';
import { BaseApiService } from './base-api.service';
import { QueueState, DeadLetterTask, OperationAck } from '../models';

/**
 * Queue domain client: task queue snapshot and dead-letter operations.
 */
@Injectable({ providedIn: 'root' })
export class QueueApiService {
  private h = () => this.base.headers();
  constructor(private http: HttpClient, private base: BaseApiService) {}

  state(): Observable<QueueState> {
    return this.http.get<QueueState>(`${this.base.url}/tasks/queue`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }

  deadLetter(): Observable<DeadLetterTask[]> {
    return this.http.get<DeadLetterTask[]>(`${this.base.url}/tasks/dead-letter`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }

  retryDeadLetter(taskId: string): Observable<OperationAck> {
    return this.http.post<OperationAck>(`${this.base.url}/tasks/dead-letter/${this.base.enc(taskId)}/retry`, {}, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }
}
