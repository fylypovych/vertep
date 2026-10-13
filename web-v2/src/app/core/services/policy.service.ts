import { Injectable, signal, computed, inject } from '@angular/core';
import { SystemApiService } from '../api/system.api';
import { SystemStatus } from '../models';

export type SystemMode = 'NORMAL' | 'MAINTENANCE' | 'UPDATING' | 'RECOVERING' | 'READ_ONLY' | 'EMERGENCY' | 'UNAVAILABLE';
export type UserRole = 'admin' | 'viewer';

export interface PolicyDecision {
  allowed: boolean;
  reason?: string;
}

const MUTATION_BLOCKED_IN: Record<string, SystemMode[]> = {
  create_job: ['MAINTENANCE', 'UPDATING', 'READ_ONLY', 'EMERGENCY', 'UNAVAILABLE'],
  pause_job: ['READ_ONLY', 'EMERGENCY', 'UNAVAILABLE'],
  resume_job: ['UPDATING', 'EMERGENCY', 'UNAVAILABLE'],
  retry_job: ['UPDATING', 'READ_ONLY', 'EMERGENCY', 'UNAVAILABLE'],
  regenerate_job: ['UPDATING', 'READ_ONLY', 'EMERGENCY', 'UNAVAILABLE'],
  cancel_job: ['EMERGENCY', 'UNAVAILABLE'],
  approve_job: ['READ_ONLY', 'EMERGENCY', 'UNAVAILABLE'],
  publish_job: ['MAINTENANCE', 'UPDATING', 'READ_ONLY', 'EMERGENCY', 'UNAVAILABLE'],
  delete_job: ['READ_ONLY', 'EMERGENCY', 'UNAVAILABLE'],
  update_worker: ['UPDATING', 'EMERGENCY', 'UNAVAILABLE'],
  drain_worker: ['EMERGENCY', 'UNAVAILABLE'],
  create_character: ['READ_ONLY', 'EMERGENCY', 'UNAVAILABLE'],
  update_character: ['READ_ONLY', 'EMERGENCY', 'UNAVAILABLE'],
  delete_character: ['READ_ONLY', 'EMERGENCY', 'UNAVAILABLE'],
  create_brand: ['READ_ONLY', 'EMERGENCY', 'UNAVAILABLE'],
  update_brand: ['READ_ONLY', 'EMERGENCY', 'UNAVAILABLE'],
  delete_brand: ['READ_ONLY', 'EMERGENCY', 'UNAVAILABLE'],
  create_workflow: ['READ_ONLY', 'EMERGENCY', 'UNAVAILABLE'],
  update_workflow: ['READ_ONLY', 'EMERGENCY', 'UNAVAILABLE'],
  delete_workflow: ['READ_ONLY', 'EMERGENCY', 'UNAVAILABLE'],
  create_channel: ['READ_ONLY', 'EMERGENCY', 'UNAVAILABLE'],
  update_channel: ['READ_ONLY', 'EMERGENCY', 'UNAVAILABLE'],
  delete_channel: ['READ_ONLY', 'EMERGENCY', 'UNAVAILABLE'],
  update_settings: ['UPDATING', 'READ_ONLY', 'EMERGENCY', 'UNAVAILABLE'],
  start_update: ['MAINTENANCE', 'UPDATING', 'EMERGENCY', 'UNAVAILABLE'],
  create_backup: ['MAINTENANCE', 'UPDATING', 'READ_ONLY', 'EMERGENCY', 'UNAVAILABLE'],
  restore_backup: ['MAINTENANCE', 'UPDATING', 'READ_ONLY', 'EMERGENCY', 'UNAVAILABLE'],
  delete_secret: ['READ_ONLY', 'EMERGENCY', 'UNAVAILABLE'],
  update_secret: ['READ_ONLY', 'EMERGENCY', 'UNAVAILABLE'],
};

const VIEWER_BLOCKED = new Set([
  'create_job', 'delete_job', 'approve_job', 'publish_job',
  'pause_job', 'resume_job', 'retry_job', 'regenerate_job', 'cancel_job',
  'update_worker', 'drain_worker',
  'create_character', 'update_character', 'delete_character',
  'create_brand', 'update_brand', 'delete_brand',
  'create_workflow', 'update_workflow', 'delete_workflow',
  'create_channel', 'update_channel', 'delete_channel',
  'update_settings', 'start_update',
  'create_backup', 'restore_backup', 'delete_secret', 'update_secret',
]);

@Injectable({ providedIn: 'root' })
export class PolicyService {
  private systemApi = inject(SystemApiService);
  readonly systemState = signal<SystemMode>('NORMAL');
  readonly userRole = signal<UserRole>('viewer');
  readonly systemReason = signal<string | null>(null);

  readonly isNormal = computed(() => this.systemState() === 'NORMAL');
  readonly isReadOnly = computed(() => this.systemState() === 'READ_ONLY');
  readonly isUpdating = computed(() => this.systemState() === 'UPDATING');
  readonly isMaintenance = computed(() => this.systemState() === 'MAINTENANCE');
  readonly isEmergency = computed(() => this.systemState() === 'EMERGENCY');
  readonly isRecovering = computed(() => this.systemState() === 'RECOVERING');
  readonly isUnavailable = computed(() => this.systemState() === 'UNAVAILABLE');

  refresh(): void {
    this.systemApi.status().subscribe({
      next: (s: SystemStatus) => {
        const state = (s.system?.state || 'NORMAL') as SystemMode;
        if (!['NORMAL', 'MAINTENANCE', 'UPDATING', 'RECOVERING', 'READ_ONLY', 'EMERGENCY'].includes(state)) {
          this.systemState.set('UNAVAILABLE');
          this.systemReason.set('Невідомий стан системи');
        } else {
          this.systemState.set(state);
          this.systemReason.set(s.system?.reason || null);
        }
      },
      error: () => {
        this.systemState.set('UNAVAILABLE');
        this.systemReason.set('Неможливо отримати стан системи');
      },
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
