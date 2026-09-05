import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { VertepApiService } from '../core/api.service';
import { Worker } from '../core/models';

@Component({
  selector: 'app-dashboard',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="space-y-6">
      @if (loading) {
        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
          @for (_ of [1,2,3,4]; track $index) {
            <div class="animate-pulse bg-slate-100 rounded-xl h-24"></div>
          }
        </div>
        <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
          @for (_ of [1,2]; track $index) {
            <div class="animate-pulse bg-slate-100 rounded-xl h-64"></div>
          }
        </div>
      } @else if (error) {
        <div class="bg-red-50 border border-red-200 rounded-xl p-5">
          <p class="text-red-700">{{ error }}</p>
          <button (click)="loadData()" class="mt-2 text-sm text-red-600 hover:text-red-700 font-medium">Повторити</button>
        </div>
      } @else {
        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
          <div class="bg-white rounded-xl border border-slate-200 p-5">
            <div class="flex items-center justify-between">
              <div>
                <p class="text-sm text-slate-500">Воркери</p>
                <p class="text-2xl font-semibold text-slate-900 mt-1">{{ onlineWorkers }}</p>
                <p class="text-xs text-slate-400 mt-1">У мережі</p>
              </div>
              <div class="w-12 h-12 bg-emerald-50 rounded-lg flex items-center justify-center text-emerald-600">
                <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z"/></svg>
              </div>
            </div>
          </div>
          <div class="bg-white rounded-xl border border-slate-200 p-5">
            <div class="flex items-center justify-between">
              <div>
                <p class="text-sm text-slate-500">Активні завдання</p>
                <p class="text-2xl font-semibold text-slate-900 mt-1">{{ activeJobs }}</p>
                <p class="text-xs text-slate-400 mt-1">В процесі</p>
              </div>
              <div class="w-12 h-12 bg-blue-50 rounded-lg flex items-center justify-center text-blue-600">
                <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>
              </div>
            </div>
          </div>
          <div class="bg-white rounded-xl border border-slate-200 p-5">
            <div class="flex items-center justify-between">
              <div>
                <p class="text-sm text-slate-500">Завдань у черзі</p>
                <p class="text-2xl font-semibold text-slate-900 mt-1">{{ queuedJobs }}</p>
                <p class="text-xs text-slate-400 mt-1">Очікують</p>
              </div>
              <div class="w-12 h-12 bg-amber-50 rounded-lg flex items-center justify-center text-amber-600">
                <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
              </div>
            </div>
          </div>
          <div class="bg-white rounded-xl border border-slate-200 p-5">
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
            <div class="flex flex-col items-center gap-3">
              <div class="px-4 py-2 bg-slate-900 text-white rounded-lg font-semibold">CORE</div>
              <div class="grid grid-cols-3 gap-3 w-full max-w-md">
                <div *ngFor="let item of architectureItems" class="border border-slate-200 rounded-lg p-3 text-center">
                  <div class="text-sm font-medium text-slate-700">{{ item.label }}</div>
                  <div class="text-lg font-semibold text-emerald-600">{{ item.count }}</div>
                </div>
              </div>
            </div>
          </div>
          <div class="bg-white rounded-xl border border-slate-200 p-5">
            <h3 class="text-lg font-semibold text-slate-900 mb-4">Статуси завдань</h3>
            <div class="grid grid-cols-2 gap-4">
              <div class="text-center">
                <div class="text-3xl font-bold text-blue-600">{{ statusCounts.inProgress }}</div>
                <div class="text-sm text-slate-500">В процесі</div>
              </div>
              <div class="text-center">
                <div class="text-3xl font-bold text-amber-600">{{ statusCounts.queued }}</div>
                <div class="text-sm text-slate-500">Очікують</div>
              </div>
              <div class="text-center">
                <div class="text-3xl font-bold text-emerald-600">{{ statusCounts.completed }}</div>
                <div class="text-sm text-slate-500">Завершено</div>
              </div>
              <div class="text-center">
                <div class="text-3xl font-bold text-red-600">{{ statusCounts.failed }}</div>
                <div class="text-sm text-slate-500">Помилки</div>
              </div>
            </div>
            <div class="grid grid-cols-3 gap-4 mt-4">
              <div class="text-center">
                <div class="text-2xl font-bold text-slate-600">{{ statusCounts.paused }}</div>
                <div class="text-xs text-slate-500">Призупинено</div>
              </div>
              <div class="text-center">
                <div class="text-2xl font-bold text-slate-600">{{ statusCounts.cancelled }}</div>
                <div class="text-xs text-slate-500">Скасовано</div>
              </div>
              <div class="text-center">
                <div class="text-2xl font-bold text-slate-600">{{ statusCounts.waiting }}</div>
                <div class="text-xs text-slate-500">Очікують систему</div>
              </div>
            </div>
          </div>
        </div>

        <div class="bg-white rounded-xl border border-slate-200 p-5">
          <h3 class="text-lg font-semibold text-slate-900 mb-4">Ресурси системи</h3>
          <div class="space-y-4">
            <div *ngFor="let resource of resources">
              <div class="flex justify-between text-sm mb-1">
                <span class="text-slate-600">{{ resource.label }}</span>
                <span class="text-slate-900 font-medium">{{ resource.value }}%</span>
              </div>
              <div class="w-full bg-slate-100 rounded-full h-2">
                <div class="h-2 rounded-full" [class]="resource.color" [style.width.%]="resource.value"></div>
              </div>
            </div>
          </div>
        </div>

        <div class="bg-white rounded-xl border border-slate-200 p-5">
          <h3 class="text-lg font-semibold text-slate-900 mb-4">Workers</h3>
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
                  <td class="px-4 py-3">{{ worker.role }}</td>
                  <td class="px-4 py-3 text-xs text-slate-600">{{ worker.capabilities ? worker.capabilities.join(', ') : '-' }}</td>
                  <td class="px-4 py-3">
                    <span class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium"
                      [class.bg-emerald-50]="worker.status === 'ONLINE'"
                      [class.text-emerald-700]="worker.status === 'ONLINE'"
                      [class.bg-slate-100]="worker.status !== 'ONLINE'"
                      [class.text-slate-600]="worker.status !== 'ONLINE'">
                      <span class="w-1.5 h-1.5 rounded-full"
                        [class.bg-emerald-500]="worker.status === 'ONLINE'"
                        [class.bg-slate-400]="worker.status !== 'ONLINE'"></span>
                      {{ worker.status }}
                    </span>
                  </td>
                  <td class="px-4 py-3">{{ worker.load || 0 }}%</td>
                  <td class="px-4 py-3">
                    <a routerLink="/workers" class="text-emerald-600 hover:text-emerald-700 text-sm font-medium">Налаштування</a>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      }
    </div>
  `,
})
export class DashboardComponent implements OnInit {
  loading = true;
  error: string | null = null;
  onlineWorkers = 0;
  activeJobs = 0;
  queuedJobs = 0;
  systemState = 'NORMAL';
  systemReason = 'Штатний режим';
  architectureItems: { role: string; label: string; count: number }[] = [];
  statusCounts = { inProgress: 0, queued: 0, completed: 0, failed: 0, paused: 0, cancelled: 0, waiting: 0 };
  resources = [
    { label: 'CPU', value: 0, color: 'bg-emerald-500' },
    { label: 'RAM', value: 0, color: 'bg-blue-500' },
    { label: 'Диск', value: 0, color: 'bg-amber-500' },
  ];
  workers: Worker[] = [];

  constructor(private api: VertepApiService) {}

  ngOnInit(): void {
    this.loadData();
  }

  loadData(): void {
    this.loading = true;
    this.error = null;

    this.api.getStatus().subscribe({
      next: (status) => {
        this.systemState = status.system?.state || 'NORMAL';
        this.systemReason = status.system?.reason || 'Штатний режим';
        this.statusCounts = {
          inProgress: (status.orchestration?.active_jobs || 0) + (status.queue?.inflight || 0),
          queued: (status.queue?.depth || 0) + (status.scheduler?.pending || 0),
          completed: 0,
          failed: status.queue?.dead_letter || 0,
          paused: 0,
          cancelled: 0,
          waiting: 0,
        };
        this.resources = [
          { label: 'CPU', value: status.resources?.cpu || 0, color: 'bg-emerald-500' },
          { label: 'RAM', value: status.resources?.ram || 0, color: 'bg-blue-500' },
          { label: 'Диск', value: status.resources?.disk || 0, color: 'bg-amber-500' },
        ];
      },
      error: (err) => {
        this.error = err.message || 'Не вдалося завантажити дані системи';
      },
    });

    this.api.getWorkers().subscribe({
      next: (workers) => {
        this.workers = workers;
        this.onlineWorkers = workers.filter(w => w.status === 'ONLINE').length;
        const groups: Record<string, number> = {};
        workers.filter(w => w.status === 'ONLINE').forEach(w => {
          groups[w.role] = (groups[w.role] || 0) + 1;
        });
        this.architectureItems = Object.entries(groups).map(([role, count]) => ({
          role,
          label: this.translateRole(role),
          count,
        }));
        this.loading = false;
      },
      error: (err) => {
        this.error = err.message || 'Не вдалося завантажити воркери';
        this.loading = false;
      },
    });

    this.api.getJobs().subscribe({
      next: (jobs) => {
        this.activeJobs = jobs.filter(j => ['RUNNING', 'SCRIPTING', 'ASSET_GENERATION', 'VIDEO_GENERATION', 'ASSEMBLY'].includes(j.status)).length;
        this.queuedJobs = jobs.filter(j => ['NEW', 'QUEUED', 'PENDING'].includes(j.status)).length;
        this.statusCounts = {
          ...this.statusCounts,
          paused: jobs.filter(j => j.status === 'PAUSED').length,
          cancelled: jobs.filter(j => j.status === 'CANCELLED').length,
          waiting: jobs.filter(j => j.status === 'WAITING_FOR_SYSTEM').length,
        };
      },
      error: () => {
        // Jobs are secondary, don't block dashboard
      },
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

  private translateRole(role: string): string {
    const labels: Record<string, string> = {
      'core': 'CORE',
      'gpu': 'GPU',
      'text': 'Text',
      'voice': 'Voice',
      'publisher': 'Publisher',
      'backup': 'Backup',
      'monitoring': 'Monitoring',
    };
    return labels[role] || role;
  }
}
