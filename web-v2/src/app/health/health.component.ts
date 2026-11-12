import { TPipe } from '../core/i18n/t.pipe';
import { Component, OnInit, OnDestroy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Subscription, timer } from 'rxjs';
import { SystemApiService } from '../core/api/system.api';
import { VertepDatePipe } from '../shared/vertep-date.pipe';
import { LoadingStateComponent } from '../shared/loading-state.component';
import { ErrorStateComponent } from '../shared/error-state.component';
import { HealthCheck, HealthHistoryEntry, MetricsResponse } from '../core/models';
import { checkName, checkReason, checkStatus, healthLabel, serviceChecks, stateChanges } from './health.presentation';
import { RemoteState } from '../core/state/remote-state';

@Component({
  selector: 'app-health',
  standalone: true,
  imports: [TPipe, CommonModule, VertepDatePipe, LoadingStateComponent, ErrorStateComponent],
  template: `
    <div class="space-y-6" data-testid="health-page">
      <div class="flex items-center justify-between">
        <h3 class="text-lg font-semibold text-slate-900">{{ 'Стан системи' | t }}</h3>
        <a [href]="grafanaUrl()" target="_blank" class="text-sm text-emerald-600 hover:text-emerald-700 font-medium">Grafana</a>
      </div>

      @if (healthState.loading() && !healthState.data()) {
        <app-loading-state />
      } @else if (healthState.failed()) {
        <app-error-state [message]="healthState.error()!" (retry)="loadHealth()" />
      } @else {
        <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
          <div class="bg-slate-50 rounded-lg p-4">
            <p class="text-xs text-slate-500 mb-1">{{ 'Загальний стан' | t }}</p>
            <p class="text-sm font-medium" [class.text-emerald-600]="healthOk()" [class.text-red-600]="!healthOk()">{{ healthLabel(healthStatus()) | t }}</p>
          </div>
          <div class="bg-slate-50 rounded-lg p-4">
            <p class="text-xs text-slate-500 mb-1">{{ 'Сервіс' | t }}</p>
            <p class="text-sm font-medium text-slate-900">{{ (service() === 'core' ? 'Основний вузол' : service()) | t }}</p>
          </div>
          <div class="bg-slate-50 rounded-lg p-4">
            <p class="text-xs text-slate-500 mb-1">{{ 'Активних завдань' | t }}</p>
            <p class="text-sm font-medium text-slate-900">{{ jobsCount() }}</p>
          </div>
        </div>

        <div class="mb-6">
          <h4 class="text-sm font-medium text-slate-900 mb-2">{{ 'Перевірки' | t }}</h4>
          <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
            @for (check of checks(); track check.key) {
              <div class="flex items-center justify-between bg-slate-50 rounded-lg p-3">
                <span class="text-sm text-slate-700">{{ checkName(check.key) | t }}</span>
                <div class="flex items-center gap-2">
                  <span class="text-xs text-slate-500" [attr.title]="check.value[1]">{{ checkReason(check.value[1]) | t }}</span>
                  @if (check.required === false && check.value[0] !== null) { <span class="text-xs text-slate-500">{{ 'Необов’язкова перевірка' | t }}</span> }
                  <span class="px-2 py-0.5 rounded-full text-xs font-medium"
                        [class.bg-emerald-50]="check.value[0] === true"
                        [class.text-emerald-700]="check.value[0] === true"
                        [class.bg-red-50]="check.value[0] === false"
                        [class.text-red-700]="check.value[0] === false">
                    {{ checkStatus(check.value[0]) | t }}
                  </span>
                </div>
              </div>
            }
          </div>
        </div>

        <div class="mb-6">
          <h4 class="text-sm font-medium text-slate-900 mb-2">{{ 'Метрики' | t }}</h4>
          <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
            <div class="bg-slate-50 rounded-lg p-4">
              <p class="text-xs text-slate-500">{{ 'Всього завдань' | t }}</p>
              <p class="text-xl font-bold text-slate-900">{{ metrics()['jobs_total'] || 0 }}</p>
            </div>
            <div class="bg-slate-50 rounded-lg p-4">
              <p class="text-xs text-slate-500">{{ 'Готові до виконання' | t }}</p>
              <p class="text-xl font-bold text-slate-900">{{ metrics()['queue_ready'] || 0 }}</p>
            </div>
            <div class="bg-slate-50 rounded-lg p-4">
              <p class="text-xs text-slate-500">{{ 'В обробці' | t }}</p>
              <p class="text-xl font-bold text-slate-900">{{ metrics()['queue_inflight'] || 0 }}</p>
            </div>
            <div class="bg-slate-50 rounded-lg p-4">
              <p class="text-xs text-slate-500">{{ 'Воркери онлайн' | t }}</p>
              <p class="text-xl font-bold text-slate-900">{{ metrics()['workers_online'] || 0 }}</p>
            </div>
          </div>

          @if (jobsByStatus().length > 0) {
            <div class="mt-4">
              <h5 class="text-xs font-medium text-slate-500 mb-2">{{ 'Завдання за станом' | t }}</h5>
              <div class="flex flex-wrap gap-2">
                @for (item of jobsByStatus(); track item.status) {
                  <span class="px-2 py-1 rounded-full text-xs"
                        [class.bg-emerald-50]="item.count > 0 && ['READY', 'PUBLISHED'].includes(item.status)"
                        [class.text-emerald-700]="item.count > 0 && ['READY', 'PUBLISHED'].includes(item.status)"
                        [class.bg-red-50]="item.count > 0 && ['FAILED', 'CANCELLED'].includes(item.status)"
                        [class.text-red-700]="item.count > 0 && ['FAILED', 'CANCELLED'].includes(item.status)"
                        [class.bg-slate-100]="!['READY', 'PUBLISHED', 'FAILED', 'CANCELLED'].includes(item.status) || item.count === 0"
                        [class.text-slate-600]="!['READY', 'PUBLISHED', 'FAILED', 'CANCELLED'].includes(item.status) || item.count === 0">
                    {{ (item.status) | t }}: {{ item.count }}
                  </span>
                }
              </div>
            </div>
          }
        </div>

        <div>
          <h4 class="text-sm font-medium text-slate-900 mb-2">{{ 'Несправності за останню добу' | t }}</h4>
          @if (historyState.loading() && !historyState.data().length) {
            <div class="animate-pulse bg-slate-100 rounded-lg h-20"></div>
          } @else if (historyState.failed()) {
            <app-error-state [message]="historyState.error()!" (retry)="loadHistory()" />
          } @else if (historyState.data().length === 0) {
            <p class="text-sm text-slate-500">{{ 'Несправностей не зафіксовано' | t }}</p>
          } @else {
            <div class="space-y-2">
              @for (entry of historyChanges() | slice:0:10; track entry.timestamp) {
                <div class="bg-slate-50 rounded-lg p-3">
                  <div class="flex items-center justify-between">
                    <span class="text-xs text-slate-500">{{ entry.timestamp | vertepDate }}</span>
                    <span class="px-2 py-0.5 rounded-full text-xs font-medium"
                          [class.bg-emerald-50]="entry.status === 'HEALTHY'"
                          [class.text-emerald-700]="entry.status === 'HEALTHY'"
                          [class.bg-red-50]="entry.status !== 'HEALTHY'"
                          [class.text-red-700]="entry.status !== 'HEALTHY'">
                      {{ healthLabel(entry.status) | t }}
                    </span>
                  </div>
                  <div class="mt-1 flex flex-wrap gap-1 text-xs text-slate-500">
                    @for (check of objectEntries(entry.checks); track check.key) {
                      <span class="px-1.5 py-0.5 rounded" [attr.title]="check.value[1]"
                            [class.bg-emerald-50]="check.value[0] === true"
                            [class.text-emerald-700]="check.value[0] === true"
                            [class.bg-red-50]="check.value[0] === false"
                            [class.text-red-700]="check.value[0] === false">
                        {{ checkName(check.key) | t }}: {{ checkStatus(check.value[0]) | t }}
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
      this.loadHistory();
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
      // Unhealthy incidents of the current and previous day; repeated polling is folded.
      this.systemApi.healthHistory(100, 10).subscribe({
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

  checkName = checkName;
  checkReason = checkReason;
  checkStatus = checkStatus;
  healthLabel = healthLabel;
  historyChanges() { return stateChanges(this.historyState.data()); }
  checks() { return serviceChecks(this.healthState.data()?.checks || {}); }

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

  objectEntries(obj: Record<string, unknown>) { return serviceChecks(obj || {}); }
}
