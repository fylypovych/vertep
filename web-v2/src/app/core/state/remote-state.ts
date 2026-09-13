import { signal, computed, WritableSignal } from '@angular/core';
import { Observable } from 'rxjs';

/**
 * Canonical phases for a read/load lifecycle.
 */
export type RemotePhase = 'idle' | 'loading' | 'success' | 'error';

// ── RemoteState ─────────────────────────────────────────────────
/**
 * Signal-based remote state for a single async data-fetch.
 *
 * Renders with three well-known signals `phase`, `data`, `error`.
 * Accepts either a Promise or an Observable factory.
 *
 * Usage in a component:
 * ```ts
 * list = new RemoteState<Alert[]>([]);
 * loadAlerts = () => this.list.run(() => this.ops.alerts());
 * ```
 * Template:
 * ```html
 * @if (list.loading()) { <app-loading-state /> }
 * @else if (list.failed()) { <app-error-state [message]="list.error()!" (retry)="loadAlerts()" /> }
 * @else { @for (a of list.data(); track a.type) { ... } }
 * ```
 */
export class RemoteState<T> {
  readonly phase = signal<RemotePhase>('idle');
  readonly data: WritableSignal<T>;
  readonly error: WritableSignal<string | null>;
  readonly retries = signal(0);

  readonly loading = computed(() => this.phase() === 'loading');
  readonly failed  = computed(() => this.phase() === 'error');
  readonly ready   = computed(() => this.phase() === 'success');

  constructor(defaultData: T) {
    this.data  = signal(defaultData);
    this.error = signal(null);
  }

  run(factory: () => Promise<T> | Observable<T>, fallbackMessage = 'Не вдалося завантажити дані'): void {
    this.phase.set('loading');
    this.error.set(null);
    const next = (v: T) => { this.data.set(v); this.phase.set('success'); };
    const fail = (err: unknown) => {
      this.error.set((err as Error)?.message || fallbackMessage);
      this.retries.update(n => n + 1);
      this.phase.set('error');
    };

    let result: Promise<T>;
    const r = factory();
    if (r instanceof Promise) {
      result = r;
    } else {
      result = new Promise<T>((resolve, reject) => {
        r.subscribe({ next: resolve, error: reject });
      });
    }
    result.then(next, fail).catch(fail);
  }

  reset(): void {
    this.phase.set('idle');
    this.retries.set(0);
    this.error.set(null);
  }
}

// ── RemoteMutation ──────────────────────────────────────────────
/**
 * Signal-based helper for a single mutating operation (create/restore/retry/etc.).
 * Supports an optional `onProgress` callback.
 */
export class RemoteMutation {
  readonly pending = signal(false);
  readonly error   = signal<string | null>(null);
  readonly done    = signal(false);
  readonly progress = signal<number | null>(null);
  readonly message = signal<string | null>(null);

  readonly loading = computed(() => this.pending());

  /** Completed-action callbacks registered via `then`; cleared on each `run`. */
  private afterDone: (() => void) | null = null;

  reset(): void {
    this.pending.set(false);
    this.error.set(null);
    this.done.set(false);
    this.progress.set(null);
    this.message.set(null);
    this.afterDone = null;
  }

  /**
   * Register a callback to run once after the current mutation completes
   * successfully. Multiple registrations are combined.
   */
  then(cb: () => void): this {
    this.afterDone = this.afterDone ? () => { this.afterDone?.(); cb(); } : cb;
    return this;
  }

  run(
    factory: () => Promise<unknown> | Observable<unknown>,
    opts?: { onProgress?: (p: number, message?: string) => void; fallbackMessage?: string },
  ): void {
    const after = this.afterDone;
    this.afterDone = null;
    this.reset();
    this.pending.set(true);
    const onNext = (p: number, msg?: string) => {
      this.progress.set(p);
      this.message.set(msg ?? null);
      opts?.onProgress?.(p, msg);
    };
    const finish = () => {
      this.pending.set(false);
      this.done.set(true);
      after?.();
    };
    const fail = (err: unknown) => {
      this.pending.set(false);
      this.error.set((err as Error)?.message || opts?.fallbackMessage || 'Дію не виконано');
    };

    const handle = (obs: Observable<unknown>) => {
      obs.subscribe({
        next: () => finish(),
        error: fail,
      });
    };

    const r = factory();
    if (r instanceof Promise) {
      r.then(() => finish(), fail);
    } else {
      handle(r);
    }
  }
}
