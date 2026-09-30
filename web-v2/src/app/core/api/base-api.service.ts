import { HttpHeaders } from '@angular/common/http';
import { throwError } from 'rxjs';
import { Injectable } from '@angular/core';

@Injectable({ providedIn: 'root' })
export class BaseApiService {
  readonly url = '/api';

  headers(): HttpHeaders {
    let h = new HttpHeaders({ 'Content-Type': 'application/json' });
    const csrf = document.cookie
      .split('; ')
      .find(x => x.startsWith('vertep_csrf='))
      ?.split('=')[1];
    if (csrf) h = h.set('X-CSRF-Token', csrf);
    return h;
  }

  enc(v: string): string {
    return encodeURIComponent(v);
  }

  handleError(error: unknown) {
    const err = error as {
      status?: number;
      detail?: unknown;
      message?: string;
      error?: { detail?: unknown; message?: string } | string;
    } | undefined;
    const responseBody = typeof err?.error === 'object' ? err.error : undefined;
    const detail = responseBody?.detail ?? err?.detail;
    // FastAPI повертає `detail` і рядком, і об'єктом (напр. structured 409).
    // Об'єкт зберігаємо окремо, а для message беремо людське текстове поле,
    // інакше new Error() отримав би "[object Object]".
    const structured = typeof detail === 'object' && detail !== null
      ? (detail as { message?: string; error_code?: string })
      : null;
    const message = (structured?.message || structured?.error_code
      || (typeof detail === 'string' ? detail : undefined)
      || responseBody?.message || err?.message || 'API error');
    const apiError = Object.assign(new Error(message), {
      status: err?.status,
      detail: structured ?? (typeof detail === 'string' ? detail : undefined),
    });
    return throwError(() => apiError);
  }
}
