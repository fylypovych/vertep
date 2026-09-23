import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { SystemApiService } from '../../core/api/system.api';
import { ToastService } from '../../core/services/toast.service';
import { UpdateStatus, UpdateReadiness, RollingStatus } from '../../core/models';
import { LoadingStateComponent } from '../../shared/loading-state.component';
import { ErrorStateComponent } from '../../shared/error-state.component';

@Component({
  selector: 'app-settings-update',
  standalone: true,
  imports: [CommonModule, LoadingStateComponent, ErrorStateComponent],
  template: `
    <div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-update">
      <h3 class="text-lg font-semibold text-slate-900 mb-4">Оновлення</h3>
      @if (loading()) {
        <app-loading-state />
      } @else if (error()) {
        <app-error-state [message]="error()!" (retry)="loadAll()" />
      } @else if (updateStatus()) {
        <div class="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-2 text-sm mb-4">
          <div class="flex justify-between py-2 border-b border-slate-100">
            <span class="text-slate-500">Поточна версія</span>
            <span class="font-medium text-slate-900">{{ updateStatus()!.current_version || '—' }}</span>
          </div>
          <div class="flex justify-between py-2 border-b border-slate-100">
            <span class="text-slate-500">Доступна версія</span>
            <span class="font-medium text-slate-900">{{ updateStatus()!.available_version || '—' }}</span>
          </div>
          <div class="flex justify-between py-2 border-b border-slate-100">
            <span class="text-slate-500">Стан</span>
            <span class="font-medium text-slate-900">{{ updateStatus()!.state }}</span>
          </div>
          <div class="flex justify-between py-2 border-b border-slate-100">
            <span class="text-slate-500">Фаза</span>
            <span class="font-medium text-slate-900">{{ updateStatus()!.phase || '—' }}</span>
          </div>
          <div class="flex justify-between py-2 border-b border-slate-100">
            <span class="text-slate-500">Оновлення</span>
            <span class="font-medium" [class.text-emerald-600]="updateStatus()!.update_available" [class.text-slate-400]="!updateStatus()!.update_available">
              {{ updateStatus()!.update_available ? 'Доступне' : 'Немає' }}
            </span>
          </div>
        </div>
        <div class="mb-4" data-testid="update-progress" role="status" aria-live="polite">
          <div class="flex justify-between text-xs text-slate-600 mb-1">
            <span>{{ updateStatus()!.message || 'Очікування операції' }}</span>
            <span>{{ updateStatus()!.progress || 0 }}%</span>
          </div>
          <div class="h-2 rounded-full bg-slate-200 overflow-hidden" role="progressbar"
               [attr.aria-valuenow]="updateStatus()!.progress || 0" aria-valuemin="0" aria-valuemax="100">
            <div class="h-full bg-emerald-600 transition-all" [style.width.%]="updateStatus()!.progress || 0"></div>
          </div>
          @if (updateStatus()!.state === 'FAILED' || updateStatus()!.state === 'ROLLED_BACK') {
            <p class="mt-2 text-sm text-amber-700" data-testid="update-recovery-state">
              {{ updateStatus()!.state === 'ROLLED_BACK' ? 'Попередню версію відновлено.' : 'Оновлення зупинено. Перевірте повідомлення та запустіть відновлення.' }}
            </p>
          }
        </div>
        @if (readiness()) {
          <div class="mb-4 p-3 rounded-lg" [class.bg-emerald-50]="readiness()!.ready" [class.bg-amber-50]="!readiness()!.ready">
            <p class="text-sm font-medium" [class.text-emerald-700]="readiness()!.ready" [class.text-amber-700]="!readiness()!.ready">
              {{ readiness()!.ready ? 'Готове до оновлення' : 'Не готове' }}
            </p>
            @if (!readiness()!.ready) {
              <p class="text-xs text-slate-600 mt-1">Активних задач: {{ readiness()!.inflight }}, зайнятих вузлів: {{ readiness()!.busy_workers.length }}</p>
            }
          </div>
        }
        @if (rollingStatus()) {
          <div class="mb-4 text-sm text-slate-600">
            <p>Поетапне оновлення: {{ rollingStatus()!.state || '—' }} ({{ rollingStatus()!.current_batch || 0 }}/{{ rollingStatus()!.total_batches || 0 }})</p>
          </div>
        }
        <div class="flex flex-wrap gap-2">
          <button (click)="installUpdate()" [disabled]="installing()" data-testid="update-install" class="px-3 py-1.5 text-sm bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 disabled:opacity-50">
            {{ installing() ? 'Оновлення...' : 'Оновити' }}
          </button>
          <button (click)="cancelRolling()" data-testid="update-cancel-rolling" class="px-3 py-1.5 text-sm border border-slate-200 rounded-lg hover:bg-slate-50">Скасувати поетапне оновлення</button>
          <button (click)="promoteCanary()" data-testid="update-promote-canary" class="px-3 py-1.5 text-sm border border-slate-200 rounded-lg hover:bg-slate-50">Підтвердити canary</button>
          <button (click)="rollbackCanary()" data-testid="update-rollback-canary" class="px-3 py-1.5 text-sm border border-slate-200 rounded-lg hover:bg-slate-50">Відкотити canary</button>
          <button (click)="recoverToNormal()" data-testid="update-recover-normal" class="px-3 py-1.5 text-sm border border-slate-200 rounded-lg hover:bg-slate-50">Відновити нормальний стан</button>
        </div>
      }
    </div>
  `,
})
export class UpdateSectionComponent implements OnInit {
  updateStatus = signal<UpdateStatus | null>(null);
  readiness = signal<UpdateReadiness | null>(null);
  rollingStatus = signal<RollingStatus | null>(null);
  loading = signal(false);
  error = signal<string | null>(null);
  installing = signal(false);

  constructor(private system: SystemApiService, private toast: ToastService) {}

  ngOnInit(): void { this.loadAll(); }

  loadAll(): void {
    this.loading.set(true);
    this.error.set(null);
    this.system.updateStatus().subscribe({
      next: (s) => { this.updateStatus.set(s); this.loading.set(false); this.loadReadiness(); this.loadRolling(); },
      error: (err) => { this.error.set(err.message); this.loading.set(false); },
    });
  }

  loadReadiness(): void { this.system.readiness().subscribe({ next: (r) => this.readiness.set(r), error: () => {} }); }
  loadRolling(): void { this.system.rollingStatus().subscribe({ next: (s) => this.rollingStatus.set(s), error: () => {} }); }

  installUpdate(): void {
    this.installing.set(true);
    this.system.installUpdate().subscribe({
      next: () => { this.installing.set(false); this.loadAll(); },
      error: (err) => { this.error.set(err.message); this.installing.set(false); },
    });
  }

  cancelRolling(): void { this.system.cancelRolling().subscribe({ next: () => this.loadRolling(), error: (err) => this.error.set(err.message) }); }
  promoteCanary(): void { this.system.promoteCanary().subscribe({ next: () => this.loadRolling(), error: (err) => this.error.set(err.message) }); }
  rollbackCanary(): void { this.system.rollbackCanary().subscribe({ next: () => this.loadRolling(), error: (err) => this.error.set(err.message) }); }
  recoverToNormal(): void { this.system.recoverToNormal().subscribe({ next: () => this.loadAll(), error: (err) => this.error.set(err.message) }); }
}
