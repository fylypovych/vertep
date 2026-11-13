import { Injectable } from '@angular/core';
import {
  HttpContextToken,
  HttpEvent,
  HttpHandler,
  HttpInterceptor,
  HttpRequest,
} from '@angular/common/http';
import { Observable } from 'rxjs';
import { finalize } from 'rxjs/operators';
import { LoadingService } from '../services/loading.service';

/**
 * Marks a request that must block the whole interface while it runs (#458).
 *
 * Off by default: polling, background refresh and ordinary page requests keep their
 * own local loading/error states and never cover the page. Without this, the header's
 * 30-second `/api/status` poll darkened every page with a fullscreen spinner for as
 * long as the slowest request in flight took.
 */
export const BLOCKING_REQUEST = new HttpContextToken<boolean>(() => false);

@Injectable()
export class LoadingInterceptor implements HttpInterceptor {
  constructor(private loadingService: LoadingService) {}

  intercept(
    req: HttpRequest<unknown>,
    next: HttpHandler
  ): Observable<HttpEvent<unknown>> {
    if (!req.context.get(BLOCKING_REQUEST)) {
      return next.handle(req);
    }
    this.loadingService.start();
    // finalize runs on success, error and unsubscribe (cancel, route change): the
    // counter stays balanced and the overlay never outlives its request.
    return next.handle(req).pipe(finalize(() => this.loadingService.stop()));
  }
}
