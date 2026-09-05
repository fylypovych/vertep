import { Injectable } from '@angular/core';
import { HttpEvent, HttpHandler, HttpInterceptor, HttpRequest } from '@angular/common/http';
import { Router } from '@angular/router';
import { Observable } from 'rxjs';
import { tap } from 'rxjs/operators';

@Injectable()
export class AuthInterceptor implements HttpInterceptor {
  constructor(private router: Router) {}

  intercept(req: HttpRequest<any>, next: HttpHandler): Observable<HttpEvent<any>> {
    const csrf = document.cookie
      .split('; ')
      .find(x => x.startsWith('vertep_csrf='))
      ?.split('=')[1];

    let headers = req.headers;
    if (csrf) {
      headers = headers.set('X-CSRF-Token', csrf);
    }

    const cloned = req.clone({ headers });
    return next.handle(cloned).pipe(
      tap({
        error: (err) => {
          if (err.status === 401) {
            this.router.navigate(['/login']);
          }
        },
      })
    );
  }
}
