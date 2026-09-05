import { Injectable, Inject } from '@angular/core';
import { CanActivate, Router, UrlTree } from '@angular/router';
import { Observable, of } from 'rxjs';
import { catchError, map } from 'rxjs/operators';
import { VertepApiService } from './api.service';

@Injectable()
export class AuthGuard implements CanActivate {
  constructor(@Inject(VertepApiService) private api: VertepApiService, private router: Router) {}

  canActivate(): Observable<boolean | UrlTree> {
    return this.api.getStatus().pipe(
      map(() => true),
      catchError(() => of(this.router.createUrlTree(['/login'])))
    );
  }
}
