import { Injectable, ErrorHandler } from '@angular/core';
import { HttpClient } from '@angular/common/http';

@Injectable({ providedIn: 'root' })
export class GlobalErrorHandlerService implements ErrorHandler {
  private errors: unknown[] = [];

  constructor(private http: HttpClient) {}

  handleError(error: unknown): void {
    const report = {
      timestamp: new Date().toISOString(),
      error: typeof error === 'string' ? error : error instanceof Error ? error.message : JSON.stringify(error),
      user_agent: navigator.userAgent,
      url: window.location.href,
    };
    this.errors.push(report);
    console.error('[GlobalErrorHandler]', error);
    // Send to backend collector
    this.http.post('/api/errors/collect', report).subscribe({
      next: () => {},
      error: () => {
        // Fail closed: keep local copy
      }
    });
  }

  getErrors(): unknown[] {
    return this.errors;
  }

  clear(): void {
    this.errors = [];
  }
}
