import { Component, OnInit, OnDestroy, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterModule } from '@angular/router';
import { Subscription, timer } from 'rxjs';
import { VertepApiService } from '../core/api.service';
import { ToastService } from '../core/services/toast.service';
import { Alert } from '../core/models';
import { VertepDatePipe } from '../shared/vertep-date.pipe';
import { LoadingStateComponent } from '../shared/loading-state.component';
import { ErrorStateComponent } from '../shared/error-state.component';
import { EmptyStateComponent } from '../shared/empty-state.component';

const SEVERITY_LABELS: Record<string, string> = {
  error: 'Помилка', warning: 'Попередження', info: 'Інформація',
};

@Component({
  selector: 'app-alerts',
  standalone: true,
  imports: [CommonModule, RouterModule, VertepDatePipe, LoadingStateComponent, ErrorStateComponent, EmptyStateComponent],
  template: `
    <div class="space-y-4" data-testid="alerts-page">
      <div class="flex items-center justify-between">
        <h3 class="text-lg font-semibold text-slate-900">Алерти</h3>
        <button (click)="loadAlerts()" class="text-sm text-emerald-600 hover:text-emerald-700 font-medium">Оновити</button>
      </div>

      @if (loading()) {
        <app-loading-state />
      } @else if (error()) {
        <app-error-state [message]="error()!" (retry)="loadAlerts()" />
      } @else if (alerts().length === 0) {
        <app-empty-state message="Немає активних алертів" />
      } @else {
        <div class="space-y-2">
          @for (alert of alerts(); track alert.type + '_' + (alert.job_id || alert.node_name || alert.operation_id || $index)) {
            <div class="bg-slate-50 rounded-lg p-3 flex items-start justify-between">
              <div class="flex-1">
                <div class="flex items-center gap-2">
                  <span class="px-2 py-0.5 rounded-full text-xs font-medium"
                        [class.bg-red-50]="alert.severity === 'error'"
                        [class.text-red-700]="alert.severity === 'error'"
                        [class.bg-amber-50]="alert.severity === 'warning'"
                        [class.text-amber-700]="alert.severity === 'warning'"
                        [class.bg-blue-50]="alert.severity === 'info'"
                        [class.text-blue-700]="alert.severity === 'info'">
                    {{ severityLabel(alert.severity) }}
                  </span>
                  <span class="text-sm font-medium text-slate-900">{{ alert.type }}</span>
                </div>
                @if (alert.message) {
                  <p class="text-sm text-slate-600 mt-1">{{ alert.message }}</p>
                }
                <div class="flex flex-wrap gap-3 mt-1 text-xs text-slate-500">
                  @if (alert.job_id) { <span>Job: {{ alert.job_id }}</span> }
                  @if (alert.node_name) { <span>Node: {{ alert.node_name }}</span> }
                  @if (alert.task_id) { <span>Task: {{ alert.task_id }}</span> }
                  @if (alert.state) { <span>Стан: {{ alert.state }}</span> }
                  @if (alert.updated_at) { <span>{{ alert.updated_at | vertepDate }}</span> }
                </div>
              </div>
              @if (alert.job_id) {
                <a [routerLink]="['/jobs', alert.job_id]" class="text-xs text-emerald-600 hover:text-emerald-700 font-medium ml-2">Відкрити</a>
              }
            </div>
          }
        </div>
      }
    </div>
  `,
})
export class AlertsComponent implements OnInit, OnDestroy {
  loading = signal(false);
  error = signal<string | null>(null);
  alerts = signal<Alert[]>([]);
  private pollTimer: Subscription | null = null;

  constructor(private api: VertepApiService, private toast: ToastService) {}

  ngOnInit(): void {
    this.loadAlerts();
    this.pollTimer = timer(0, 10000).subscribe(() => this.loadAlerts());
  }

  ngOnDestroy(): void { if (this.pollTimer) this.pollTimer.unsubscribe(); }

  loadAlerts(): void {
    this.loading.set(true);
    this.error.set(null);
    this.api.getAlerts().subscribe({
      next: (alerts) => { this.alerts.set(alerts); this.loading.set(false); },
      error: (err) => { this.error.set(err.message || 'Не вдалося завантажити алерти'); this.loading.set(false); },
    });
  }

  severityLabel(severity?: string): string { return SEVERITY_LABELS[severity || ''] || severity || '—'; }
}
