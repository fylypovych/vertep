import { Injectable } from '@angular/core';
import { CanActivate, Router, UrlTree } from '@angular/router';
import { Observable, of } from 'rxjs';
import { catchError, map, switchMap, tap } from 'rxjs/operators';
import { AuthApiService } from './api/auth.api';
import { PolicyService } from './services/policy.service';

@Injectable({ providedIn: 'root' })
export class AdminGuard implements CanActivate {
  constructor(private auth: AuthApiService, private router: Router, private policy: PolicyService) {}

  canActivate(): Observable<boolean | UrlTree> {
    return this.auth.getUserProfile().pipe(
      tap((profile) => { this.policy.userRole.set(profile.role); }),
      map((profile) => profile.role === 'admin'),
      catchError(() => of(this.router.createUrlTree(['/login'])))
    );
  }
}