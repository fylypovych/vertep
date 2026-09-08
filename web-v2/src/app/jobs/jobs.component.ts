import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { VertepApiService } from '../core/api.service';
import { ToastService } from '../core/services/toast.service';
import { ConfirmService } from '../core/services/confirm.service';
import { Job, Character, Brand, Workflow, JobCreate } from '../core/models';
import { VertepDatePipe } from '../shared/vertep-date.pipe';

@Component({
  selector: 'app-jobs',
  standalone: true,
  imports: [CommonModule, FormsModule, VertepDatePipe],
  template: `
    <div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="jobs-page">
      <div class="flex items-center justify-between mb-4">
        <h3 class="text-lg font-semibold text-slate-900">Завдання</h3>
        <button (click)="openCreateModal()" data-testid="create-job-button" class="px-4 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 text-sm font-medium">
          Нове завдання
        </button>
      </div>

      <div class="mb-4">
        <input [(ngModel)]="search" data-testid="jobs-search" placeholder="Пошук за ID..." class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-sm">
      </div>

      @if (loading()) {
        <div class="space-y-3">
          @for (_ of [1,2,3]; track $index) {
            <div class="animate-pulse bg-slate-100 rounded-lg h-16"></div>
          }
        </div>
      } @else if (error()) {
        <div class="bg-red-50 border border-red-200 rounded-xl p-5">
          <p class="text-red-700">{{ error() }}</p>
          <button (click)="loadJobs()" class="mt-2 text-sm text-red-600 hover:text-red-700 font-medium">Повторити</button>
        </div>
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
                      {{ job.status }}
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
export class JobsComponent implements OnInit {
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

  constructor(private api: VertepApiService, private toast: ToastService, private confirm: ConfirmService, private router: Router) {}

  ngOnInit(): void {
    this.loadJobs();
    this.loadCharacters();
    this.loadBrands();
    this.loadWorkflows();
  }

  get filteredJobs(): Job[] {
    if (!this.search.trim()) return this.jobs;
    const term = this.search.toLowerCase();
    return this.jobs.filter(j => (j.job_id || '').toLowerCase().includes(term));
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

  isActive(status: string): boolean {
    return ['RUNNING', 'SCRIPTING', 'ASSET_GENERATION', 'VIDEO_GENERATION', 'ASSEMBLY'].includes(status);
  }

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
