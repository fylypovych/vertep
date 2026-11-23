import { Injectable, signal } from '@angular/core';

/**
 * Administrator credentials kept in memory only — never persisted by the panel.
 *
 * #491 L3: after «Увійти через Vertep» CORE redirects back with
 * `#vertep_identity=<assertion>`; the assertion is taken from the fragment, the
 * fragment is removed from the address bar, and every API call carries it as
 * `X-Vertep-Identity`. The LAB service owner token stays the fallback: when it is
 * typed in, it is sent instead.
 */
@Injectable({ providedIn: 'root' })
export class OwnerTokenService {
  readonly token = signal('');
  readonly identity = signal('');

  constructor() {
    const match = /(?:^#|&)vertep_identity=([^&]+)/.exec(window.location.hash);
    if (match) {
      this.identity.set(decodeURIComponent(match[1]));
      history.replaceState(null, '', window.location.pathname + window.location.search);
    }
  }

  headers(): Record<string, string> {
    const value = this.token().trim();
    if (value) {
      return { 'X-Lab-Owner-Token': value };
    }
    const identity = this.identity();
    return identity ? { 'X-Vertep-Identity': identity } : {};
  }

  set(value: string): void {
    this.token.set(value.trim());
  }

  signedIn(): boolean {
    return !!(this.token() || this.identity());
  }
}
