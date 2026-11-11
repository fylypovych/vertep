import { TPipe } from '../../core/i18n/t.pipe';
import { ChangeDetectorRef, Component, OnInit, OnDestroy, signal } from '@angular/core';
import { Subscription, timer } from 'rxjs';
import { CommonModule } from '@angular/common';
import { WorkersApiService } from '../../core/api/workers.api';
import { ToastService } from '../../core/services/toast.service';
import { SystemRole, SystemRolesResponse } from '../../core/models';
import { LoadingStateComponent } from '../../shared/loading-state.component';
import { ErrorStateComponent } from '../../shared/error-state.component';
import { runtimeProblems, runtimeStatusLabel } from '../../core/presentation';

@Component({
  selector: 'app-settings-roles',
  standalone: true,
  imports: [TPipe, CommonModule, LoadingStateComponent, ErrorStateComponent],
  template: `<div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-roles"><h3 class="text-lg font-semibold mb-4">{{ 'Ролі вузла' | t }}</h3><app-loading-state *ngIf="loading()" /><app-error-state *ngIf="error()" [message]="error()!" /><div *ngIf="rolesResponse"><p class="mb-3 text-sm text-slate-600">{{ 'Застосовані локальні ролі:' | t }} {{ (activeRoles.length ? activeRoles.join(', ') : 'не налаштовано') | t }}</p><p class="mb-3 text-sm text-slate-600">{{ 'Поточна роль:' | t }} <strong>{{ rolesResponse.node_role }}</strong> — <span [attr.data-testid]="'role-status-' + rolesResponse.node_role">{{ (roleStatusText(rolesResponse.node_role)) | t }}</span></p><ul *ngIf="problems(rolesResponse.node_role_evidence).length" class="mb-3 text-xs text-amber-700 list-disc pl-5" data-testid="role-problems-current"><li *ngFor="let problem of problems(rolesResponse.node_role_evidence)">{{ problem.label | t }} {{ problem.detail }}</li></ul><div class="mb-4" data-testid="role-runtime-status-list"><div class="text-sm font-medium text-slate-700 mb-2">{{ 'Стан ролей' | t }}</div><div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4"><div *ngFor="let role of allRoles" class="border border-slate-200 rounded-xl p-4 text-sm text-slate-600"><label class="flex items-center gap-2 font-semibold text-slate-900 mb-2"><input type="checkbox" [disabled]="operationPending() || saving()" [checked]="selectedRoles.includes(role.id)" (change)="toggleRole(role.id, $event)">{{ (role.label) | t }}</label><span [attr.data-testid]="'role-status-' + role.id" [class]="tileClass(role)">{{ (tileText(role)) | t }}</span><ul *ngIf="role.runtime_status === 'DEGRADED' && problems(role.runtime_evidence).length" class="mt-1 text-xs text-amber-700 list-disc pl-5" [attr.data-testid]="'role-problems-' + role.id"><li *ngFor="let problem of problems(role.runtime_evidence)">{{ problem.label | t }} {{ problem.detail }}</li></ul><p *ngIf="!activeRoles.includes(role.id) && role.runtime_evidence?.nodes?.length" class="text-xs text-slate-500">{{ 'Стан віддалених вузлів; локально роль вимкнена' | t }}: {{ statusText(role.runtime_status) | t }}</p><span *ngIf="role.runtime_evidence?.nodes?.length" class="text-xs text-slate-400">{{ 'вузлів:' | t }} {{ role.runtime_evidence!.nodes!.length }}</span></div></div></div><p role="status" data-testid="deployment-state" class="text-sm mt-3">{{ deploymentText() | t }}</p><p *ngIf="rolesResponse.deployment?.['message']" class="text-amber-700">{{ rolesResponse.deployment!['message'] | t }}</p><p *ngIf="rolesResponse.deployment?.error" class="text-red-700" data-testid="deployment-error">{{ 'Помилка застосування ролей' | t }}: {{ rolesResponse.deployment!.error }}</p><p *ngIf="hasChanges()" class="mt-4 text-sm text-amber-700" data-testid="roles-unsaved">{{ 'Є незбережені зміни: натисніть «Зберегти ролі»' | t }}</p><p *ngIf="saveMessage()" role="status" class="mt-4 text-sm text-slate-600" data-testid="roles-save-message">{{ saveMessage()! | t }}</p><button (click)="saveRoles()" [disabled]="saving() || operationPending()" [class.ring-2]="hasChanges()" [class.ring-amber-400]="hasChanges()" class="mt-4 block px-3 py-1.5 text-sm bg-emerald-600 text-white rounded-lg disabled:opacity-50" data-testid="roles-save-button">{{ (saving() ? 'Збереження...' : 'Зберегти ролі') | t }}</button></div></div>`,
})
export class RolesSectionComponent implements OnInit, OnDestroy {
  private poll?: Subscription;
  private fetching = false;
  allRoles: SystemRole[] = [];
  selectedRoles: string[] = [];
  /** Roles the server runs now. */
  activeRoles: string[] = [];
  /** Roles already sent to the system executor but not yet applied. */
  requestedRoles: string[] | null = null;
  rolesResponse: SystemRolesResponse | null = null;
  loading = signal(false);
  error = signal<string | null>(null);
  saving = signal(false);
  saveMessage = signal<string | null>(null);
  saveState = signal<string | null>(null);

  constructor(private workersApi: WorkersApiService, private toast: ToastService, private changeDetector: ChangeDetectorRef) {}

  ngOnInit(): void { this.poll = timer(0, 3000).subscribe(() => this.loadRoles()); }
  ngOnDestroy(): void { this.poll?.unsubscribe(); }
  operationPending(): boolean { return !!this.rolesResponse?.queued || ['QUEUED', 'APPLYING'].includes(this.rolesResponse?.deployment?.state || '') || this.requestedRoles !== null; }
  deploymentText(): string {
    const state = this.rolesResponse?.deployment?.state;
    if (this.rolesResponse?.deployment?.['stalled']) return 'Потрібна перевірка системного виконавця';
    if (state === 'FAILED') return 'Застосування не вдалося';
    if (state === 'SUCCEEDED') return 'Локальне розгортання застосовано; готовність показано окремо';
    const phase = this.rolesResponse?.deployment?.['phase'] as string;
    return ({ QUEUED: 'Очікує системного виконавця', PREPARING: 'Підготовка розгортання', PULLING: 'Завантаження служб', STARTING: 'Запуск служб', VERIFYING: 'Перевірка готовності служб', STOPPING: 'Вимкнення зайвих служб' } as Record<string, string>)[phase] || (this.operationPending() ? 'Застосовується…' : '');
  }

  loadRoles(): void {
    if (this.fetching) return;
    this.fetching = true;
    this.loading.set(!this.rolesResponse);
    this.error.set(null);
    this.workersApi.systemRoles().subscribe({
      next: (resp) => {
        const dirty = this.hasChanges();
        this.rolesResponse = resp;
        this.allRoles = resp.available_roles || [];
        this.activeRoles = resp.active_roles || [];
        if (resp.requested_roles !== undefined) this.requestedRoles = resp.requested_roles;
        if (['FAILED', 'SUCCEEDED'].includes(resp.deployment?.state || '') && !resp.queued) this.requestedRoles = null;
        if (this.requestedRoles && sameRoles(this.requestedRoles, this.activeRoles)) this.requestedRoles = null;
        if (!dirty || this.requestedRoles || resp.deployment?.state === 'FAILED') this.selectedRoles = [...(this.requestedRoles ?? this.activeRoles)];
        this.fetching = false;
        this.loading.set(false);
        this.changeDetector.markForCheck();
      },
      error: (err) => { this.fetching = false; this.error.set(err.message); this.loading.set(false); },
    });
  }

  roleStatus(roleId: string): string {
    return this.rolesResponse?.role_runtime_status?.[roleId]
      || this.allRoles.find((role) => role.id === roleId)?.runtime_status
      || 'UNKNOWN';
  }

  statusText(status?: string): string { return runtimeStatusLabel(status); }

  problems = runtimeProblems;

  /** A role that is neither selected here nor enrolled elsewhere has nothing to measure. */
  roleStatusText(roleId: string): string {
    const status = this.roleStatus(roleId);
    return status === 'UNKNOWN' && !this.selectedRoles.includes(roleId) ? 'Не вибрано' : this.statusText(status);
  }

  /** What a tile says: a pending choice first, then the saved request, then the measured state. */
  tileText(role: SystemRole): string {
    const selected = this.selectedRoles.includes(role.id);
    const saved = (this.requestedRoles ?? this.activeRoles).includes(role.id);
    if (selected !== saved) return selected ? 'Буде увімкнено після збереження' : 'Буде вимкнено після збереження';
    if (this.requestedRoles && this.requestedRoles.includes(role.id) !== this.activeRoles.includes(role.id)) {
      return 'Застосовується…';
    }
    if (!this.activeRoles.includes(role.id)) return 'Локально вимкнено';
    return this.statusText(role.runtime_status || 'UNKNOWN');
  }

  tileClass(role: SystemRole): string {
    const text = this.tileText(role);
    if (text !== this.statusText(role.runtime_status || 'UNKNOWN') && text !== 'Не вибрано') return 'text-amber-700 font-medium';
    return this.statusClass(role.runtime_status);
  }

  hasChanges(): boolean {
    return !sameRoles(this.selectedRoles, this.requestedRoles ?? this.activeRoles);
  }

  statusClass(status?: string): string {
    switch (status) {
      case 'READY': return 'text-emerald-700 font-medium';
      case 'DEGRADED': return 'text-amber-700 font-medium';
      case 'OFFLINE':
      case 'REVOKED': return 'text-red-700 font-medium';
      default: return 'text-slate-500';
    }
  }

  toggleRole(roleId: string, event: Event): void {    const checked = (event.target as HTMLInputElement).checked;
    this.selectedRoles = checked
      ? [...new Set([...this.selectedRoles, roleId])]
      : this.selectedRoles.filter(r => r !== roleId);
  }

  saveRoles(): void {
    if (this.saving() || this.operationPending()) return;
    this.saving.set(true);
    this.saveMessage.set(null);
    this.workersApi.updateSystemRoles(this.selectedRoles).subscribe({
      next: (resp) => {
        this.saving.set(false);
        this.requestedRoles = resp.state === 'QUEUED' ? [...(resp.requested_roles ?? this.selectedRoles)] : null;
        this.saveMessage.set(resp.message || null);
        this.toast.show(resp.state === 'QUEUED' ? 'Ролі збережено; зміни застосовуються' : 'Ролі збережено', 'success');
        this.loadRoles();
      },
      error: (err) => { this.saving.set(false); this.toast.show(err.message || 'Помилка', 'error'); },
    });
  }
}

function sameRoles(left: string[], right: string[]): boolean {
  return left.length === right.length && left.every(role => right.includes(role));
}
