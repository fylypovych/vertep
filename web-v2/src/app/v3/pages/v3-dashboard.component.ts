import { ChangeDetectionStrategy, Component, OnInit, computed, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterModule } from '@angular/router';
import { TPipe } from '../../core/i18n/t.pipe';
import { I18nService } from '../../core/i18n/i18n.service';
import { SystemApiService } from '../../core/api/system.api';
import { WorkersApiService } from '../../core/api/workers.api';
import { JobsApiService } from '../../core/api/jobs.api';
import { Job, Worker } from '../../core/models';
import { computeJobStatistics, percentLabel, roleLabel, workerStatusLabel } from '../../core/presentation';
import { LoadingStateComponent } from '../../shared/loading-state.component';
import { ErrorStateComponent } from '../../shared/error-state.component';
import { KpiStatCardComponent } from '../ui/kpi-stat-card.component';
import { PaperCardComponent } from '../ui/paper-card.component';
import { MemberListItem, MembersListCardComponent } from '../ui/members-list-card.component';

interface ResourceBar {
  label: string;
  value: number | null;
  color: string;
  detail: string | null;
}

function formatGiB(bytes: number, locale: string): string {
  const value = bytes / 1024 ** 3;
  const digits = value < 10 ? 1 : 0;
  return `${value.toLocaleString(locale, { minimumFractionDigits: digits, maximumFractionDigits: digits })} GB`;
}

@Component({
  selector: 'app-v3-dashboard',
  standalone: true,
  imports: [
    CommonModule,
    RouterModule,
    TPipe,
    LoadingStateComponent,
    ErrorStateComponent,
    KpiStatCardComponent,
    PaperCardComponent,
    MembersListCardComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="space-y-6" data-testid="v3-dashboard">
      @if (loading()) {
        <app-loading-state />
      } @else if (error()) {
        <app-error-state [message]="error()!" (retry)="loadData()" />
      } @else {
        <!-- 4 KPI Stat Cards -->
        <div class="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-5">
          <app-v3-kpi-card
            label="Воркери"
            [value]="metric(onlineWorkers())"
            footer="У мережі"
            icon="workers"
            accent="emerald"
            link="/v3/workers"
            testId="v3-stat-workers" />

          <app-v3-kpi-card
            label="Активні завдання"
            [value]="metric(activeJobs())"
            footer="В процесі"
            icon="active"
            accent="sky"
            link="/v3/jobs"
            [queryParams]="{ group: 'active' }"
            testId="v3-stat-active-jobs" />

          <app-v3-kpi-card
            label="Завдань у черзі"
            [value]="metric(queuedJobs())"
            footer="Очікують"
            icon="queue"
            accent="amber"
            link="/v3/jobs"
            [queryParams]="{ tab: 'queue' }"
            testId="v3-stat-queued-jobs" />

          <app-v3-kpi-card
            label="Стан системи"
            [value]="systemStateLabel()"
            [footer]="systemReason()"
            icon="health"
            [accent]="systemOk() ? 'emerald' : 'coral'"
            [valueColor]="systemOk() ? 'var(--v3-accent)' : 'var(--v3-coral)'"
            link="/v3/monitor"
            [queryParams]="{ tab: 'health' }"
            testId="v3-stat-system-state" />
        </div>

        <!-- Middle row: Workers (Team Members pattern) + Job Status & Resources -->
        <div class="grid grid-cols-1 lg:grid-cols-12 gap-6 items-start">
          <div class="lg:col-span-5">
            <app-v3-members-list
              title="Активні вузли"
              subtitle="Підключені воркери кластера"
              emptyText="Немає зареєстрованих воркерів"
              [items]="workerMembers()"
              testId="v3-dashboard-workers" />
          </div>

          <div class="lg:col-span-7 space-y-6">
            <app-v3-paper-card title="Статуси завдань" eyebrow="Конвеєр" testId="v3-dashboard-job-statuses">
              <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 pt-1">
                <div class="rounded-xl p-3 text-center" style="background-color: var(--v3-sky-soft)">
                  <div class="text-2xl font-bold" style="color: var(--v3-sky)">{{ metric(statusCounts().inProgress) | t }}</div>
                  <div class="text-xs mt-1" style="color: var(--v3-text-muted)">{{ 'В процесі' | t }}</div>
                </div>
                <div class="rounded-xl p-3 text-center" style="background-color: var(--v3-amber-soft)">
                  <div class="text-2xl font-bold" style="color: var(--v3-amber)">{{ metric(statusCounts().queued) | t }}</div>
                  <div class="text-xs mt-1" style="color: var(--v3-text-muted)">{{ 'Очікують' | t }}</div>
                </div>
                <div class="rounded-xl p-3 text-center" style="background-color: var(--v3-accent-soft)">
                  <div class="text-2xl font-bold" style="color: var(--v3-accent)">{{ metric(statusCounts().completed) | t }}</div>
                  <div class="text-xs mt-1" style="color: var(--v3-text-muted)">{{ 'Завершено' | t }}</div>
                </div>
                <div class="rounded-xl p-3 text-center" style="background-color: var(--v3-danger-soft)">
                  <div class="text-2xl font-bold" style="color: var(--v3-danger)">{{ metric(statusCounts().failed) | t }}</div>
                  <div class="text-xs mt-1" style="color: var(--v3-text-muted)">{{ 'Помилки' | t }}</div>
                </div>
              </div>
              <div class="grid grid-cols-3 gap-3 mt-3 pt-3 border-t text-center" style="border-color: var(--v3-border)">
                <div>
                  <span class="text-sm font-semibold" style="color: var(--v3-text)">{{ metric(statusCounts().paused) | t }}</span>
                  <span class="block text-[11px]" style="color: var(--v3-text-subtle)">{{ 'Призупинено' | t }}</span>
                </div>
                <div>
                  <span class="text-sm font-semibold" style="color: var(--v3-text)">{{ metric(statusCounts().cancelled) | t }}</span>
                  <span class="block text-[11px]" style="color: var(--v3-text-subtle)">{{ 'Скасовано' | t }}</span>
                </div>
                <div>
                  <span class="text-sm font-semibold" style="color: var(--v3-text)">{{ metric(statusCounts().waiting) | t }}</span>
                  <span class="block text-[11px]" style="color: var(--v3-text-subtle)">{{ 'Очікують систему' | t }}</span>
                </div>
              </div>
            </app-v3-paper-card>

            <app-v3-paper-card title="Ресурси системи" eyebrow="Хост CORE" testId="v3-dashboard-resources">
              @if (resources().length > 0) {
                <div class="space-y-3.5 pt-1">
                  @for (res of resources(); track res.label) {
                    <div>
                      <div class="flex items-center justify-between text-xs mb-1.5">
                        <span class="font-medium" style="color: var(--v3-text-muted)">{{ res.label | t }}</span>
                        <span class="font-semibold" style="color: var(--v3-text)">
                          {{ (res.value == null ? 'Немає даних' : res.value + '%') | t }}
                          @if (res.value != null && res.detail) {
                            <span class="font-normal" style="color: var(--v3-text-subtle)"> ({{ res.detail }})</span>
                          }
                        </span>
                      </div>
                      <div class="w-full h-2 rounded-full overflow-hidden" style="background-color: #efede7">
                        @if (res.value != null) {
                          <div class="h-full rounded-full transition-all" [style.width.%]="res.value" [style.background-color]="res.color"></div>
                        }
                      </div>
                    </div>
                  }
                </div>
              } @else {
                <p class="text-xs py-3" style="color: var(--v3-text-subtle)">{{ 'Немає даних про ресурси' | t }}</p>
              }
            </app-v3-paper-card>
          </div>
        </div>

        <!-- Bottom row: Recent Jobs table -->
        <app-v3-paper-card title="Останні завдання" eyebrow="Студія" [flush]="true" [headerDivider]="true" testId="v3-dashboard-jobs">
          <a card-actions routerLink="/v3/jobs" class="text-xs font-semibold hover:underline" style="color: var(--v3-accent)">
            {{ 'Усі завдання' | t }} →
          </a>

          @if (recentJobs().length === 0) {
            <div class="px-5 py-8 text-center text-sm" style="color: var(--v3-text-subtle)">
              {{ 'Завдань не знайдено' | t }}
            </div>
          } @else {
            <div class="overflow-x-auto">
              <table class="w-full text-sm text-left">
                <thead>
                  <tr class="border-b text-[11px] uppercase tracking-wider" style="border-color: var(--v3-border); color: var(--v3-text-subtle); background-color: var(--v3-surface-subtle)">
                    <th class="px-5 py-3 font-semibold">{{ 'Тема' | t }}</th>
                    <th class="px-5 py-3 font-semibold">{{ 'Персонаж' | t }}</th>
                    <th class="px-5 py-3 font-semibold">{{ 'Статус' | t }}</th>
                    <th class="px-5 py-3 font-semibold text-right">{{ 'Дії' | t }}</th>
                  </tr>
                </thead>
                <tbody class="divide-y" style="border-color: var(--v3-border)">
                  @for (job of recentJobs(); track job.job_id) {
                    <tr class="hover:bg-[#faf9f6] transition-colors" [style.border-color]="'var(--v3-border)'">
                      <td class="px-5 py-3">
                        <div class="font-medium truncate max-w-md" style="color: var(--v3-text)">{{ job.topic || job.job_id }}</div>
                        <div class="text-[11px] font-mono" style="color: var(--v3-text-subtle)">{{ job.job_id }}</div>
                      </td>
                      <td class="px-5 py-3 text-xs font-mono" style="color: var(--v3-text-muted)">{{ job.character_id || '—' }}</td>
                      <td class="px-5 py-3">
                        <span class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium"
                              style="background-color: #efede7; color: var(--v3-text)">
                          {{ job.status }}
                        </span>
                      </td>
                      <td class="px-5 py-3 text-right">
                        <a [routerLink]="['/v3/jobs', job.job_id]" class="text-xs font-semibold hover:underline" style="color: var(--v3-accent)">
                          {{ 'Відкрити' | t }}
                        </a>
                      </td>
                    </tr>
                  }
                </tbody>
              </table>
            </div>
          }
        </app-v3-paper-card>
      }
    </div>
  `,
})
export class V3DashboardComponent implements OnInit {
  private readonly i18n = inject(I18nService);

  loading = signal(true);
  error = signal<string | null>(null);
  workers = signal<Worker[]>([]);
  jobs = signal<Job[]>([]);
  systemState = signal('NORMAL');
  systemReason = signal('Штатний режим');
  resources = signal<ResourceBar[]>([]);

  onlineWorkers = computed(() => this.workers().filter(w => ['READY', 'ONLINE', 'FREE'].includes(w.status)).length);
  activeJobs = computed(() => computeJobStatistics(this.jobs()).active);
  queuedJobs = computed(() => computeJobStatistics(this.jobs()).queued);
  statusCounts = computed(() => {
    const s = computeJobStatistics(this.jobs());
    return {
      inProgress: s.active,
      queued: s.queued,
      completed: s.completed,
      failed: s.failed,
      paused: s.paused,
      cancelled: s.cancelled,
      waiting: s.waiting,
    };
  });

  recentJobs = computed(() => this.jobs().slice(0, 6));

  systemOk = computed(() => ['OK', 'HEALTHY', 'NORMAL'].includes(this.systemState().toUpperCase()));

  systemStateLabel = computed(() => {
    const labels: Record<string, string> = {
      OK: 'Працює',
      HEALTHY: 'Працює',
      NORMAL: 'Нормальний',
      FAILED: 'Помилка',
      ERROR: 'Помилка',
      OFFLINE: 'Недоступний',
      MAINTENANCE: 'Обслуговування',
      UPDATING: 'Оновлення',
      EMERGENCY: 'Аварія',
    };
    return labels[this.systemState().toUpperCase()] || this.systemState();
  });

  workerMembers = computed<MemberListItem[]>(() =>
    this.workers().map(w => {
      const online = ['READY', 'ONLINE', 'FREE'].includes(w.status);
      const load = percentLabel(w.gpu_load ?? w.cpu_load);
      const subtitleParts = [roleLabel(w.role), load ? load : null, w.temperature ? `${w.temperature}°C` : null].filter(Boolean);
      return {
        id: w.node_id || w.node_name,
        name: w.node_name,
        subtitle: subtitleParts.join(' · '),
        statusLabel: workerStatusLabel(w.status),
        statusTone: online ? 'ok' : 'muted',
        badge: w.gpu_name ? 'GPU' : w.role,
        route: ['/v3/workers', w.node_id || w.node_name],
        actionLabel: 'Відкрити',
      };
    }),
  );

  constructor(
    private systemApi: SystemApiService,
    private workersApi: WorkersApiService,
    private jobsApi: JobsApiService,
  ) {}

  ngOnInit(): void {
    this.loadData();
  }

  loadData(): void {
    this.loading.set(true);
    this.error.set(null);

    this.systemApi.status().subscribe({
      next: status => {
        this.systemState.set(status.system?.state || 'NORMAL');
        this.systemReason.set(status.system?.reason || 'Штатний режим');
        if (status.resources && (status.resources.cpu !== undefined || status.resources.ram !== undefined || status.resources.disk !== undefined)) {
          this.resources.set([
            {
              label: 'CPU',
              value: status.resources.cpu ?? null,
              color: 'var(--v3-accent)',
              detail: status.resources.cpu_used_cores != null && status.resources.cpu_cores
                ? `${status.resources.cpu_used_cores} | ${status.resources.cpu_cores} CPU`
                : null,
            },
            {
              label: 'RAM',
              value: status.resources.ram ?? null,
              color: 'var(--v3-sky)',
              detail: status.resources.ram_used_bytes != null && status.resources.ram_total_bytes
                ? `${formatGiB(status.resources.ram_used_bytes, this.i18n.locale)} | ${formatGiB(status.resources.ram_total_bytes, this.i18n.locale)}`
                : null,
            },
            {
              label: 'Диск',
              value: status.resources.disk ?? null,
              color: 'var(--v3-amber)',
              detail: status.resources.disk_used_bytes != null && status.resources.disk_total_bytes
                ? `${formatGiB(status.resources.disk_used_bytes, this.i18n.locale)} | ${formatGiB(status.resources.disk_total_bytes, this.i18n.locale)}`
                : null,
            },
          ]);
        } else {
          this.resources.set([]);
        }
      },
      error: err => {
        this.error.set(err?.message || 'Не вдалося завантажити дані системи');
      },
    });

    this.workersApi.list().subscribe({
      next: workers => {
        this.workers.set(Array.isArray(workers) ? workers : []);
        this.loading.set(false);
      },
      error: err => {
        this.error.set(err?.message || 'Не вдалося завантажити воркери');
        this.loading.set(false);
      },
    });

    this.jobsApi.list().subscribe({
      next: jobs => this.jobs.set(Array.isArray(jobs) ? jobs : []),
      error: () => {},
    });
  }

  metric(value: number | null): string {
    return value == null ? 'Немає даних' : value.toLocaleString(this.i18n.locale);
  }
}
