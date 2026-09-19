import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, catchError, throwError } from 'rxjs';

export interface SecurityCheckResponse {
  ok: boolean;
  weak_or_missing: string[];
  recommendation: string;
}

export interface CertificatesResponse {
  certificates: Record<string, { status: string; present?: boolean; expires_at?: string; days_remaining?: number }> | string;
}

export interface CertificateRenewResponse {
  certificate: string;
  detail?: string;
}

@Injectable({ providedIn: 'root' })
export class SecurityApiService {
  private baseUrl = '/api';

  constructor(private http: HttpClient) {}

  private getHeaders(): Record<string, string> {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    const csrf = document.cookie
      .split('; ')
      .find(x => x.startsWith('vertep_csrf='))
      ?.split('=')[1];
    if (csrf) {
      headers['X-CSRF-Token'] = csrf;
    }
    return headers;
  }

  private handleError(err: unknown) {
    return throwError(() => err);
  }

  check(): Observable<SecurityCheckResponse> {
    return this.http.get<SecurityCheckResponse>(`${this.baseUrl}/security/check`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  certificates(): Observable<CertificatesResponse> {
    return this.http.get<CertificatesResponse>(`${this.baseUrl}/system/certificates`, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }

  renewCertificate(): Observable<CertificateRenewResponse> {
    return this.http.post<CertificateRenewResponse>(`${this.baseUrl}/system/certificates/renew`, {}, { headers: this.getHeaders() }).pipe(catchError(this.handleError));
  }
}
