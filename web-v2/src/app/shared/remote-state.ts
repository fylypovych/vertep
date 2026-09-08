import { signal, computed } from '@angular/core';

export type RemotePhase = 'idle' | 'loading' | 'success' | 'error';

export interface RemoteState<T> {
  phase: RemotePhase;
  data: T | null;
  error: string | null;
  lastUpdated: number | null;
}

export function createRemoteState<T>(initial: T | null = null): {
  readonly state: ReturnType<typeof signal<RemoteState<T>>>;
  readonly phase: ReturnType<typeof computed<RemotePhase>>;
  readonly data: ReturnType<typeof computed<T | null>>;
  readonly error: ReturnType<typeof computed<string | null>>;
  readonly loading: ReturnType<typeof computed<boolean>>;
  load: (promise: Promise<T>) => void;
  reset: () => void;
} {
  const state = signal<RemoteState<T>>({ phase: 'idle', data: initial, error: null, lastUpdated: null });
  const phase = computed(() => state().phase);
  const data = computed(() => state().data);
  const error = computed(() => state().error);
  const loading = computed(() => state().phase === 'loading');

  return {
    state, phase, data, error, loading,
    load(promise: Promise<T>) {
      state.set({ phase: 'loading', data: state().data, error: null, lastUpdated: null });
      promise.then(
        (value) => state.set({ phase: 'success', data: value, error: null, lastUpdated: Date.now() }),
        (err: unknown) => state.set({ phase: 'error', data: null, error: (err as Error).message || 'Помилка', lastUpdated: null }),
      );
    },
    reset() {
      state.set({ phase: 'idle', data: initial, error: null, lastUpdated: null });
    },
  };
}

export type MutationPhase = 'idle' | 'running' | 'succeeded' | 'failed';

export interface MutationState {
  phase: MutationPhase;
  error: string | null;
}

export function createMutationState(): {
  readonly state: ReturnType<typeof signal<MutationState>>;
  readonly running: ReturnType<typeof computed<boolean>>;
  readonly error: ReturnType<typeof computed<string | null>>;
  execute: <T>(promise: Promise<T>) => Promise<T | null>;
  reset: () => void;
} {
  const state = signal<MutationState>({ phase: 'idle', error: null });
  const running = computed(() => state().phase === 'running');
  const error = computed(() => state().error);

  return {
    state, running, error,
    async execute<T>(promise: Promise<T>): Promise<T | null> {
      state.set({ phase: 'running', error: null });
      try {
        const result = await promise;
        state.set({ phase: 'succeeded', error: null });
        return result;
      } catch (err: unknown) {
        state.set({ phase: 'failed', error: (err as Error).message || 'Помилка' });
        return null;
      }
    },
    reset() { state.set({ phase: 'idle', error: null }); },
  };
}
