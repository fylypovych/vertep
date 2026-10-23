import { Component, OnInit, OnDestroy, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Subscription, timer } from 'rxjs';
import { SystemApiService } from '../core/api/system.api';
import { VertepDatePipe } from '../shared/vertep-date.pipe';
import { LoadingStateComponent } from '../shared/loading-state.component';
import { ErrorStateComponent } from '../shared/error-state.component';
import { EmptyStateComponent } from '../shared/empty-state.component';
import { HealthCheck, HealthHistoryEntry, MetricsResponse } from '../core/models';
import { roleLabel } from '../core/presentation';
import { RemoteState } from '../core/state/remote-state';
import { RemoteMutation } from '../core/state/remote-state';

@Component({
  selector: 'app-health',
  standalone: true,
  imports: [CommonModule, VertepDatePipe, LoadingStateComponent, ErrorStateComponent, EmptyStateComponent],
  template: `
    <div class="space-y-6" data-testid="health-page">
      <div class="flex items-center justify-between">
        <h3 class="text-lg font-semibold text-slate-900">Стан системи</h3>
        <a [href]="grafanaUrl()" target="_blank" class="text-sm text-emerald-600 hover:text-emerald-700 font-medium">Grafana</a>
      </div>

      @if (healthState.loading()) {
        <app-loading-state />
      } @else if (healthState.failed()) {
        <app-error-state [message]="healthState.error()!" (retry)="loadHealth()" />
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
          @if (historyState.loading()) {
            <div class="animate-pulse bg-slate-100 rounded-lg h-20"></div>
          } @else if (historyState.failed()) {
            <app-error-state [message]="historyState.error()!" (retry)="loadHistory()" />
          } @else if (historyState.data().length === 0) {
            <p class="text-sm text-slate-500">Немає історії</p>
          } @else {
            <div class="space-y-2">
              @for (entry of historyState.data() | slice:0:10; track entry.timestamp) {
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
  readonly healthState = new RemoteState<HealthCheck | null>(null);
  readonly metricsState = new RemoteState<MetricsResponse>({
    jobs_total: 0, queue_ready: 0, queue_inflight: 0, queue_dead_letter: 0, workers_online: 0, jobs_scheduled: 0, jobs_by_status: {}, scenes_by_status: {}
  });
  readonly historyState = new RemoteState<HealthHistoryEntry[]>([]);

  private pollTimer: Subscription | null = null;

  constructor(private systemApi: SystemApiService) {}

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
    this.healthState.run(() => new Promise<HealthCheck>((resolve, reject) => {
      this.systemApi.health().subscribe({
        next: (health) => resolve(health),
        error: (err) => reject(err),
      });
    }), 'Не вдалося завантажити стан');
    this.systemApi.metrics().subscribe({
      next: (metrics) => this.metricsState.data.set(metrics),
      error: () => {},
    });
  }

  loadHistory(): void {
    this.historyState.run(() => new Promise<HealthHistoryEntry[]>((resolve, reject) => {
      this.systemApi.healthHistory().subscribe({
        next: (data) => resolve((data.history || []) as unknown as HealthHistoryEntry[]),
        error: (err) => reject(err),
      });
    }), 'Не вдалося завантажити історію');
  }

  healthStatus(): string {
    return this.healthState.data()?.status || '—';
  }

  service(): string {
    return this.healthState.data()?.service || '—';
  }

  jobsCount(): number {
    return this.healthState.data()?.jobs || 0;
  }

  checks(): Array<{ key: string; value: [boolean, string] }> {
    const health = this.healthState.data();
    if (!health) return [];
    return Object.entries(health.checks || {}).map(([key, value]) => ({ key, value: value as [boolean, string] }));
  }

  metrics(): MetricsResponse {
    return this.metricsState.data();
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
    const m = this.metricsState.data() as { jobs_by_status?: Record<string, number> };
    const byStatus = m?.jobs_by_status || {};
    return Object.entries(byStatus).map(([status, count]) => ({ status, count }));
  }

  objectEntries(obj: Record<string, unknown>): Array<{ key: string; value: [boolean, string] }> {
    return Object.entries(obj || {}).map(([key, value]) => ({ key, value: value as [boolean, string] }));
  }
}
