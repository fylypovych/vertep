import { Component, OnInit, computed, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { SystemApiService } from '../../core/api/system.api';
import { SettingsApiService, ProviderMatrixEntry } from '../../core/api/settings.api';
import { ToastService } from '../../core/services/toast.service';
import { SystemStatus } from '../../core/models';
import { LoadingStateComponent } from '../../shared/loading-state.component';
import { ErrorStateComponent } from '../../shared/error-state.component';

@Component({
  selector: 'app-settings-system-info',
  standalone: true,
  imports: [CommonModule, FormsModule, LoadingStateComponent, ErrorStateComponent],
  template: `<div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-system-info"><h3 class="text-lg font-semibold mb-4">Система</h3><app-loading-state *ngIf="loading()" /><app-error-state *ngIf="error()" [message]="error()!" /><div *ngIf="status()" class="grid grid-cols-1 md:grid-cols-2 gap-3" data-testid="system-info"><p>Стан: {{ systemStateLabel }}</p><p>Версія: {{ status()?.version || '-' }}</p><p>Ядро: {{ status()?.core || '-' }}</p><p>База даних: {{ status()?.postgres || '-' }}</p><p>Redis: {{ status()?.redis || '-' }}</p><p>Поточна: <span data-testid="status-update-current-version">{{ status()?.update?.['current_version'] || '-' }}</span></p><p>Доступна: <span data-testid="status-update-available-version">{{ status()?.update?.['available_version'] || '-' }}</span></p></div>
  <table *ngIf="backendsList().length" class="w-full mt-4" data-testid="backends-table">
    <thead><tr class="text-left text-slate-500 text-sm"><th class="py-1">Слот</th><th>Налаштовано</th><th>Бекенд</th><th>Зміна</th></tr></thead>
    <tbody>
      <tr *ngFor="let entry of backendsList(); trackBy: trackSlot" class="border-t" [attr.data-testid]="'backend-row-' + entry[0]">
        <td class="py-1">{{ entry[0] }}</td>
        <td>{{ entry[1]?.['configured'] ? 'Так' : 'Ні' }}</td>
        <td [attr.data-testid]="'backend-current-' + entry[0]">{{ entry[1]?.['backend'] || '-' }}</td>
        <td>
          <ng-container *ngIf="switchable(entry[0], entry[1])">
            <select class="border rounded px-2 py-1 mr-2" [attr.data-testid]="'backend-select-' + entry[0]"
                    [ngModel]="choice(entry[0], entry[1])"
                    (ngModelChange)="selected[entry[0]] = $any($event)">
              <option *ngFor="let option of entry[1]?.['options'] || []" [value]="option">{{ option }}</option>
            </select>
            <button class="px-2 py-1 bg-emerald-600 text-white rounded text-sm"
                    [disabled]="switching() || choice(entry[0], entry[1]) === entry[1]?.['backend']"
                    (click)="switchBackend(entry[0], choice(entry[0], entry[1]))"
                    [attr.data-testid]="'backend-switch-' + entry[0]">Застосувати</button>
          </ng-container>
          <span *ngIf="!switchable(entry[0], entry[1])" class="text-slate-400 text-sm">—</span>
        </td>
      </tr>
    </tbody>
  </table>
  <div *ngIf="switchError()" class="text-red-600 text-sm mt-2" data-testid="backend-switch-error">{{ switchError() }}</div>
</div>`,
})
export class SystemInfoSectionComponent implements OnInit {
  status = signal<SystemStatus | null>(null);
  loading = signal(false);
  error = signal<string | null>(null);
  switching = signal(false);
  switchError = signal<string | null>(null);
  selected: Record<string, string> = {};

  private readonly switchableSlots = new Set(['llm', 'tts', 'compute', 'image', 'video', 'video_engine']);

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

  readonly backendsList = computed<[string, ProviderMatrixEntry][]>(() => {
    const providers = this.status()?.providers;
    if (!providers) return [];
    return Object.entries(providers as unknown as Record<string, ProviderMatrixEntry>);
  });

  trackSlot(_index: number, entry: [string, ProviderMatrixEntry]): string {
    return entry[0];
  }

  constructor(
    private systemApi: SystemApiService,
    private settingsApi: SettingsApiService,
    private toast: ToastService,
  ) {}

  switchable(slot: string, entry: ProviderMatrixEntry | undefined): boolean {
    return !!entry && this.switchableSlots.has(slot) && Array.isArray(entry.options) && entry.options.length > 1;
  }

  choice(slot: string, entry: ProviderMatrixEntry | undefined): string {
    return this.selected[slot] || String(entry?.backend || '');
  }

  switchBackend(slot: string, backend: string): void {
    if (!backend || this.switching()) return;
    this.switching.set(true);
    this.switchError.set(null);
    this.settingsApi.switchProvider(slot, backend).subscribe({
      next: (result) => {
        this.switching.set(false);
        this.selected[slot] = result.backend;
        const status = { ...(this.status() as unknown as Record<string, unknown>) };
        if (result.matrix) status['providers'] = result.matrix;
        this.status.set(status as unknown as SystemStatus);
        this.toast.show(`Слот ${slot}: ${result.backend}`, 'success');
      },
      error: (err) => {
        this.switching.set(false);
        this.switchError.set(err?.error?.detail || err?.message || 'Не вдалося змінити бекенд');
      },
    });
  }

  ngOnInit(): void {
    this.loading.set(true);
    this.systemApi.status().subscribe({
      next: (s) => { this.status.set(s); this.loading.set(false); },
      error: (err) => { this.error.set(err.message || 'Не вдалося завантажити стан'); this.loading.set(false); },
    });
  }
}
