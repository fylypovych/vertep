import { Injectable } from '@angular/core';
import { CanActivate, Router, UrlTree } from '@angular/router';
import { Observable, of } from 'rxjs';
import { catchError, map, tap } from 'rxjs/operators';
import { VertepApiService } from './api.service';
import { PolicyService } from './services/policy.service';

@Injectable()
export class AuthGuard implements CanActivate {
  constructor(private api: VertepApiService, private router: Router, private policy: PolicyService) {}

  canActivate(): Observable<boolean | UrlTree> {
    return this.api.getSession().pipe(
      tap(() => { this.policy.refresh(); }),
      // Пропускаємо лише справжню авторизовану сесію; authenticated:false веде на login.
      map((session) => (session.authenticated ? true : this.router.createUrlTree(['/login']))),
      catchError(() => of(this.router.createUrlTree(['/login'])))
    );
  }
}
