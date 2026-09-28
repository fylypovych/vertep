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
      // Issue #75 S1: getUserProfile rejects a missing/unknown role, so reaching
      // this point means the server confirmed a complete identity. Меню отримує
      // перевірену роль до створення layout, незалежно від запиту header.
      tap((profile) => {
        this.policy.userRole.set(profile.role);
        this.policy.refresh();
      }),
      map(() => true),
      catchError((err) => {
        // Якщо backend повертає, що система вже налаштована, не треба редиректити на login
        if (err && err.error && err.error.configured) {
          // Система вже налаштована – перейти на головну сторінку
          return of(this.router.createUrlTree(['/']));
        }
        this.policy.userRole.set('viewer');
        return of(this.router.createUrlTree(['/login']));
      })
    );
  }
}
