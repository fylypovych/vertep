import { HttpHeaders } from '@angular/common/http';
import { throwError } from 'rxjs';

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
    const err = error as { detail?: string; message?: string } | undefined;
    return throwError(() => new Error(err?.detail || err?.message || 'API error'));
  }
}
