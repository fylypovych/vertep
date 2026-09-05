import { Injectable } from '@angular/core';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { Observable, throwError } from 'rxjs';
import { catchError } from 'rxjs/operators';
import { Worker, Job, Character, SystemStatus } from '../core/models';

@Injectable({ providedIn: 'root' })
export class VertepApiService {
  private baseUrl = '/api';

  constructor(private http: HttpClient) {}

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
    return this.http.get<SystemStatus>(`${this.baseUrl}/status`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getWorkers(): Observable<Worker[]> {
    return this.http.get<Worker[]>(`${this.baseUrl}/workers`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getJobs(): Observable<Job[]> {
    return this.http.get<Job[]>(`${this.baseUrl}/jobs`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  getCharacters(): Observable<Character[]> {
    return this.http.get<Character[]>(`${this.baseUrl}/characters`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  private handleError(error: any) {
    return throwError(() => new Error(error?.detail || error?.message || 'API error'));
  }
}
