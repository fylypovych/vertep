import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { SystemApiService } from '../../core/api/system.api';
import { SystemStatus } from '../../core/models';
import { LoadingStateComponent } from '../../shared/loading-state.component';
import { ErrorStateComponent } from '../../shared/error-state.component';

@Component({
  selector: 'app-settings-system-info',
  standalone: true,
  imports: [CommonModule, LoadingStateComponent, ErrorStateComponent],
  template: `<div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-system-info"><h3 class="text-lg font-semibold mb-4">Система</h3><app-loading-state *ngIf="loading()" /><app-error-state *ngIf="error()" [message]="error()!" /><div *ngIf="status()" class="grid grid-cols-1 md:grid-cols-2 gap-3" data-testid="system-info"><p>Стан: {{ systemStateLabel }}</p><p>Версія: {{ status()?.version || '-' }}</p><p>Ядро: {{ status()?.core || '-' }}</p><p>База даних: {{ status()?.postgres || '-' }}</p><p>Redis: {{ status()?.redis || '-' }}</p><p>Поточна: <span data-testid="status-update-current-version">{{ status()?.update?.['current_version'] || '-' }}</span></p><p>Доступна: <span data-testid="status-update-available-version">{{ status()?.update?.['available_version'] || '-' }}</span></p></div><table *ngIf="backendsList.length" class="w-full mt-4" data-testid="backends-table"><tbody><tr *ngFor="let entry of backendsList"><td>{{ entry[0] }}</td><td>{{ entry[1]?.['configured'] ? 'Так' : 'Ні' }}</td><td>{{ entry[1]?.['backend'] || '-' }}</td></tr></tbody></table></div>`,
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

  get backendsList(): [string, Record<string, unknown>][] {
    const providers = this.status()?.providers;
    if (!providers) return [];
    return Object.entries(providers as unknown as Record<string, Record<string, unknown>>);
  }

  constructor(private systemApi: SystemApiService) {}

  ngOnInit(): void {
    this.loading.set(true);
    this.systemApi.status().subscribe({
      next: (s) => { this.status.set(s); this.loading.set(false); },
      error: (err) => { this.error.set(err.message || 'Не вдалося завантажити стан'); this.loading.set(false); },
    });
  }
}
