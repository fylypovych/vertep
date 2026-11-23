import { HttpErrorResponse, HttpInterceptorFn } from '@angular/common/http';
import { inject } from '@angular/core';
import { catchError, throwError } from 'rxjs';
import { OwnerTokenService } from './owner-token.service';

/** Every LAB API call is an administrator's (#491 L2): attach the in-memory credential. */
export const authInterceptor: HttpInterceptorFn = (request, next) => {
  const owner = inject(OwnerTokenService);
  if (!request.url.startsWith('/api/lab/')) {
    return next(request);
  }
  const usedIdentity = !owner.token() && !!owner.identity();
  return next(request.clone({ setHeaders: owner.headers() })).pipe(
    catchError((error: HttpErrorResponse) => {
      // The Vertep assertion is short-lived: once refused, offer «Увійти через Vertep» again.
      if (error.status === 401 && usedIdentity) {
        owner.identity.set('');
      }
      return throwError(() => error);
    }),
  );
};
