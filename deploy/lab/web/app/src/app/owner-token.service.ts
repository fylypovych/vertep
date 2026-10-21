import { Injectable, signal } from '@angular/core';

/** Owner token kept in memory only — never persisted by the panel. */
@Injectable({ providedIn: 'root' })
export class OwnerTokenService {
  readonly token = signal('');

  headers(): Record<string, string> {
    const value = this.token().trim();
    return value ? { 'X-Lab-Owner-Token': value } : {};
  }

  set(value: string): void {
    this.token.set(value.trim());
  }
}
