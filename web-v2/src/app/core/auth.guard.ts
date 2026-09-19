import { Injectable } from '@angular/core';
import { CanActivate, Router, UrlTree } from '@angular/router';
import { Observable, of } from 'rxjs';
import { catchError, map, tap } from 'rxjs/operators';
import { AuthApiService } from './api/auth.api';
import { PolicyService } from './services/policy.service';

@Injectable()
export class AuthGuard implements CanActivate {
  constructor(private auth: AuthApiService, private router: Router, private policy: PolicyService) {}

  canActivate(): Observable<boolean | UrlTree> {
    return this.auth.getUserProfile().pipe(
      // Меню отримує перевірену роль до створення layout, незалежно від запиту header.
      tap((profile) => {
        this.policy.userRole.set(profile.role);
        this.policy.refresh();
      }),
      map(() => true),
      catchError(() => {
        this.policy.userRole.set('viewer');
        return of(this.router.createUrlTree(['/login']));
      })
    );
  }
}
