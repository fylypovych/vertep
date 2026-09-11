import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { VertepApiService } from '../../core/api.service';
import { ToastService } from '../../core/services/toast.service';
import { UpdateStatus, UpdateReadiness, RollingStatus } from '../../core/models';

@Component({
  selector: 'app-settings-update',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-update">
      <h3 class="text-lg font-semibold text-slate-900 mb-4">Оновлення</h3>
      @if (loading()) {
        <div class="animate-pulse space-y-2"><div class="h-5 bg-slate-200 rounded w-full"></div></div>
      } @else if (error()) {
        <p class="text-red-600">{{ error() }}</p>
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
            <span class="text-slate-500">Оновлення</span>
            <span class="font-medium" [class.text-emerald-600]="updateStatus()!.update_available" [class.text-slate-400]="!updateStatus()!.update_available">
              {{ updateStatus()!.update_available ? 'Доступне' : 'Немає' }}
            </span>
          </div>
        </div>
        @if (readiness()) {
          <div class="mb-4 p-3 rounded-lg" [class.bg-emerald-50]="readiness()!.ready" [class.bg-amber-50]="!readiness()!.ready">
            <p class="text-sm font-medium" [class.text-emerald-700]="readiness()!.ready" [class.text-amber-700]="!readiness()!.ready">
              {{ readiness()!.ready ? 'Готове до оновлення' : 'Не готове' }}
            </p>
            @if (!readiness()!.ready) {
              <p class="text-xs text-slate-600 mt-1">Активних задач: {{ readiness()!.inflight }}, Busy воркерів: {{ readiness()!.busy_workers.length }}</p>
            }
          </div>
        }
        @if (rollingStatus()) {
          <div class="mb-4 text-sm text-slate-600">
            <p>Rolling: {{ rollingStatus()!.state || '—' }} ({{ rollingStatus()!.current_batch || 0 }}/{{ rollingStatus()!.total_batches || 0 }})</p>
          </div>
        }
        <div class="flex gap-2">
          <button (click)="installUpdate()" [disabled]="installing()" class="px-3 py-1.5 text-sm bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 disabled:opacity-50">
            {{ installing() ? 'Оновлення...' : 'Оновити' }}
          </button>
          <button (click)="cancelRolling()" class="px-3 py-1.5 text-sm border border-slate-200 rounded-lg hover:bg-slate-50">Скасувати rolling</button>
          <button (click)="promoteCanary()" class="px-3 py-1.5 text-sm border border-slate-200 rounded-lg hover:bg-slate-50">Promote canary</button>
          <button (click)="rollbackCanary()" class="px-3 py-1.5 text-sm border border-slate-200 rounded-lg hover:bg-slate-50">Rollback canary</button>
          <button (click)="recoverToNormal()" class="px-3 py-1.5 text-sm border border-slate-200 rounded-lg hover:bg-slate-50">Відновити до NORMAL</button>
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

  constructor(private api: VertepApiService, private toast: ToastService) {}

  ngOnInit(): void { this.loadAll(); }

  loadAll(): void {
    this.loading.set(true);
    this.error.set(null);
    this.api.getUpdateStatus().subscribe({
      next: (s) => { this.updateStatus.set(s); this.loading.set(false); this.loadReadiness(); this.loadRolling(); },
      error: (err) => { this.error.set(err.message); this.loading.set(false); },
    });
  }

  loadReadiness(): void { this.api.getUpdateReadiness().subscribe({ next: (r) => this.readiness.set(r), error: () => {} }); }
  loadRolling(): void { this.api.getRollingStatus().subscribe({ next: (s) => this.rollingStatus.set(s), error: () => {} }); }

  installUpdate(): void {
    this.installing.set(true);
    this.api.installUpdate().subscribe({
      next: () => { this.installing.set(false); this.loadAll(); },
      error: (err) => { this.error.set(err.message); this.installing.set(false); },
    });
  }

  cancelRolling(): void { this.api.cancelRolling().subscribe({ next: () => this.loadRolling(), error: (err) => this.error.set(err.message) }); }
  promoteCanary(): void { this.api.promoteCanary().subscribe({ next: () => this.loadRolling(), error: (err) => this.error.set(err.message) }); }
  rollbackCanary(): void { this.api.rollbackCanary().subscribe({ next: () => this.loadRolling(), error: (err) => this.error.set(err.message) }); }
  recoverToNormal(): void { this.api.recoverToNormal().subscribe({ next: () => this.loadAll(), error: (err) => this.error.set(err.message) }); }
}
