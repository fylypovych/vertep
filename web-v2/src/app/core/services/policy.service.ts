import { Injectable, signal, computed, inject } from '@angular/core';
import { SystemApiService } from '../api/system.api';
import { SystemStatus } from '../models';

export type SystemMode = 'NORMAL' | 'MAINTENANCE' | 'UPDATING' | 'RECOVERING' | 'READ_ONLY' | 'EMERGENCY' | 'UNAVAILABLE';
export type UserRole = 'admin' | 'viewer';

export interface PolicyDecision {
  allowed: boolean;
  reason?: string;
}

// Issue #114/#168.3: this table mirrors the server's authoritative
// ``STATE_OPERATION_POLICY`` (core/system_state.py). A UI mutation is disabled
// in exactly those states in which the CORE middleware would answer 423
// ("Operation <op> is blocked by system state"), so the button and the API
// agree. Keep both tables in lock-step.
const BLOCKED_AFTER_NORMAL: SystemMode[] = ['UPDATING', 'RECOVERING', 'READ_ONLY', 'EMERGENCY', 'UNAVAILABLE'];
const BLOCKED_CREATE_JOB: SystemMode[] = ['READ_ONLY', 'EMERGENCY', 'UNAVAILABLE'];
const BLOCKED_BACKUP: SystemMode[] = ['MAINTENANCE', 'UPDATING', 'RECOVERING', 'READ_ONLY', 'EMERGENCY', 'UNAVAILABLE'];
const BLOCKED_RESTORE: SystemMode[] = ['MAINTENANCE', 'UPDATING', 'READ_ONLY', 'EMERGENCY', 'UNAVAILABLE'];
const BLOCKED_RECOVERY: SystemMode[] = ['NORMAL', 'MAINTENANCE', 'UPDATING', 'RECOVERING', 'READ_ONLY', 'UNAVAILABLE'];

const MUTATION_BLOCKED_IN: Record<string, SystemMode[]> = {
  // POST /api/jobs -> "create_job" (also admitted in MAINTENANCE/UPDATING/
  // RECOVERING, where the job is durably created with WAITING_FOR_SYSTEM).
  create_job: BLOCKED_CREATE_JOB,
  // /api/jobs non-create routes -> "mutate_job".
  pause_job: BLOCKED_AFTER_NORMAL,
  resume_job: BLOCKED_AFTER_NORMAL,
  retry_job: BLOCKED_AFTER_NORMAL,
  regenerate_job: BLOCKED_AFTER_NORMAL,
  cancel_job: BLOCKED_AFTER_NORMAL,
  approve_job: BLOCKED_AFTER_NORMAL,
  publish_job: BLOCKED_AFTER_NORMAL,
  delete_job: BLOCKED_AFTER_NORMAL,
  // /api/nodes non-register routes -> "node_control".
  update_worker: BLOCKED_AFTER_NORMAL,
  drain_worker: BLOCKED_AFTER_NORMAL,
  // /api/characters|brands|workflows|channels -> "resource_management".
  create_character: BLOCKED_AFTER_NORMAL,
  update_character: BLOCKED_AFTER_NORMAL,
  delete_character: BLOCKED_AFTER_NORMAL,
  create_brand: BLOCKED_AFTER_NORMAL,
  update_brand: BLOCKED_AFTER_NORMAL,
  delete_brand: BLOCKED_AFTER_NORMAL,
  create_workflow: BLOCKED_AFTER_NORMAL,
  update_workflow: BLOCKED_AFTER_NORMAL,
  delete_workflow: BLOCKED_AFTER_NORMAL,
  create_channel: BLOCKED_AFTER_NORMAL,
  update_channel: BLOCKED_AFTER_NORMAL,
  delete_channel: BLOCKED_AFTER_NORMAL,
  // /api/settings (incl. secrets) -> "configuration".
  update_settings: BLOCKED_AFTER_NORMAL,
  update_secret: BLOCKED_AFTER_NORMAL,
  delete_secret: BLOCKED_AFTER_NORMAL,
  // /api/system/update -> "update".
  start_update: BLOCKED_AFTER_NORMAL,
  // POST /api/system/backups -> "backup" (NORMAL only).
  create_backup: BLOCKED_BACKUP,
  // POST /api/system/backups/<id>/restore -> "restore" (NORMAL/RECOVERING).
  restore_backup: BLOCKED_RESTORE,
  // /api/system/models and /api/models/text -> "model_pull".
  pull_model: BLOCKED_AFTER_NORMAL,
  cancel_pull: BLOCKED_AFTER_NORMAL,
  delete_model: BLOCKED_AFTER_NORMAL,
  // /api/system/recovery -> "recovery" (EMERGENCY only).
  start_recovery: BLOCKED_RECOVERY,
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
  'pull_model', 'cancel_pull', 'delete_model', 'start_recovery',
]);


// Issue #168.3/#114: only these six states are authoritative. Anything else
// (unknown string, missing payload, rejected request) must fail closed.
const KNOWN_MODES: readonly SystemMode[] = [
  'NORMAL', 'MAINTENANCE', 'UPDATING', 'RECOVERING', 'READ_ONLY', 'EMERGENCY',
];

@Injectable({ providedIn: 'root' })
export class PolicyService {
  private systemApi = inject(SystemApiService);
  // Issue #168.3/#114: the pre-load state must not be the permissive NORMAL.
  // Until the first authoritative /api/status answer arrives the policy blocks
  // every guarded mutation instead of optimistically granting it.
  readonly systemState = signal<SystemMode>('UNAVAILABLE');
  readonly userRole = signal<UserRole>('viewer');
  readonly systemReason = signal<string | null>('Стан системи ще не визначено');
  // Distinguishes "still loading" from "loaded as UNAVAILABLE" so the UI can
  // tell a transient wait apart from a real failure.
  readonly stateLoaded = signal(false);

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
        // Issue #168.3/#114: a missing or unknown state is never treated as
        // NORMAL. The server is the only authority; if it does not name a known
        // mode the policy fails closed as UNAVAILABLE.
        const raw = s.system?.state;
        if (!raw) {
          this.systemState.set('UNAVAILABLE');
          this.systemReason.set('Стан системи невідомий');
        } else if (!KNOWN_MODES.includes(raw as SystemMode)) {
          this.systemState.set('UNAVAILABLE');
          this.systemReason.set('Невідомий стан системи');
        } else {
          this.systemState.set(raw as SystemMode);
          this.systemReason.set(s.system?.reason || null);
        }
        this.stateLoaded.set(true);
      },
      error: () => {
        this.systemState.set('UNAVAILABLE');
        this.systemReason.set('Неможливо отримати стан системи');
        this.stateLoaded.set(true);
      },
    });
  }

  can(action: string): PolicyDecision {
    if (this.userRole() === 'viewer' && VIEWER_BLOCKED.has(action)) {
      return { allowed: false, reason: 'Недостатньо прав' };
    }
    // Issue #168.3/#114: while the authoritative state is unknown every guarded
    // mutation stays blocked; a request that never resolves must not grant
    // access by omission.
    if (!this.stateLoaded()) {
      return { allowed: false, reason: 'Стан системи ще не визначено' };
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
