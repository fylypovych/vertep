import { inject, Injectable } from '@angular/core';
import { I18nService } from '../i18n/i18n.service';
import { API_ERRORS } from '../i18n/api-errors';
import {
  HttpEvent,
  HttpHandler,
  HttpInterceptor,
  HttpRequest,
  HttpErrorResponse,
} from '@angular/common/http';
import { Observable, from, throwError } from 'rxjs';
import { catchError, switchMap } from 'rxjs/operators';

const CYRILLIC = /[А-ЯЄІЇҐа-яєіїґ]/;

export function errorMessage(error: HttpErrorResponse, i18n: I18nService): string {
  const known = API_ERRORS[error.error?.code];
  if (known) return known[i18n.language()];
  const raw = error.error?.detail;
  const detail = typeof raw === 'string' ? raw : raw?.message;
  // A message built at run time has no code; it still names the cause, so it is shown unless it is Ukrainian in the English panel.
  if (typeof detail === 'string' && detail && !(i18n.language() === 'en' && CYRILLIC.test(detail))) return detail;
  return i18n.t(statusMessage(error));
}

function statusMessage(error: HttpErrorResponse): string {
  if (error.status === 0) return 'Немає зв\'язку із сервером';
  if (error.status === 401) return 'Потрібно увійти в систему';
  if (error.status === 403) return 'Недостатньо прав';
  if (error.status === 404) return 'Не знайдено';
  if (error.status === 423) return 'Дію заблоковано станом системи';
  if (error.status >= 500) return 'Сервер тимчасово недоступний';
  return 'Не вдалося виконати запит';
}

/**
 * Показ помилки належить компоненту, який знає контекст; другий toast тут дублював би
 * його. Інтерцептор лише гарантує, що err.message придатний для показу українською.
 */
@Injectable()
export class ErrorInterceptor implements HttpInterceptor {
  private i18n = inject(I18nService);

  intercept(
    req: HttpRequest<unknown>,
    next: HttpHandler
  ): Observable<HttpEvent<unknown>> {
    return next.handle(req).pipe(
      catchError((error: HttpErrorResponse) => {
        // A request for binary data (audio) receives its JSON error as a Blob: read it, so the
        // code and detail reach the message instead of a generic status text (#455).
        if (error instanceof HttpErrorResponse && error.error instanceof Blob && /json/i.test(error.error.type)) {
          return from(error.error.text()).pipe(switchMap((text) => {
            let body: unknown = null;
            try { body = JSON.parse(text); } catch { /* not JSON after all: keep the status text */ }
            return this.reject(new HttpErrorResponse({ error: body, headers: error.headers, status: error.status,
              statusText: error.statusText, url: error.url ?? undefined }));
          }));
        }
        return this.reject(error);
      })
    );
  }

  private reject(error: HttpErrorResponse): Observable<never> {
    if (error instanceof HttpErrorResponse) {
      const message = errorMessage(error, this.i18n);
      Object.defineProperty(error, 'message', { value: message, configurable: true });
      // Some components read the server text directly: they get the chosen language too.
      if (typeof error.error?.detail === 'string') error.error.detail = message;
    }
    return throwError(() => error);
  }
}
