import { Injectable, signal, computed, inject } from '@angular/core';
import { SystemApiService } from '../api/system.api';
import { SystemStatus } from '../models';

export type SystemMode = 'NORMAL' | 'MAINTENANCE' | 'UPDATING' | 'RECOVERING' | 'READ_ONLY' | 'EMERGENCY';
export type UserRole = 'admin' | 'viewer';

export interface PolicyDecision {
  allowed: boolean;
  reason?: string;
}

const MUTATION_BLOCKED_IN: Record<string, SystemMode[]> = {
  create_job: ['MAINTENANCE', 'UPDATING', 'READ_ONLY', 'EMERGENCY'],
  pause_job: ['READ_ONLY', 'EMERGENCY'],
  resume_job: ['UPDATING', 'EMERGENCY'],
  retry_job: ['UPDATING', 'READ_ONLY', 'EMERGENCY'],
  regenerate_job: ['UPDATING', 'READ_ONLY', 'EMERGENCY'],
  cancel_job: ['EMERGENCY'],
  approve_job: ['READ_ONLY', 'EMERGENCY'],
  publish_job: ['MAINTENANCE', 'UPDATING', 'READ_ONLY', 'EMERGENCY'],
  delete_job: ['READ_ONLY', 'EMERGENCY'],
  update_worker: ['UPDATING', 'EMERGENCY'],
  drain_worker: ['EMERGENCY'],
  create_character: ['READ_ONLY', 'EMERGENCY'],
  update_character: ['READ_ONLY', 'EMERGENCY'],
  create_brand: ['READ_ONLY', 'EMERGENCY'],
  update_brand: ['READ_ONLY', 'EMERGENCY'],
  update_settings: ['UPDATING', 'READ_ONLY', 'EMERGENCY'],
  start_update: ['MAINTENANCE', 'UPDATING', 'EMERGENCY'],
};

const VIEWER_BLOCKED = new Set([
  'create_job', 'delete_job', 'approve_job', 'publish_job',
  'update_worker', 'drain_worker', 'create_character', 'update_character',
  'create_brand', 'update_brand', 'update_settings', 'start_update',
  'create_backup', 'restore_backup', 'delete_secret', 'update_secret',
]);

@Injectable({ providedIn: 'root' })
export class PolicyService {
  private systemApi = inject(SystemApiService);
  readonly systemState = signal<SystemMode>('NORMAL');
  readonly userRole = signal<UserRole>('admin');
  readonly systemReason = signal<string | null>(null);

  readonly isNormal = computed(() => this.systemState() === 'NORMAL');
  readonly isReadOnly = computed(() => this.systemState() === 'READ_ONLY');
  readonly isUpdating = computed(() => this.systemState() === 'UPDATING');
  readonly isMaintenance = computed(() => this.systemState() === 'MAINTENANCE');
  readonly isEmergency = computed(() => this.systemState() === 'EMERGENCY');

  refresh(): void {
    this.systemApi.status().subscribe({
      next: (s: SystemStatus) => {
        const state = (s.system?.state || 'NORMAL') as SystemMode;
        this.systemState.set(state);
        this.systemReason.set(s.system?.reason || null);
      },
      error: () => this.systemState.set('NORMAL'),
    });
  }

  can(action: string): PolicyDecision {
    if (this.userRole() === 'viewer' && VIEWER_BLOCKED.has(action)) {
      return { allowed: false, reason: 'Недостатньо прав' };
    }
    const blocked = MUTATION_BLOCKED_IN[action];
    if (blocked && blocked.includes(this.systemState())) {
      return { allowed: false, reason: `Система у стані ${this.systemState()}${this.systemReason() ? ': ' + this.systemReason() : ''}` };
    }
    return { allowed: true };
  }

  disabledReason(action: string): string | null {
    const d = this.can(action);
    return d.allowed ? null : d.reason || null;
  }
}
