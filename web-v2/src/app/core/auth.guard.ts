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
    return this.api.getUserProfile().pipe(
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
