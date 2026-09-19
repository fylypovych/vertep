import { Injectable, signal } from '@angular/core';

@Injectable({ providedIn: 'root' })
export class LoadingService {
  private _loading = signal(false);
  loading = this._loading.asReadonly();

  private pendingRequests = 0;

  start(): void {
    if (this.pendingRequests === 0) {
      this._loading.set(true);
    }
    this.pendingRequests++;
  }

  stop(): void {
    this.pendingRequests = Math.max(0, this.pendingRequests - 1);
    if (this.pendingRequests === 0) {
      this._loading.set(false);
    }
  }
}
