import { Injectable } from '@angular/core';
import {
  HttpEvent,
  HttpHandler,
  HttpInterceptor,
  HttpRequest,
  HttpErrorResponse,
} from '@angular/common/http';
import { Observable, throwError } from 'rxjs';
import { catchError } from 'rxjs/operators';

function ukrainianMessage(error: HttpErrorResponse): string {
  const detail = error.error?.detail;
  if (typeof detail === 'string' && detail) return detail;
  if (typeof detail?.message === 'string' && detail.message) return detail.message;
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
  intercept(
    req: HttpRequest<unknown>,
    next: HttpHandler
  ): Observable<HttpEvent<unknown>> {
    return next.handle(req).pipe(
      catchError((error: HttpErrorResponse) => {
        if (error instanceof HttpErrorResponse) {
          Object.defineProperty(error, 'message', { value: ukrainianMessage(error), configurable: true });
        }
        return throwError(() => error);
      })
    );
  }
}
