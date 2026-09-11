import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { VertepApiService } from '../../core/api.service';
import { SystemStatus } from '../../core/models';

@Component({
  selector: 'app-settings-system-info',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-system-info">
      <h3 class="text-lg font-semibold text-slate-900 mb-4">Система</h3>
      @if (loading()) {
        <div class="flex items-center justify-center py-8">
          <div class="w-8 h-8 border-4 border-emerald-500 border-t-transparent rounded-full animate-spin"></div>
        </div>
      } @else if (error()) {
        <p class="text-red-600">{{ error() }}</p>
      } @else {
        <div class="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-2" data-testid="system-info">
          <div class="flex justify-between py-2 border-b border-slate-100">
            <span class="text-sm text-slate-500">Стан системи</span>
            <span class="text-sm font-medium" [class.text-emerald-600]="systemOk" [class.text-red-600]="!systemOk">{{ systemStateLabel }}</span>
          </div>
          <div class="flex justify-between py-2 border-b border-slate-100">
            <span class="text-sm text-slate-500">Версія</span>
            <span class="text-sm font-medium text-slate-900">{{ status()?.version || '—' }}</span>
          </div>
          <div class="flex justify-between py-2 border-b border-slate-100">
            <span class="text-sm text-slate-500">Ядро</span>
            <span class="text-sm font-medium" [class.text-emerald-600]="status()?.core === 'OK'" [class.text-red-600]="status()?.core !== 'OK'">{{ status()?.core || '—' }}</span>
          </div>
          <div class="flex justify-between py-2 border-b border-slate-100">
            <span class="text-sm text-slate-500">База даних</span>
            <span class="text-sm font-medium" [class.text-emerald-600]="status()?.postgres === 'OK'" [class.text-red-600]="status()?.postgres !== 'OK'">{{ status()?.postgres || '—' }}</span>
          </div>
          <div class="flex justify-between py-2 border-b border-slate-100">
            <span class="text-sm text-slate-500">Redis</span>
            <span class="text-sm font-medium" [class.text-emerald-600]="status()?.redis === 'OK'" [class.text-red-600]="status()?.redis !== 'OK'">{{ status()?.redis || '—' }}</span>
          </div>
          <div class="flex justify-between py-2 border-b border-slate-100">
            <span class="text-sm text-slate-500">Сховище</span>
            <span class="text-sm font-medium" [class.text-emerald-600]="status()?.storage === 'OK'" [class.text-red-600]="status()?.storage !== 'OK'">{{ status()?.storage || '—' }}</span>
          </div>
          <div class="flex justify-between py-2 border-b border-slate-100">
            <span class="text-sm text-slate-500">Поточна версія</span>
            <p class="text-sm font-medium text-slate-900" data-testid="status-update-current-version">{{ status()?.update?.['current_version'] || '—' }}</p>
          </div>
          <div>
            <span class="text-xs text-slate-500">Доступна версія</span>
            <p class="text-sm font-medium text-slate-900" data-testid="status-update-available-version">{{ status()?.update?.['available_version'] || '—' }}</p>
          </div>
          <div>
            <span class="text-xs text-slate-500">Стан</span>
            <p class="text-sm font-medium text-slate-900">{{ status()?.update?.['state'] || '—' }}</p>
          </div>
          <div>
            <span class="text-xs text-slate-500">Оновлення</span>
            <p class="text-sm font-medium text-slate-900">{{ status()?.update?.['update_available'] ? 'Доступне' : 'Немає' }}</p>
          </div>
        </div>
      }
    </div>
  `,
})
export class SystemInfoSectionComponent implements OnInit {
  status = signal<SystemStatus | null>(null);
  loading = signal(false);
  error = signal<string | null>(null);

  get systemOk(): boolean {
    const s = this.status()?.system?.state?.toUpperCase();
    return s === 'NORMAL' || s === 'OK' || s === 'HEALTHY';
  }

  get systemStateLabel(): string {
    const state = this.status()?.system?.state?.toUpperCase() ?? 'NORMAL';
    const labels: Record<string, string> = {
      NORMAL: 'Нормальний', OK: 'Працює', HEALTHY: 'Працює',
      MAINTENANCE: 'Обслуговування', UPDATING: 'Оновлення',
      EMERGENCY: 'Аварія', FAILED: 'Помилка', ERROR: 'Помилка',
    };
    return labels[state] ?? state;
  }

  constructor(private api: VertepApiService) {}

  ngOnInit(): void {
    this.loading.set(true);
    this.api.getStatus().subscribe({
      next: (s) => { this.status.set(s); this.loading.set(false); },
      error: (err) => { this.error.set(err.message || 'Не вдалося завантажити статус'); this.loading.set(false); },
    });
  }
}
