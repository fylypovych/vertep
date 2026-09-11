import { Component, OnInit, OnDestroy, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Subscription, timer } from 'rxjs';
import { VertepApiService } from '../core/api.service';
import { VertepDatePipe } from '../shared/vertep-date.pipe';
import { LoadingStateComponent } from '../shared/loading-state.component';
import { ErrorStateComponent } from '../shared/error-state.component';
import { HealthCheck, HealthHistoryEntry } from '../core/models';
import { roleLabel } from '../core/presentation';

@Component({
  selector: 'app-health',
  standalone: true,
  imports: [CommonModule, VertepDatePipe, LoadingStateComponent, ErrorStateComponent],
  template: `
    <div class="space-y-6" data-testid="health-page">
      <div class="flex items-center justify-between">
        <h3 class="text-lg font-semibold text-slate-900">Стан системи</h3>
        <a [href]="grafanaUrl()" target="_blank" class="text-sm text-emerald-600 hover:text-emerald-700 font-medium">Grafana</a>
      </div>

      @if (loading()) {
        <app-loading-state />
      } @else if (error()) {
        <app-error-state [message]="error()!" (retry)="loadHealth()" />
      } @else {
        <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
          <div class="bg-slate-50 rounded-lg p-4">
            <p class="text-xs text-slate-500 mb-1">Загальний стан</p>
            <p class="text-sm font-medium" [class.text-emerald-600]="healthOk()" [class.text-red-600]="!healthOk()">{{ healthStatus() }}</p>
          </div>
          <div class="bg-slate-50 rounded-lg p-4">
            <p class="text-xs text-slate-500 mb-1">Сервіс</p>
            <p class="text-sm font-medium text-slate-900">{{ service() }}</p>
          </div>
          <div class="bg-slate-50 rounded-lg p-4">
            <p class="text-xs text-slate-500 mb-1">Активних завдань</p>
            <p class="text-sm font-medium text-slate-900">{{ jobsCount() }}</p>
          </div>
        </div>

        <div class="mb-6">
          <h4 class="text-sm font-medium text-slate-900 mb-2">Перевірки</h4>
          <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
            @for (check of checks(); track check.key) {
              <div class="flex items-center justify-between bg-slate-50 rounded-lg p-3">
                <span class="text-sm text-slate-700">{{ check.key }}</span>
                <div class="flex items-center gap-2">
                  <span class="text-xs text-slate-500">{{ check.value[1] }}</span>
                  <span class="px-2 py-0.5 rounded-full text-xs font-medium"
                        [class.bg-emerald-50]="check.value[0]"
                        [class.text-emerald-700]="check.value[0]"
                        [class.bg-red-50]="!check.value[0]"
                        [class.text-red-700]="!check.value[0]">
                    {{ check.value[0] ? 'OK' : 'FAIL' }}
                  </span>
                </div>
              </div>
            }
          </div>
        </div>

        <div class="mb-6">
          <h4 class="text-sm font-medium text-slate-900 mb-2">Метрики</h4>
          <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
          <div class="bg-slate-50 rounded-lg p-4">
            <p class="text-xs text-slate-500">Всього завдань</p>
            <p class="text-xl font-bold text-slate-900">{{ metrics()['jobs_total'] || 0 }}</p>
          </div>
          <div class="bg-slate-50 rounded-lg p-4">
            <p class="text-xs text-slate-500">Готові до виконання</p>
            <p class="text-xl font-bold text-slate-900">{{ metrics()['queue_ready'] || 0 }}</p>
          </div>
          <div class="bg-slate-50 rounded-lg p-4">
            <p class="text-xs text-slate-500">В обробці</p>
            <p class="text-xl font-bold text-slate-900">{{ metrics()['queue_inflight'] || 0 }}</p>
          </div>
          <div class="bg-slate-50 rounded-lg p-4">
            <p class="text-xs text-slate-500">Воркери онлайн</p>
            <p class="text-xl font-bold text-slate-900">{{ metrics()['workers_online'] || 0 }}</p>
          </div>
          </div>

          @if (jobsByStatus().length > 0) {
            <div class="mt-4">
              <h5 class="text-xs font-medium text-slate-500 mb-2">Завдання за станом</h5>
              <div class="flex flex-wrap gap-2">
                @for (item of jobsByStatus(); track item.status) {
                  <span class="px-2 py-1 rounded-full text-xs"
                        [class.bg-emerald-50]="item.count > 0 && ['READY', 'PUBLISHED'].includes(item.status)"
                        [class.text-emerald-700]="item.count > 0 && ['READY', 'PUBLISHED'].includes(item.status)"
                        [class.bg-red-50]="item.count > 0 && ['FAILED', 'CANCELLED'].includes(item.status)"
                        [class.text-red-700]="item.count > 0 && ['FAILED', 'CANCELLED'].includes(item.status)"
                        [class.bg-slate-100]="!['READY', 'PUBLISHED', 'FAILED', 'CANCELLED'].includes(item.status) || item.count === 0"
                        [class.text-slate-600]="!['READY', 'PUBLISHED', 'FAILED', 'CANCELLED'].includes(item.status) || item.count === 0">
                    {{ item.status }}: {{ item.count }}
                  </span>
                }
              </div>
            </div>
          }
        </div>

        <div>
          <h4 class="text-sm font-medium text-slate-900 mb-2">Історія стану</h4>
          @if (historyLoading()) {
            <div class="animate-pulse bg-slate-100 rounded-lg h-20"></div>
          } @else if (history().length === 0) {
            <p class="text-sm text-slate-500">Немає історії</p>
          } @else {
            <div class="space-y-2">
              @for (entry of history() | slice:0:10; track entry.timestamp) {
                <div class="bg-slate-50 rounded-lg p-3">
                  <div class="flex items-center justify-between">
                    <span class="text-xs text-slate-500">{{ entry.timestamp | vertepDate }}</span>
                    <span class="px-2 py-0.5 rounded-full text-xs font-medium"
                          [class.bg-emerald-50]="entry.status === 'HEALTHY'"
                          [class.text-emerald-700]="entry.status === 'HEALTHY'"
                          [class.bg-red-50]="entry.status !== 'HEALTHY'"
                          [class.text-red-700]="entry.status !== 'HEALTHY'">
                      {{ entry.status }}
                    </span>
                  </div>
                  <div class="mt-1 flex flex-wrap gap-1 text-xs text-slate-500">
                    @for (check of objectEntries(entry.checks); track check.key) {
                      <span class="px-1.5 py-0.5 rounded"
                            [class.bg-emerald-50]="check.value[0]"
                            [class.text-emerald-700]="check.value[0]"
                            [class.bg-red-50]="!check.value[0]"
                            [class.text-red-700]="!check.value[0]">
                        {{ check.key }}: {{ check.value[0] ? 'OK' : 'FAIL' }}
                      </span>
                    }
                  </div>
                </div>
              }
            </div>
          }
        </div>
      }
    </div>
  `,
})
export class HealthComponent implements OnInit, OnDestroy {
  loading = signal(false);
  error = signal<string | null>(null);
  healthStatus = signal<string>('—');
  service = signal<string>('—');
  jobsCount = signal(0);
  checks = signal<Array<{ key: string; value: [boolean, string] }>>([]);
  metrics = signal<Record<string, unknown>>({});
  historyLoading = signal(false);
  history = signal<HealthHistoryEntry[]>([]);
  private pollTimer: Subscription | null = null;

  constructor(private api: VertepApiService) {}

  ngOnInit(): void {
    this.loadHealth();
    this.loadHistory();
    this.pollTimer = timer(0, 10000).subscribe(() => {
      this.loadHealth();
    });
  }

  ngOnDestroy(): void {
    if (this.pollTimer) {
      this.pollTimer.unsubscribe();
    }
  }

  loadHealth(): void {
    this.loading.set(true);
    this.error.set(null);
    this.api.getHealth().subscribe({
      next: (health: HealthCheck) => {
        this.healthStatus.set(health.status);
        this.service.set(health.service || '—');
        this.jobsCount.set(health.jobs || 0);
        this.checks.set(Object.entries(health.checks || {}).map(([key, value]) => ({ key, value: value as [boolean, string] })));
        this.loading.set(false);
      },
      error: (err) => {
        this.error.set(err.message || 'Не вдалося завантажити стан');
        this.loading.set(false);
      },
    });
    this.api.getMetrics().subscribe({
      next: (metrics) => this.metrics.set(metrics),
      error: () => {},
    });
  }

  loadHistory(): void {
    this.historyLoading.set(true);
    this.api.getHealthHistory().subscribe({
      next: (data) => {
        this.history.set((data.history || []) as unknown as HealthHistoryEntry[]);
        this.historyLoading.set(false);
      },
      error: () => this.historyLoading.set(false),
    });
  }

  healthOk(): boolean {
    return this.healthStatus() === 'HEALTHY' || this.healthStatus() === 'OK';
  }

  grafanaUrl(): string {
    const envUrl = (window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1')
      ? 'http://localhost:3000'
      : `https://${window.location.hostname}:3001`;
    return envUrl;
  }

  jobsByStatus(): Array<{ status: string; count: number }> {
    const m = this.metrics() as { jobs_by_status?: Record<string, number> };
    const byStatus = m?.jobs_by_status || {};
    return Object.entries(byStatus).map(([status, count]) => ({ status, count }));
  }

  objectEntries(obj: Record<string, unknown>): Array<{ key: string; value: [boolean, string] }> {
    return Object.entries(obj || {}).map(([key, value]) => ({ key, value: value as [boolean, string] }));
  }
}
