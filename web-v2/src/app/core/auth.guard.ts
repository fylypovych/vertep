import { Injectable } from '@angular/core';
import { CanActivate, CanActivateChild, Router, UrlTree } from '@angular/router';
import { Observable, of } from 'rxjs';
import { catchError, map, tap } from 'rxjs/operators';
import { AuthApiService } from './api/auth.api';
import { PolicyService } from './services/policy.service';

export function isConnectionFailure(error: { status?: number } | null | undefined): boolean {
  const status = error?.status;
  return status === 0 || (typeof status === 'number' && status >= 500);
}

@Injectable()
export class AuthGuard implements CanActivate, CanActivateChild {
  constructor(private auth: AuthApiService, private router: Router, private policy: PolicyService) {}

  canActivate(): Observable<boolean | UrlTree> {
    return this.auth.getUserProfile().pipe(
      // Issue #75 S1: getUserProfile rejects a missing/unknown role, so reaching
      // this point means the server confirmed a complete identity. Меню отримує
      // перевірену роль до створення layout, незалежно від запиту header.
      tap((profile) => {
        this.policy.userRole.set(profile.role);
        this.policy.refresh();
      }),
      map(() => true),
      // Лише підтверджена відсутність сесії веде на login. Під час рестарту CORE
      // користувач лишається в системі, а policy блокує дії (fail-closed).
      catchError((error) => of(isConnectionFailure(error) ? true : this.router.createUrlTree(['/login'])))
    );
  }

  canActivateChild(): Observable<boolean | UrlTree> {
    return this.canActivate();
  }
}
