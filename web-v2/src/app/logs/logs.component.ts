import { Component, OnInit, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterModule } from '@angular/router';
import { finalize } from 'rxjs';
import { LogsApiService } from '../core/api/logs.api';
import { LogEntry } from '../core/models';
import { EmptyStateComponent } from '../shared/empty-state.component';
import { ErrorStateComponent } from '../shared/error-state.component';
import { LoadingStateComponent } from '../shared/loading-state.component';
import { VertepDatePipe } from '../shared/vertep-date.pipe';

@Component({
  selector: 'app-logs',
  standalone: true,
  imports: [FormsModule, RouterModule, EmptyStateComponent, ErrorStateComponent, LoadingStateComponent, VertepDatePipe],
  template: `
    <div class="space-y-5" data-testid="logs-page">
      <div class="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <h3 class="text-lg font-semibold text-slate-900">Логи</h3>
          <p class="mt-1 text-sm text-slate-500">Події CORE і підключених вузлів</p>
        </div>

        <form class="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-5" (ngSubmit)="loadLogs()">
          <label class="text-xs text-slate-600">
            Рівень
            <select [(ngModel)]="level" name="level" class="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm">
              <option value="">Усі</option>
              <option value="DEBUG">DEBUG</option>
              <option value="INFO">INFO</option>
              <option value="WARNING">WARNING</option>
              <option value="ERROR">ERROR</option>
              <option value="CRITICAL">CRITICAL</option>
            </select>
          </label>
          <label class="text-xs text-slate-600">
            Job ID
            <input [(ngModel)]="jobId" name="jobId" class="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm" />
          </label>
          <label class="text-xs text-slate-600">
            Вузол
            <input [(ngModel)]="nodeName" name="nodeName" class="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm" />
          </label>
          <label class="text-xs text-slate-600">
            Кількість
            <select [(ngModel)]="limit" name="limit" class="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm">
              <option [ngValue]="100">100</option>
              <option [ngValue]="200">200</option>
              <option [ngValue]="500">500</option>
              <option [ngValue]="1000">1000</option>
            </select>
          </label>
          <button type="submit" class="self-end rounded-lg bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-700">
            Оновити
          </button>
        </form>
      </div>

      @if (loading()) {
        <app-loading-state />
      } @else if (error()) {
        <app-error-state [message]="error()" (retry)="loadLogs()" />
      } @else if (entries().length === 0) {
        <app-empty-state message="Логів не знайдено" />
      } @else {
        <div class="overflow-x-auto rounded-xl border border-slate-200 bg-white" data-testid="logs-table">
          <table class="min-w-full divide-y divide-slate-200 text-left text-sm">
            <thead class="bg-slate-50 text-xs uppercase text-slate-500">
              <tr>
                <th class="px-4 py-3 font-medium">Час</th>
                <th class="px-4 py-3 font-medium">Рівень</th>
                <th class="px-4 py-3 font-medium">Джерело</th>
                <th class="px-4 py-3 font-medium">Повідомлення</th>
                <th class="px-4 py-3 font-medium">Зв'язок</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-100">
              @for (entry of entries(); track entry.timestamp + '_' + entry.logger + '_' + entry.message) {
                <tr class="align-top hover:bg-slate-50">
                  <td class="whitespace-nowrap px-4 py-3 text-xs text-slate-500">{{ entry.timestamp | vertepDate }}</td>
                  <td class="px-4 py-3">
                    <span class="rounded-full px-2 py-1 text-xs font-medium" [class]="levelClass(entry.level)">
                      {{ entry.level }}
                    </span>
                  </td>
                  <td class="px-4 py-3 text-xs text-slate-600">
                    @if (entry.node_name) {
                      <a [routerLink]="['/workers', entry.node_name]" class="block text-emerald-700 hover:underline">{{ entry.node_name }}</a>
                    } @else {
                      <div>{{ entry.logger || 'CORE' }}</div>
                    }
                    @if (entry.actor) { <div>Користувач: {{ entry.actor }}</div> }
                  </td>
                  <td class="min-w-80 px-4 py-3 text-slate-800">
                    <div class="whitespace-pre-wrap break-words">{{ entry.message }}</div>
                    @if (entry.exception) {
                      <pre class="mt-2 max-h-48 overflow-auto whitespace-pre-wrap rounded bg-red-50 p-2 text-xs text-red-800">{{ entry.exception }}</pre>
                    }
                  </td>
                  <td class="px-4 py-3 text-xs">
                    @if (entry.job_id) {
                      <a [routerLink]="['/jobs', entry.job_id]" class="block text-emerald-700 hover:underline">Job {{ entry.job_id }}</a>
                    }
                    @if (entry.action) { <div class="text-slate-500">{{ entry.action }}</div> }
                  </td>
                </tr>
              }
            </tbody>
          </table>
        </div>
        @if (hasMore()) {
          <div class="flex justify-center">
            <button type="button" (click)="loadOlder()" [disabled]="loadingOlder()"
                    data-testid="logs-load-older"
                    class="rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50">
              {{ loadingOlder() ? 'Завантаження…' : 'Показати старіші' }}
            </button>
          </div>
        }
      }
    </div>
  `,
})
export class LogsComponent implements OnInit {
  readonly entries = signal<LogEntry[]>([]);
  readonly loading = signal(false);
  readonly loadingOlder = signal(false);
  readonly hasMore = signal(false);
  readonly error = signal<string | null>(null);

  level = '';
  jobId = '';
  nodeName = '';
  limit = 200;

  constructor(private readonly logsApi: LogsApiService) {}

  ngOnInit(): void {
    this.loadLogs();
  }

  loadLogs(): void {
    this.loading.set(true);
    this.error.set(null);
    this.logsApi.logs(this.query()).pipe(finalize(() => this.loading.set(false))).subscribe({
      next: (entries) => {
        this.entries.set(entries);
        this.hasMore.set(entries.length >= this.limit);
      },
      error: (error) => this.error.set(this.errorMessage(error, 'Не вдалося завантажити логи')),
    });
  }

  loadOlder(): void {
    const current = this.entries();
    if (current.length === 0 || this.loadingOlder()) {
      return;
    }
    const oldest = current[current.length - 1].timestamp;
    this.loadingOlder.set(true);
    this.logsApi.logs({ ...this.query(), before: oldest }).pipe(finalize(() => this.loadingOlder.set(false))).subscribe({
      next: (older) => {
        const seen = new Set(current.map((entry) => `${entry.timestamp}_${entry.logger}_${entry.message}`));
        const appended = older.filter((entry) => !seen.has(`${entry.timestamp}_${entry.logger}_${entry.message}`));
        this.entries.set([...current, ...appended]);
        this.hasMore.set(older.length >= this.limit && appended.length > 0);
      },
      error: (error) => this.error.set(this.errorMessage(error, 'Не вдалося завантажити старіші логи')),
    });
  }

  private errorMessage(error: unknown, fallback: string): string {
    if (typeof error === 'string') {
      return error || fallback;
    }

    const payload = (error as { error?: unknown })?.error;
    if (typeof payload === 'string' && payload.trim()) {
      return payload;
    }
    if (payload && typeof payload === 'object') {
      const detail = (payload as { detail?: unknown }).detail;
      if (typeof detail === 'string' && detail.trim()) {
        return detail;
      }
      const message = (payload as { message?: unknown }).message;
      if (typeof message === 'string' && message.trim()) {
        return message;
      }
    }

    const message = (error as { message?: unknown })?.message;
    if (typeof message === 'string' && message.trim()) {
      return message;
    }

    return fallback;
  }

  private query(): { limit: number; level?: string; job_id?: string; node_name?: string } {
    return {
      limit: this.limit,
      level: this.level || undefined,
      job_id: this.jobId.trim() || undefined,
      node_name: this.nodeName.trim() || undefined,
    };
  }

  levelClass(level: string): string {
    switch (level.toUpperCase()) {
      case 'CRITICAL':
      case 'ERROR':
        return 'bg-red-50 text-red-700';
      case 'WARNING':
        return 'bg-amber-50 text-amber-700';
      case 'DEBUG':
        return 'bg-slate-100 text-slate-600';
      default:
        return 'bg-blue-50 text-blue-700';
    }
  }
}
