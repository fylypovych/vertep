import { Injectable, Inject } from '@angular/core';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { Observable, throwError } from 'rxjs';
import { catchError, map } from 'rxjs/operators';
import { Worker, Job, Character, SystemStatus } from '../core/models';

export interface Brand {
  brand_id: string;
  name: string;
}

export interface Channel {
  channel_id: string;
  brand_id: string;
  type: string;
}

export interface SystemRole {
  name: string;
  count: number;
}

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
    console.log('[DIAG][ApiService] getStatus() request');
    return this.http.get<SystemStatus>(`${this.baseUrl}/status`, { headers: this.getHeaders() }).pipe(
      catchError((err) => {
        console.error('[DIAG][ApiService] getStatus() http error', err);
        return throwError(() => err);
      }),
      map((status) => {
        console.log('[DIAG][ApiService] getStatus() raw response', status);
        const normalized = {
          ...status,
          workers: Array.isArray(status.workers) ? status.workers : [],
        };
        console.log('[DIAG][ApiService] getStatus() normalized', normalized);
        return normalized;
      }),
      catchError((err) => {
        console.error('[DIAG][ApiService] getStatus() pipe error', err);
        return throwError(() => err);
      }),
    );
  }

  getWorkers(): Observable<Worker[]> {
    console.log('[DIAG][ApiService] getWorkers() request');
    return this.http.get<Worker[]>(`${this.baseUrl}/workers`, { headers: this.getHeaders() }).pipe(
      catchError((err) => {
        console.error('[DIAG][ApiService] getWorkers() http error', err);
        return throwError(() => err);
      }),
      map((workers) => {
        console.log('[DIAG][ApiService] getWorkers() raw response', workers);
        const normalized = workers.map((w) => ({
          ...w,
          load: typeof w.load === 'number' ? w.load : 0,
        }));
        console.log('[DIAG][ApiService] getWorkers() normalized', normalized);
        return normalized;
      }),
      catchError((err) => {
        console.error('[DIAG][ApiService] getWorkers() pipe error', err);
        return throwError(() => err);
      }),
    );
  }

  getJobs(): Observable<Job[]> {
    console.log('[DIAG][ApiService] getJobs() request');
    return this.http.get<any[]>(`${this.baseUrl}/jobs`, { headers: this.getHeaders() }).pipe(
      catchError((err) => {
        console.error('[DIAG][ApiService] getJobs() http error', err);
        return throwError(() => err);
      }),
      map((jobs) => {
        console.log('[DIAG][ApiService] getJobs() raw response', jobs);
        const normalized = jobs.map((job) => ({
          ...job,
          id: job.job_id || job.id,
        })) as Job[];
        console.log('[DIAG][ApiService] getJobs() normalized', normalized);
        return normalized;
      }),
      catchError((err) => {
        console.error('[DIAG][ApiService] getJobs() pipe error', err);
        return throwError(() => err);
      }),
    );
  }

  getCharacters(): Observable<Character[]> {
    return this.http.get<Character[]>(`${this.baseUrl}/characters`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  createJob(payload: any): Observable<any> {
    return this.http.post(`${this.baseUrl}/jobs`, payload, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  deleteJob(id: string): Observable<any> {
    return this.http.delete(`${this.baseUrl}/jobs/${encodeURIComponent(id)}`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  createCharacter(payload: any): Observable<any> {
    return this.http.post(`${this.baseUrl}/characters`, payload, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  updateCharacter(id: string, payload: any): Observable<any> {
    return this.http.put(`${this.baseUrl}/characters/${encodeURIComponent(id)}`, payload, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  deleteCharacter(id: string): Observable<any> {
    return this.http.delete(`${this.baseUrl}/characters/${encodeURIComponent(id)}`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  createWorker(payload: any): Observable<any> {
    return this.http.post(`${this.baseUrl}/nodes`, payload, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  updateWorker(nodeId: string, payload: any): Observable<any> {
    return this.http.put(`${this.baseUrl}/nodes/${encodeURIComponent(nodeId)}`, payload, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  deleteWorker(nodeId: string): Observable<any> {
    return this.http.delete(`${this.baseUrl}/nodes/${encodeURIComponent(nodeId)}`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  workerAction(nodeId: string, action: string): Observable<any> {
    return this.http.post(`${this.baseUrl}/nodes/${encodeURIComponent(nodeId)}/actions`, { action }, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getBrands(): Observable<Brand[]> {
    return this.http.get<Brand[]>(`${this.baseUrl}/brands`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getWorkflows(): Observable<any[]> {
    return this.http.get<any[]>(`${this.baseUrl}/workflows`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getTelegramStatus(): Observable<any> {
    return this.http.get<any>(`${this.baseUrl}/telegram/status`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getSystemRoles(): Observable<SystemRole[]> {
    return this.http.get<SystemRole[]>(`${this.baseUrl}/system/roles`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  createSession(payload: { login: string; password: string }): Observable<any> {
    return this.http.post(`${this.baseUrl}/session`, payload, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  deleteSession(): Observable<any> {
    return this.http.delete(`${this.baseUrl}/session`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getSession(): Observable<any> {
    return this.http.get<any>(`${this.baseUrl}/session`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  private handleError(error: any) {
    return throwError(() => new Error(error?.detail || error?.message || 'API error'));
  }
}
