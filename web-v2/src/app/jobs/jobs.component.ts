import { Component, OnInit, signal, OnDestroy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, Router, RouterModule } from '@angular/router';
import { VertepApiService } from '../core/api.service';
import { ToastService } from '../core/services/toast.service';
import { ConfirmService } from '../core/services/confirm.service';
import { Job, Character, Brand, Workflow, JobCreate, QueueState, DeadLetterTask } from '../core/models';
import { VertepDatePipe } from '../shared/vertep-date.pipe';
import { LoadingStateComponent } from '../shared/loading-state.component';
import { ErrorStateComponent } from '../shared/error-state.component';
import { EmptyStateComponent } from '../shared/empty-state.component';
import { inStatusGroup, statusLabel } from '../core/presentation';
import { Subscription, timer } from 'rxjs';

@Component({
  selector: 'app-jobs',
  standalone: true,
  imports: [CommonModule, FormsModule, VertepDatePipe, RouterModule, LoadingStateComponent, ErrorStateComponent, EmptyStateComponent],
  template: `
    <div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="jobs-page">
      <div class="flex items-center justify-between mb-4">
        <h3 class="text-lg font-semibold text-slate-900">Завдання</h3>
        @if (view() === 'list') {
          <button (click)="openCreateModal()" data-testid="create-job-button" class="px-4 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 text-sm font-medium">
            Нове завдання
          </button>
        }
      </div>

      <div class="mb-4 flex gap-2 border-b border-slate-200">
        <button (click)="setView('list')" [class.border-b-2]="view() === 'list'" [class.border-emerald-600]="view() === 'list'" [class.text-emerald-700]="view() === 'list'" class="px-4 py-2 text-sm font-medium text-slate-600 hover:text-slate-900">Список</button>
        <button (click)="setView('queue')" [class.border-b-2]="view() === 'queue'" [class.border-emerald-600]="view() === 'queue'" [class.text-emerald-700]="view() === 'queue'" class="px-4 py-2 text-sm font-medium text-slate-600 hover:text-slate-900">Виконання</button>
      </div>

      @if (view() === 'list') {
        <div class="mb-4 grid grid-cols-1 md:grid-cols-[1fr_220px] gap-3">
          <input [(ngModel)]="search" data-testid="jobs-search" placeholder="Пошук за ID або темою..." class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-sm">
          <select [(ngModel)]="statusGroup" data-testid="jobs-status-filter" class="px-3 py-2 border border-slate-200 rounded-lg text-sm">
            <option value="">Усі стани</option><option value="active">Активні</option><option value="queued">У черзі</option><option value="waiting">Очікують</option><option value="completed">Завершені</option><option value="failed">З помилкою</option>
          </select>
        </div>

        @if (loading()) {
          <app-loading-state />
        } @else if (error()) {
          <app-error-state [message]="error()!" (retry)="loadJobs()" />
        } @else {
          <div class="overflow-x-auto">
            <table class="w-full text-sm text-left" data-testid="jobs-table">
              <thead class="text-xs text-slate-500 uppercase bg-slate-50">
                <tr>
                  <th class="px-4 py-3">ID</th>
                  <th class="px-4 py-3">Статус</th>
                  <th class="px-4 py-3">Створено</th>
                  <th class="px-4 py-3">Дії</th>
                </tr>
              </thead>
              <tbody>
                @for (job of pagedJobs; track job.job_id) {
                  <tr class="border-t border-slate-100">
                    <td class="px-4 py-3 font-medium text-slate-900">{{ job.job_id }}</td>
                    <td class="px-4 py-3">
                      <span class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium"
                        [class.bg-emerald-50]="isActive(job.status)"
                        [class.text-emerald-700]="isActive(job.status)"
                        [class.bg-slate-100]="!isActive(job.status)"
                        [class.text-slate-600]="!isActive(job.status)">
                        {{ jobStatusLabel(job.status) }}
                      </span>
                    </td>
                    <td class="px-4 py-3">{{ job.created_at | vertepDate }}</td>
                    <td class="px-4 py-3">
                      <button (click)="openJob(job.job_id)" class="text-emerald-600 hover:text-emerald-700 text-sm font-medium mr-2">Відкрити</button>
                      <button (click)="deleteJob(job.job_id)" class="text-red-600 hover:text-red-700 text-sm font-medium">Видалити</button>
                    </td>
                  </tr>
                } @empty {
                  <tr><td colspan="4" class="px-4 py-6 text-center text-slate-500" data-testid="jobs-empty">Завдань не знайдено</td></tr>
                }
              </tbody>
            </table>
          </div>
          @if (pages > 1) {
            <div class="flex items-center justify-between mt-4">
              <button (click)="prevPage()" [disabled]="page === 1" class="px-3 py-1.5 text-sm border border-slate-200 rounded-lg disabled:opacity-50">Назад</button>
              <span class="text-sm text-slate-600">Сторінка {{ page }} з {{ pages }}</span>
              <button (click)="nextPage()" [disabled]="page === pages" class="px-3 py-1.5 text-sm border border-slate-200 rounded-lg disabled:opacity-50">Вперед</button>
            </div>
          }
        }
      } @else {
        <div class="space-y-6" data-testid="queue-page">
          <div class="flex items-center justify-between">
            <div>
              <h4 class="text-sm font-medium text-slate-900">Виконання завдань</h4>
              <p class="text-xs text-slate-500">Операційний стан scheduled, ready, inflight і dead-letter етапів життєвого циклу.</p>
            </div>
            <button (click)="loadQueue()" class="text-sm text-emerald-600 hover:text-emerald-700 font-medium">Оновити</button>
          </div>

          @if (queueLoading()) {
            <div class="space-y-3">
              @for (_ of [1,2,3]; track $index) {
                <div class="animate-pulse bg-slate-100 rounded-lg h-16"></div>
              }
            </div>
          } @else if (queueError()) {
            <p class="text-sm text-red-600">{{ queueError() }}</p>
          } @else {
            <div class="grid grid-cols-1 md:grid-cols-4 gap-4 mb-6">
              <div class="bg-slate-50 rounded-lg p-4">
                <p class="text-xs text-slate-500">Відкладені</p>
                <p class="text-2xl font-bold text-slate-900">{{ scheduledJobs().length }}</p>
              </div>
              <div class="bg-slate-50 rounded-lg p-4">
                <p class="text-xs text-slate-500">Готові</p>
                <p class="text-2xl font-bold text-slate-900">{{ readyCount() }}</p>
              </div>
              <div class="bg-slate-50 rounded-lg p-4">
                <p class="text-xs text-slate-500">В обробці</p>
                <p class="text-2xl font-bold text-slate-900">{{ inflightCount() }}</p>
              </div>
              <div class="bg-slate-50 rounded-lg p-4">
                <p class="text-xs text-slate-500">Потребують втручання</p>
                <p class="text-2xl font-bold text-red-600">{{ deadLetterCount() }}</p>
              </div>
            </div>

            @if (scheduledJobs().length > 0) {
              <div class="mb-6">
                <h4 class="text-sm font-medium text-slate-900 mb-2">Відкладені завдання</h4>
                <div class="space-y-2">
                  @for (job of scheduledJobs(); track job.job_id) {
                    <div class="bg-slate-50 rounded-lg p-3 flex items-center justify-between">
                      <div>
                        <span class="text-sm font-medium text-slate-900">{{ job.topic }}</span>
                        <p class="text-xs text-slate-500">ID: {{ job.job_id }} · Заплановано: {{ job.scheduled_for | vertepDate }}</p>
                      </div>
                      <a [routerLink]="['/jobs', job.job_id]" class="text-xs text-emerald-600 hover:text-emerald-700 font-medium">Відкрити</a>
                    </div>
                  }
                </div>
              </div>
            }

            @if (readyTasks().length > 0) {
              <div class="mb-6">
                <h4 class="text-sm font-medium text-slate-900 mb-2">Готові до виконання</h4>
                <div class="space-y-2">
                  @for (task of readyTasks(); track task.task_id) {
                    <div class="bg-slate-50 rounded-lg p-3 flex items-center justify-between">
                      <div>
                        <span class="text-sm font-medium text-slate-900">{{ task.task }}</span>
                        <p class="text-xs text-slate-500">Задача: {{ task.task_id }} · Сцена: {{ task.scene_id }}</p>
                      </div>
                      <a [routerLink]="['/jobs', task.job_id]" class="text-xs text-emerald-600 hover:text-emerald-700 font-medium">Відкрити</a>
                    </div>
                  }
                </div>
              </div>
            }

            @if (inflightTasks().length > 0) {
              <div class="mb-6">
                <h4 class="text-sm font-medium text-slate-900 mb-2">В процесі виконання</h4>
                <div class="space-y-2">
                  @for (task of inflightTasks(); track task.task_id) {
                    <div class="bg-slate-50 rounded-lg p-3 flex items-center justify-between">
                      <div>
                        <span class="text-sm font-medium text-slate-900">{{ task.task }}</span>
                        <p class="text-xs text-slate-500">Задача: {{ task.task_id }} · Сцена: {{ task.scene_id }}</p>
                      </div>
                      <a [routerLink]="['/jobs', task.job_id]" class="text-xs text-emerald-600 hover:text-emerald-700 font-medium">Відкрити</a>
                    </div>
                  }
                </div>
              </div>
            }

            @if (deadLetterTasks().length > 0) {
              <div class="mb-6">
                <h4 class="text-sm font-medium text-slate-900 mb-2">Dead-letter задачі</h4>
                <div class="space-y-2">
                  @for (task of deadLetterTasks(); track task.task_id) {
                    <div class="bg-red-50 border border-red-200 rounded-lg p-3 flex items-center justify-between">
                      <div>
                        <p class="text-sm font-medium text-slate-900">{{ task.job_id }}</p>
                        <p class="text-xs text-slate-500">Task: {{ task.task_id }} · Сцена: {{ task.scene_id }}</p>
                        <p class="text-xs text-red-600">{{ task.error }}</p>
                      </div>
                      <div class="flex gap-2">
                        <a [routerLink]="['/jobs', task.job_id]" class="text-xs text-emerald-600 hover:text-emerald-700 font-medium">Відкрити</a>
                        <button (click)="retryTask(task.task_id)" [disabled]="retrying() === task.task_id" class="text-xs px-3 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700 disabled:opacity-50">
                          {{ retrying() === task.task_id ? 'Повтор...' : 'Повторити' }}
                        </button>
                      </div>
                    </div>
                  }
                </div>
              </div>
            } @else if (readyTasks().length === 0 && inflightTasks().length === 0 && !scheduledJobs().length) {
              <p class="text-sm text-slate-500">Черга порожня</p>
            }
          }
        </div>
      }
    </div>

    <!-- Create Job Modal -->
    <div *ngIf="showCreateModal" data-testid="create-job-modal" class="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div class="bg-white rounded-xl p-6 w-full max-w-lg mx-4">
        <h3 class="text-lg font-semibold text-slate-900 mb-4">Нове завдання</h3>
        <div class="space-y-4">
          <div>
            <label class="block text-sm font-medium text-slate-700 mb-1">Тема</label>
            <input [(ngModel)]="newJob.topic" data-testid="job-topic-input" placeholder="Наприклад, Історія про діда Самогонщика" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500">
          </div>

          <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label class="block text-sm font-medium text-slate-700 mb-1">Персонаж</label>
              <select [(ngModel)]="newJob.character_id" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500">
                <option value="">За замовчуванням</option>
                @for (character of characters(); track character.id) {
                  <option [value]="character.id">{{ character.name }} ({{ character.id }})</option>
                }
              </select>
            </div>
            <div>
              <label class="block text-sm font-medium text-slate-700 mb-1">Бренд</label>
              <select [(ngModel)]="newJob.brand_id" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500">
                <option value="">За замовчуванням</option>
                @for (brand of brands(); track brand.id) {
                  <option [value]="brand.id">{{ brand.name }} ({{ brand.id }})</option>
                }
              </select>
            </div>
          </div>

          <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label class="block text-sm font-medium text-slate-700 mb-1">Пріоритет (1-10)</label>
              <input type="number" [(ngModel)]="newJob.priority" min="1" max="10" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500">
            </div>
            <div>
              <label class="block text-sm font-medium text-slate-700 mb-1">Тип завдання</label>
              <select [(ngModel)]="newJob.task_type" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500">
                <option value="image">Зображення</option>
                <option value="video">Відео</option>
              </select>
            </div>
          </div>

          <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label class="block text-sm font-medium text-slate-700 mb-1">Workflow</label>
              <select [(ngModel)]="newJob.workflow" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500">
                <option value="">Без workflow</option>
                @for (workflow of workflows(); track workflow.name) {
                  <option [value]="workflow.name">{{ workflow.kind }}/{{ workflow.name }}</option>
                }
              </select>
            </div>
            <div>
              <label class="block text-sm font-medium text-slate-700 mb-1">Співвідношення сторін</label>
              <select [(ngModel)]="newJob.aspect_ratio" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500">
                <option value="16:9">16:9</option>
                <option value="9:16">9:16</option>
              </select>
            </div>
          </div>

          <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label class="block text-sm font-medium text-slate-700 mb-1">Вихідний preset</label>
              <select [(ngModel)]="newJob.output_preset" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500">
                <option value="youtube">YouTube</option>
                <option value="tiktok">TikTok</option>
                <option value="instagram">Instagram</option>
                <option value="facebook">Facebook</option>
              </select>
            </div>
            <div class="flex items-center gap-2">
              <input type="checkbox" [(ngModel)]="newJobScheduled" id="scheduled">
              <label for="scheduled" class="text-sm text-slate-700">Запланувати</label>
            </div>
          </div>

          <div *ngIf="newJobScheduled">
            <label class="block text-sm font-medium text-slate-700 mb-1">Дата та час</label>
            <input type="datetime-local" [(ngModel)]="newJobDate" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500">
          </div>
        </div>
        <div class="flex justify-end gap-2 mt-6">
          <button (click)="showCreateModal = false" class="px-4 py-2 text-slate-600 hover:text-slate-800 text-sm font-medium">Скасувати</button>
          <button (click)="createJob()" [disabled]="creating" class="px-4 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 text-sm font-medium disabled:opacity-50">{{ creating ? 'Створення...' : 'Створити' }}</button>
        </div>
      </div>
    </div>
  `,
})
export class JobsComponent implements OnInit, OnDestroy {
  view = signal<'list' | 'queue'>('list');
  jobs: Job[] = [];
  loading = signal(false);
  error = signal<string | null>(null);
  showCreateModal = false;
  creating = false;
  search = '';
  page = 1;
  pageSize = 10;

  characters = signal<Character[]>([]);
  brands = signal<Brand[]>([]);
  workflows = signal<Workflow[]>([]);

  newJob: Partial<JobCreate> = {
    topic: '',
    character_id: '',
    priority: 5,
    task_type: 'image',
    brand_id: '',
    aspect_ratio: '16:9',
    output_preset: 'youtube',
    workflow: '',
  };
  newJobScheduled = false;
  newJobDate = '';

  statusGroup = '';

  queueLoading = signal(false);
  queueError = signal<string | null>(null);
  scheduledJobs = signal<Job[]>([]);
  readyTasks = signal<QueueState['ready']>([]);
  inflightTasks = signal<QueueState['inflight']>([]);
  deadLetterTasks = signal<DeadLetterTask[]>([]);
  readyCount = signal(0);
  inflightCount = signal(0);
  deadLetterCount = signal(0);
  retrying = signal<string | null>(null);
  private queueSubs = new Subscription();
  private pollTimer: Subscription | null = null;

  constructor(private api: VertepApiService, private toast: ToastService, private confirm: ConfirmService, private router: Router, private route: ActivatedRoute) {}

  ngOnInit(): void {
    const tab = this.route.snapshot.queryParamMap.get('tab');
    this.view.set(tab === 'queue' ? 'queue' : 'list');
    this.statusGroup = this.route.snapshot.queryParamMap.get('group') || '';
    this.loadJobs();
    this.loadCharacters();
    this.loadBrands();
    this.loadWorkflows();
    if (this.view() === 'queue') {
      this.startQueuePolling();
    }
  }

  ngOnDestroy(): void {
    this.queueSubs.unsubscribe();
    if (this.pollTimer) {
      this.pollTimer.unsubscribe();
    }
  }

  setView(v: 'list' | 'queue'): void {
    this.view.set(v);
    if (v === 'queue') {
      this.loadQueue();
      this.startQueuePolling();
    } else {
      this.stopQueuePolling();
    }
  }

  startQueuePolling(): void {
    this.stopQueuePolling();
    this.loadQueue();
    this.pollTimer = timer(0, 5000).subscribe(() => this.loadQueue());
  }

  stopQueuePolling(): void {
    if (this.pollTimer) {
      this.pollTimer.unsubscribe();
      this.pollTimer = null;
    }
  }

  get filteredJobs(): Job[] {
    const term = this.search.toLowerCase();
    return this.jobs.filter(j => {
      const matchesText = !term || `${j.job_id} ${j.topic}`.toLowerCase().includes(term);
      const matchesGroup = !this.statusGroup || inStatusGroup(j.status, this.statusGroup as 'active' | 'queued' | 'waiting' | 'completed' | 'failed');
      return matchesText && matchesGroup;
    });
  }

  get pagedJobs(): Job[] {
    const start = (this.page - 1) * this.pageSize;
    return this.filteredJobs.slice(start, start + this.pageSize);
  }

  get pages() {
    return Math.max(1, Math.ceil(this.filteredJobs.length / this.pageSize));
  }

  loadJobs(): void {
    this.loading.set(true);
    this.error.set(null);
    this.api.getJobs().subscribe({
      next: (jobs) => {
        this.jobs = jobs;
        this.loading.set(false);
      },
      error: (err) => {
        this.error.set(err.message);
        this.loading.set(false);
      },
    });
  }

  loadCharacters(): void {
    this.api.getCharacters().subscribe({
      next: (characters) => this.characters.set(characters),
      error: () => {},
    });
  }

  loadBrands(): void {
    this.api.getBrands().subscribe({
      next: (brands) => this.brands.set(brands),
      error: () => {},
    });
  }

  loadWorkflows(): void {
    this.api.getWorkflows().subscribe({
      next: (workflows) => this.workflows.set(workflows),
      error: () => {},
    });
  }

  loadQueue(): void {
    this.queueLoading.set(true);
    this.queueError.set(null);
    this.queueSubs.add(
      this.api.getQueueState().subscribe({
        next: (state: QueueState) => {
          this.readyTasks.set(state.ready || []);
          this.inflightTasks.set(state.inflight || []);
          this.readyCount.set((state.ready || []).length);
          this.inflightCount.set((state.inflight || []).length);
          this.queueLoading.set(false);
        },
        error: (err) => {
          this.queueError.set(err.message || 'Не вдалося завантажити чергу');
          this.queueLoading.set(false);
        },
      }),
    );
    this.queueSubs.add(
      this.api.getDeadLetterTasks().subscribe({
        next: (tasks) => {
          this.deadLetterTasks.set(tasks);
          this.deadLetterCount.set(tasks.length);
        },
        error: () => {},
      }),
    );
    this.queueSubs.add(
      this.api.getJobs().subscribe({
        next: (jobs) => {
          const scheduled = jobs.filter(j => j.status === 'NEW' && j.scheduled_for);
          this.scheduledJobs.set(scheduled);
        },
        error: () => {},
      }),
    );
  }

  retryTask(taskId: string): void {
    this.retrying.set(taskId);
    this.queueSubs.add(
      this.api.retryDeadLetterTask(taskId).subscribe({
        next: () => {
          this.toast.show('Задачу відправлено до черги', 'success');
          this.loadQueue();
          this.retrying.set(null);
        },
        error: (err) => {
          this.toast.show(err.message || 'Помилка повтору', 'error');
          this.retrying.set(null);
        },
      }),
    );
  }

  isActive(status: string): boolean {
    return inStatusGroup(status, 'active');
  }

  jobStatusLabel(status: string): string { return statusLabel(status); }

  openCreateModal(): void {
    this.newJob = {
      topic: '',
      character_id: '',
      priority: 5,
      task_type: 'image',
      brand_id: '',
      aspect_ratio: '16:9',
      output_preset: 'youtube',
      workflow: '',
    };
    this.newJobScheduled = false;
    this.newJobDate = '';
    this.showCreateModal = true;
  }

  createJob(): void {
    if (!this.newJob.topic?.trim()) return;
    this.creating = true;
    const payload: JobCreate = {
      topic: this.newJob.topic,
      character_id: this.newJob.character_id || undefined,
      priority: this.newJob.priority || 5,
      task_type: this.newJob.task_type || 'image',
      brand_id: this.newJob.brand_id || undefined,
      aspect_ratio: this.newJob.aspect_ratio || '16:9',
      output_preset: this.newJob.output_preset || 'youtube',
      workflow: this.newJob.workflow || undefined,
    };
    if (this.newJobScheduled && this.newJobDate) {
      payload.scheduled_for = this.newJobDate;
    }
    this.api.createJob(payload).subscribe({
      next: () => {
        this.showCreateModal = false;
        this.loadJobs();
        this.toast.show('Завдання створено', 'success');
      },
      error: (err) => {
        this.error.set(err.message);
        this.creating = false;
        this.toast.show(err.message || 'Помилка створення', 'error');
      },
    });
  }

  openJob(id: string): void {
    this.router.navigate(['/jobs', id]);
  }

  deleteJob(id: string): void {
    this.confirm.confirm({ title: 'Видалити завдання', message: `Ви впевнені, що хочете видалити ${id}?` }).subscribe((ok) => {
      if (!ok) return;
      this.api.deleteJob(id).subscribe({
        next: () => {
          this.loadJobs();
          this.toast.show('Завдання видалено', 'success');
        },
        error: (err) => this.toast.show(err.message || 'Помилка видалення', 'error'),
      });
    });
  }

  prevPage(): void {
    if (this.page > 1) this.page--;
  }

  nextPage(): void {
    if (this.page < this.pages) this.page++;
  }
}
