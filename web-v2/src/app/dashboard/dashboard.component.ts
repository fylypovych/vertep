import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterModule } from '@angular/router';
import { VertepApiService } from '../core/api.service';
import { Worker, Job } from '../core/models';
import { JOB_STATUS_GROUPS, roleLabel, workerStatusLabel, computeJobStatistics } from '../core/presentation';
import { LoadingStateComponent } from '../shared/loading-state.component';
import { ErrorStateComponent } from '../shared/error-state.component';

@Component({
  selector: 'app-dashboard',
  standalone: true,
  imports: [CommonModule, RouterModule, LoadingStateComponent, ErrorStateComponent],
  template: `
    <div class="space-y-6" data-testid="dashboard">
      @if (loading()) {
        <app-loading-state />
      } @else if (error) {
        <app-error-state [message]="error" (retry)="loadData()" />
      } @else {
        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
          <div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="stat-workers">
            <div class="flex items-center justify-between">
              <div>
                <p class="text-sm text-slate-500">Воркери</p>
                <p class="text-2xl font-semibold text-slate-900 mt-1">{{ metric(onlineWorkers) }}</p>
                <p class="text-xs text-slate-400 mt-1">У мережі</p>
              </div>
              <div class="w-12 h-12 bg-emerald-50 rounded-lg flex items-center justify-center text-emerald-600">
                <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z"/></svg>
              </div>
            </div>
          </div>
          <a routerLink="/jobs" [queryParams]="{group:'active'}" aria-label="Відкрити активні завдання" class="block bg-white rounded-xl border border-slate-200 p-5 hover:border-blue-300" data-testid="stat-active-jobs">
            <div class="flex items-center justify-between">
              <div>
                <p class="text-sm text-slate-500">Активні завдання</p>
                <p class="text-2xl font-semibold text-slate-900 mt-1">{{ metric(activeJobs) }}</p>
                <p class="text-xs text-slate-400 mt-1">В процесі</p>
              </div>
              <div class="w-12 h-12 bg-blue-50 rounded-lg flex items-center justify-center text-blue-600">
                <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>
              </div>
            </div>
          </a>
          <a routerLink="/jobs" [queryParams]="{group:'queued'}" aria-label="Відкрити завдання у черзі" class="block bg-white rounded-xl border border-slate-200 p-5 hover:border-amber-300" data-testid="stat-queued-jobs">
            <div class="flex items-center justify-between">
              <div>
                <p class="text-sm text-slate-500">Завдань у черзі</p>
                <p class="text-2xl font-semibold text-slate-900 mt-1">{{ metric(queuedJobs) }}</p>
                <p class="text-xs text-slate-400 mt-1">Очікують</p>
              </div>
              <div class="w-12 h-12 bg-amber-50 rounded-lg flex items-center justify-center text-amber-600">
                <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
              </div>
            </div>
          </a>
          <div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="stat-system-state">
            <div class="flex items-center justify-between">
              <div>
                <p class="text-sm text-slate-500">Стан системи</p>
                <p class="text-2xl font-semibold text-emerald-600 mt-1">{{ systemStateLabel }}</p>
                <p class="text-xs text-slate-400 mt-1">{{ systemReason }}</p>
              </div>
              <div class="w-12 h-12 bg-emerald-50 rounded-lg flex items-center justify-center text-emerald-600">
                <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
              </div>
            </div>
          </div>
        </div>

        <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <div class="bg-white rounded-xl border border-slate-200 p-5">
            <h3 class="text-lg font-semibold text-slate-900 mb-4">Архітектура системи</h3>
            <div class="space-y-2" data-testid="architecture">
              <div class="flex items-center">
                <span class="px-3 py-1.5 bg-slate-900 text-white rounded-lg font-semibold text-sm">CORE</span>
                <span class="ml-2 text-sm text-slate-500">{{ coreModules.join(' / ') || 'Base' }}</span>
              </div>
              @if (architectureItems.length > 0) {
                <div class="ml-4 space-y-1 border-l-2 border-slate-200 pl-4">
                  @for (item of architectureItems; track item.role) {
                    <div class="flex items-center text-sm">
                      <span class="w-2 h-2 rounded-full bg-emerald-400 mr-2"></span>
                      <span class="text-slate-700">{{ item.label }}</span>
                      @if (item.capabilities.length > 0) {
                        <span class="ml-2 text-xs text-slate-400">({{ item.capabilities.slice(0, 3).join(', ') }}{{ item.capabilities.length > 3 ? '...' : '' }})</span>
                      }
                      <span class="ml-auto text-emerald-600 font-medium">{{ item.count }}</span>
                    </div>
                  }
                </div>
              } @else {
                <p class="text-sm text-slate-500 ml-4">Немає підключених вузлів</p>
              }
            </div>
          </div>
          <div class="bg-white rounded-xl border border-slate-200 p-5">
            <h3 class="text-lg font-semibold text-slate-900 mb-4">Статуси завдань</h3>
            <div class="grid grid-cols-2 gap-4" data-testid="job-statuses">
              <div class="text-center">
                <div class="text-3xl font-bold text-blue-600">{{ metric(statusCounts['inProgress']) }}</div>
                <div class="text-sm text-slate-500">В процесі</div>
              </div>
              <div class="text-center">
                <div class="text-3xl font-bold text-amber-600">{{ metric(statusCounts['queued']) }}</div>
                <div class="text-sm text-slate-500">Очікують</div>
              </div>
              <div class="text-center">
                <div class="text-3xl font-bold text-emerald-600">{{ metric(statusCounts['completed']) }}</div>
                <div class="text-sm text-slate-500">Завершено</div>
              </div>
              <div class="text-center">
                <div class="text-3xl font-bold text-red-600">{{ metric(statusCounts['failed']) }}</div>
                <div class="text-sm text-slate-500">Помилки</div>
              </div>
            </div>
            <div class="grid grid-cols-3 gap-4 mt-4">
              <div class="text-center">
                <div class="text-2xl font-bold text-slate-600">{{ metric(statusCounts['paused']) }}</div>
                <div class="text-xs text-slate-500">Призупинено</div>
              </div>
              <div class="text-center">
                <div class="text-2xl font-bold text-slate-600">{{ metric(statusCounts['cancelled']) }}</div>
                <div class="text-xs text-slate-500">Скасовано</div>
              </div>
              <div class="text-center">
                <div class="text-2xl font-bold text-slate-600">{{ metric(statusCounts['waiting']) }}</div>
                <div class="text-xs text-slate-500">Очікують систему</div>
              </div>
            </div>
          </div>
        </div>

        <div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="resources">
          <h3 class="text-lg font-semibold text-slate-900 mb-4">Ресурси системи</h3>
          @if (resourcesAvailable) {
            <div class="space-y-4">
              @for (resource of resources; track resource.label) {
                <div>
                  <div class="flex justify-between text-sm mb-1">
                    <span class="text-slate-600">{{ resource.label }}</span>
                    <span class="text-slate-900 font-medium">{{ resource.value == null ? 'Немає даних' : resource.value + '%' }}</span>
                  </div>
                  <div class="w-full bg-slate-100 rounded-full h-2">
                    @if (resource.value != null) {
                      <div class="h-2 rounded-full" [class]="resource.color" [style.width.%]="resource.value"></div>
                    }
                  </div>
                </div>
              }
            </div>
          } @else {
            <div class="text-center py-6 text-slate-400" data-testid="resources-empty">
              <svg class="w-8 h-8 mx-auto mb-2 text-slate-300" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"/></svg>
              <p class="text-sm">Немає даних про ресурси</p>
              <p class="text-xs text-slate-300 mt-1">Дані з'являться після підключення вузлів</p>
            </div>
          }
        </div>

        <div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="workers-table-section">
          <h3 class="text-lg font-semibold text-slate-900 mb-4">Воркери</h3>
          @if (workers.length > 0) {
            <div class="overflow-x-auto">
              <table class="w-full text-sm text-left">
                <thead class="text-xs text-slate-500 uppercase bg-slate-50">
                  <tr>
                    <th class="px-4 py-3">Назва</th>
                    <th class="px-4 py-3">Роль</th>
                    <th class="px-4 py-3">Можливості</th>
                    <th class="px-4 py-3">Статус</th>
                    <th class="px-4 py-3">Навантаження</th>
                    <th class="px-4 py-3">Дії</th>
                  </tr>
                </thead>
                <tbody>
                  <tr *ngFor="let worker of workers" class="border-t border-slate-100">
                    <td class="px-4 py-3">
                      <div class="font-medium text-slate-900">{{ worker.node_name }}</div>
                      <div class="text-xs text-slate-500">{{ worker.node_id }}</div>
                    </td>
                    <td class="px-4 py-3">{{ roleLabel(worker.role) }}</td>
                    <td class="px-4 py-3 text-xs text-slate-600">{{ worker.capabilities ? worker.capabilities.join(', ') : '-' }}</td>
                    <td class="px-4 py-3">
                      <span class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium"
                        [class.bg-emerald-50]="['READY', 'ONLINE', 'FREE'].includes(worker.status)"
                        [class.text-emerald-700]="['READY', 'ONLINE', 'FREE'].includes(worker.status)"
                        [class.bg-slate-100]="!['READY', 'ONLINE', 'FREE'].includes(worker.status)"
                        [class.text-slate-600]="!['READY', 'ONLINE', 'FREE'].includes(worker.status)">
                        <span class="w-1.5 h-1.5 rounded-full"
                          [class.bg-emerald-500]="['READY', 'ONLINE', 'FREE'].includes(worker.status)"
                          [class.bg-slate-400]="!['READY', 'ONLINE', 'FREE'].includes(worker.status)"></span>
                        {{ workerStatusLabel(worker.status) }}
                      </span>
                    </td>
                    <td class="px-4 py-3">{{ worker.gpu_load ?? worker.cpu_load == null ? 'Немає даних' : (worker.gpu_load ?? worker.cpu_load) + '%' }}{{ worker.temperature ? ' · ' + worker.temperature + '°C' : '' }}</td>
                    <td class="px-4 py-3">
                      <a routerLink="/workers" class="text-emerald-600 hover:text-emerald-700 text-sm font-medium">Налаштування</a>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          } @else {
            <p class="text-sm text-slate-500">Немає зареєстрованих воркерів</p>
          }
        </div>
      }
    </div>
  `,
})
export class DashboardComponent implements OnInit {
  loading = signal(true);
  error: string | null = null;
  onlineWorkers: number | null = null;
  activeJobs: number | null = null;
  queuedJobs: number | null = null;
  systemState = 'NORMAL';
  systemReason = 'Штатний режим';
  coreModules: string[] = [];
  architectureItems: { role: string; label: string; count: number; capabilities: string[] }[] = [];
  statusCounts: Record<string, number | null> = { inProgress: null, queued: null, completed: null, failed: null, paused: null, cancelled: null, waiting: null };
  resources: { label: string; value: number | null; color: string }[] = [];
  resourcesAvailable = false;
  workers: Worker[] = [];

  constructor(private api: VertepApiService) {}

  ngOnInit(): void {
    this.loadData();
  }

  loadData(): void {
    this.loading.set(true);
    this.error = null;

    this.api.getStatus().subscribe({
      next: (status) => {
        this.systemState = status.system?.state || 'NORMAL';
        this.systemReason = status.system?.reason || 'Штатний режим';
        if (status.providers) {
          const modules: string[] = [];
          const providers = status.providers as Record<string, { configured?: boolean }>;
          if (providers['llm']?.configured) modules.push('LLM');
          if (providers['tts']?.configured) modules.push('TTS');
          if (providers['compute']?.configured) modules.push('GPU');
          if (providers['assembly']?.configured) modules.push('FFmpeg');
          if (status.telegram) modules.push('Telegram');
          this.coreModules = modules;
        }
        if (status.resources && (status.resources.cpu !== undefined || status.resources.ram !== undefined || status.resources.disk !== undefined)) {
          this.resourcesAvailable = true;
          this.resources = [
            { label: 'CPU', value: status.resources.cpu ?? null, color: 'bg-emerald-500' },
            { label: 'RAM', value: status.resources.ram ?? null, color: 'bg-blue-500' },
            { label: 'Диск', value: status.resources.disk ?? null, color: 'bg-amber-500' },
          ];
        }
      },
      error: (err) => {
        this.error = err.message || 'Не вдалося завантажити дані системи';
      },
    });

    this.api.getWorkers().subscribe({
      next: (workers) => {
        this.workers = workers;
        this.onlineWorkers = workers.filter(w => ['READY', 'ONLINE', 'FREE'].includes(w.status)).length;
        const groups: Record<string, { count: number; capabilities: Set<string> }> = {};
        workers.filter(w => ['READY', 'ONLINE', 'FREE'].includes(w.status)).forEach(w => {
          if (!groups[w.role]) {
            groups[w.role] = { count: 0, capabilities: new Set() };
          }
          groups[w.role].count++;
          (w.capabilities || []).forEach(c => groups[w.role].capabilities.add(c));
        });
        this.architectureItems = Object.entries(groups).map(([role, data]) => ({
          role,
          label: roleLabel(role),
          count: data.count,
          capabilities: Array.from(data.capabilities),
        }));
        this.loading.set(false);
      },
      error: (err) => {
        this.error = err.message || 'Не вдалося завантажити воркери';
        this.loading.set(false);
      },
    });

    this.api.getJobs().subscribe({
      next: (jobs) => {
        const stats = computeJobStatistics(jobs);
        this.activeJobs = stats.active;
        this.queuedJobs = stats.queued;
        this.statusCounts = {
          inProgress: stats.active,
          queued: stats.queued,
          completed: stats.completed,
          failed: stats.failed,
          paused: stats.paused,
          cancelled: stats.cancelled,
          waiting: stats.waiting,
        };
      },
      error: () => {},
    });
  }

  get systemStateLabel(): string {
    const labels: Record<string, string> = {
      'OK': 'Працює',
      'HEALTHY': 'Працює',
      'NORMAL': 'Нормальний',
      'FAILED': 'Помилка',
      'ERROR': 'Помилка',
      'OFFLINE': 'Недоступний',
      'MAINTENANCE': 'Обслуговування',
      'UPDATING': 'Оновлення',
      'EMERGENCY': 'Аварія',
    };
    return labels[this.systemState.toUpperCase()] || this.systemState;
  }

  roleLabel(role: string): string { return roleLabel(role); }
  workerStatusLabel(status: string): string { return workerStatusLabel(status); }
  metric(value: number | null): string | number { return value == null ? 'Немає даних' : value; }
}
