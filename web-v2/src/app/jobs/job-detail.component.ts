import { Component, OnInit, signal, OnDestroy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, Router, RouterModule } from '@angular/router';
import { VertepApiService } from '../core/api.service';
import { ToastService } from '../core/services/toast.service';
import { ConfirmService } from '../core/services/confirm.service';
import { VertepDatePipe } from '../shared/vertep-date.pipe';
import { Subscription } from 'rxjs';

interface JobDetail {
  job_id: string;
  topic: string;
  character_id: string;
  status: string;
  priority: number;
  created_at: string;
  updated_at?: string;
  script?: { title?: string; scenes?: any[] };
  events: string[];
  output_path?: string;
  retries: number;
  source: string;
  assigned_worker?: string;
  approved: boolean;
  approval_status: string;
  published_to: string[];
  task_type: string;
  min_vram_mb: number;
  max_retries: number;
  brand_id: string;
  workflow?: string;
  active_task_id?: string;
  aspect_ratio: string;
  output_preset: string;
  version: number;
  stages: Record<string, any>;
  scenes: any[];
  artifacts: any[];
  scheduled_for?: string;
}

@Component({
  selector: 'app-job-detail',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterModule, VertepDatePipe],
  template: `
    <div class="space-y-6" data-testid="job-detail-page">
      @if (loading()) {
        <div class="flex items-center justify-center py-12">
          <div class="w-10 h-10 border-4 border-emerald-500 border-t-transparent rounded-full animate-spin"></div>
        </div>
      } @else if (error()) {
        <div class="bg-red-50 border border-red-200 rounded-xl p-6">
          <p class="text-red-700">{{ error() }}</p>
          <button (click)="goBack()" class="mt-3 text-sm text-red-600 hover:text-red-700 font-medium">
            ← Повернутися до списку
          </button>
        </div>
      } @else if (job()) {
        <div class="bg-white rounded-xl border border-slate-200 p-6">
          <div class="flex items-start justify-between mb-6">
            <div>
              <div class="flex items-center gap-3 mb-2">
                <h2 class="text-xl font-semibold text-slate-900">{{ job()!.topic }}</h2>
                <span class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium"
                  [class.bg-emerald-50]="isActiveStatus(job()!.status)"
                  [class.text-emerald-700]="isActiveStatus(job()!.status)"
                  [class.bg-slate-100]="!isActiveStatus(job()!.status)"
                  [class.text-slate-600]="!isActiveStatus(job()!.status)">
                  {{ job()!.status }}
                </span>
              </div>
              <p class="text-sm text-slate-500">ID: {{ job()!.job_id }}</p>
            </div>
            <div class="flex gap-2">
              @if (!editing()) {
                <button (click)="startEditing()" data-testid="edit-job-button"
                  class="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm font-medium">
                  Редагувати
                </button>
              }
              <button (click)="confirmDelete()" data-testid="delete-job-button"
                class="px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700 text-sm font-medium">
                Видалити
              </button>
              <button (click)="goBack()" data-testid="back-to-list-button"
                class="px-4 py-2 border border-slate-200 text-slate-600 rounded-lg hover:bg-slate-50 text-sm font-medium">
                ← Список
              </button>
            </div>
          </div>

          @if (editing()) {
            <div class="bg-blue-50 border border-blue-200 rounded-lg p-4 mb-6">
              <h4 class="text-sm font-medium text-blue-900 mb-3">Редагування завдання</h4>
              <div class="space-y-4">
                <div>
                  <label class="block text-sm font-medium text-slate-700 mb-1">Тема</label>
                  <input [(ngModel)]="editForm.topic" data-testid="edit-topic-input"
                    class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500">
                </div>
                <div>
                  <label class="block text-sm font-medium text-slate-700 mb-1">Пріоритет (1-10)</label>
                  <input type="number" [(ngModel)]="editForm.priority" min="1" max="10" data-testid="edit-priority-input"
                    class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500">
                </div>
                @if (job()!.workflow) {
                  <div>
                    <label class="block text-sm font-medium text-slate-700 mb-1">Workflow</label>
                    <input [(ngModel)]="editForm.workflow" data-testid="edit-workflow-input"
                      class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500">
                  </div>
                }
                <div class="flex gap-2 pt-2">
                  <button (click)="saveChanges()" [disabled]="saving()"
                    class="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm font-medium disabled:opacity-50">
                    {{ saving() ? 'Збереження...' : 'Зберегти' }}
                  </button>
                  <button (click)="cancelEditing()"
                    class="px-4 py-2 border border-slate-200 text-slate-600 rounded-lg hover:bg-slate-50 text-sm font-medium">
                    Скасувати
                  </button>
                </div>
              </div>
            </div>
          }

          <div class="grid grid-cols-2 md:grid-cols-3 gap-4">
            <div class="bg-slate-50 rounded-lg p-4">
              <p class="text-xs text-slate-500 mb-1">Створено</p>
              <p class="text-sm font-medium text-slate-900">{{ job()!.created_at | vertepDate }}</p>
            </div>
            <div class="bg-slate-50 rounded-lg p-4">
              <p class="text-xs text-slate-500 mb-1">Оновлено</p>
              <p class="text-sm font-medium text-slate-900">{{ (job()!.updated_at || job()!.created_at) | vertepDate }}</p>
            </div>
            <div class="bg-slate-50 rounded-lg p-4">
              <p class="text-xs text-slate-500 mb-1">Персонаж</p>
              <p class="text-sm font-medium text-slate-900">{{ job()!.character_id }}</p>
            </div>
            <div class="bg-slate-50 rounded-lg p-4">
              <p class="text-xs text-slate-500 mb-1">Пріоритет</p>
              <p class="text-sm font-medium text-slate-900">{{ job()!.priority }}</p>
            </div>
            <div class="bg-slate-50 rounded-lg p-4">
              <p class="text-xs text-slate-500 mb-1">Task Type</p>
              <p class="text-sm font-medium text-slate-900">{{ job()!.task_type }}</p>
            </div>
            <div class="bg-slate-50 rounded-lg p-4">
              <p class="text-xs text-slate-500 mb-1">Aspect Ratio</p>
              <p class="text-sm font-medium text-slate-900">{{ job()!.aspect_ratio }}</p>
            </div>
            <div class="bg-slate-50 rounded-lg p-4">
              <p class="text-xs text-slate-500 mb-1">Output Preset</p>
              <p class="text-sm font-medium text-slate-900">{{ job()!.output_preset }}</p>
            </div>
            <div class="bg-slate-50 rounded-lg p-4">
              <p class="text-xs text-slate-500 mb-1">Робочий вузол</p>
              <p class="text-sm font-medium text-slate-900">{{ job()!.assigned_worker || '—' }}</p>
            </div>
            <div class="bg-slate-50 rounded-lg p-4">
              <p class="text-xs text-slate-500 mb-1">Публікації</p>
              <p class="text-sm font-medium text-slate-900">{{ job()!.published_to.length ? job()!.published_to.join(', ') : '—' }}</p>
            </div>
            @if (job()!.workflow) {
              <div class="bg-slate-50 rounded-lg p-4 col-span-2">
                <p class="text-xs text-slate-500 mb-1">Workflow</p>
                <p class="text-sm font-medium text-slate-900 font-mono">{{ job()!.workflow }}</p>
              </div>
            }
            @if (job()!.scheduled_for) {
              <div class="bg-slate-50 rounded-lg p-4 col-span-2">
                <p class="text-xs text-slate-500 mb-1">Заплановано на</p>
                <p class="text-sm font-medium text-slate-900">{{ job()!.scheduled_for | vertepDate }}</p>
              </div>
            }
          </div>

          @if (job()!.scenes && job()!.scenes.length > 0) {
            <div class="mt-6">
              <h4 class="text-sm font-medium text-slate-900 mb-3">Сцени</h4>
              <div class="space-y-2">
                @for (scene of job()!.scenes; track scene.scene_id; let i = $index) {
                  <div class="bg-slate-50 rounded-lg p-3 flex items-center justify-between">
                    <div>
                      <span class="text-xs text-slate-500">Сцена {{ i + 1 }}</span>
                      <p class="text-sm text-slate-900">{{ scene.prompt?.substring(0, 100) || 'Без опису' }}...</p>
                    </div>
                    <span class="px-2 py-1 rounded-full text-xs font-medium"
                      [class.bg-emerald-50]="scene.status === 'RUNNING'"
                      [class.text-emerald-700]="scene.status === 'RUNNING'"
                      [class.bg-blue-50]="scene.status === 'READY'"
                      [class.text-blue-700]="scene.status === 'READY'"
                      [class.bg-red-50]="scene.status === 'FAILED'"
                      [class.text-red-700]="scene.status === 'FAILED'"
                      [class.bg-slate-100]="!['RUNNING', 'READY', 'FAILED'].includes(scene.status)"
                      [class.text-slate-600]="!['RUNNING', 'READY', 'FAILED'].includes(scene.status)">
                      {{ scene.status }}
                    </span>
                  </div>
                }
              </div>
            </div>
          }

          @if (job()!.artifacts && job()!.artifacts.length > 0) {
            <div class="mt-6">
              <h4 class="text-sm font-medium text-slate-900 mb-3">Артефакти</h4>
              <div class="space-y-2">
                @for (artifact of job()!.artifacts; track artifact.artifact_id) {
                  <div class="bg-slate-50 rounded-lg p-3 flex items-center justify-between">
                    <div>
                      <span class="text-xs text-slate-500">{{ artifact.kind }}</span>
                      <p class="text-sm text-slate-900">{{ artifact.filename }}</p>
                    </div>
                    @if (artifact.path) {
                      <a [href]="'/api/jobs/' + job()!.job_id + '/artifacts/' + artifact.artifact_id + '/download'"
                         class="text-sm text-emerald-600 hover:text-emerald-700 font-medium">
                        Завантажити
                      </a>
                    }
                  </div>
                }
              </div>
            </div>
          }

          @if (job()!.events && job()!.events.length > 0) {
            <div class="mt-6">
              <h4 class="text-sm font-medium text-slate-900 mb-3">Історія подій</h4>
              <div class="bg-slate-50 rounded-lg p-3 max-h-48 overflow-y-auto">
                @for (event of job()!.events.slice().reverse(); track $index) {
                  <p class="text-xs text-slate-600 font-mono py-0.5">{{ event }}</p>
                }
              </div>
            </div>
          }
        </div>
      }
    </div>
  `,
})
export class JobDetailComponent implements OnInit, OnDestroy {
  job = signal<JobDetail | null>(null);
  loading = signal(true);
  error = signal<string | null>(null);
  editing = signal(false);
  saving = signal(false);
  editForm = { topic: '', priority: 5, workflow: '' };
  private subs = new Subscription();

  constructor(
    private route: ActivatedRoute,
    private router: Router,
    private api: VertepApiService,
    private toast: ToastService,
    private confirm: ConfirmService,
  ) {}

  ngOnInit(): void {
    const jobId = this.route.snapshot.paramMap.get('id');
    if (jobId) {
      this.loadJob(jobId);
    } else {
      this.error.set('ID завдання не вказано');
      this.loading.set(false);
    }
  }

  ngOnDestroy(): void {
    this.subs.unsubscribe();
  }

  loadJob(jobId: string): void {
    this.loading.set(true);
    this.error.set(null);
    this.subs.add(
      this.api.getJob(jobId).subscribe({
        next: (job) => {
          this.job.set(job as JobDetail);
          this.loading.set(false);
        },
        error: (err) => {
          this.error.set(err.message || 'Не вдалося завантажити завдання');
          this.loading.set(false);
        },
      }),
    );
  }

  isActiveStatus(status: string): boolean {
    return ['RUNNING', 'SCRIPTING', 'ASSET_GENERATION', 'VIDEO_GENERATION', 'ASSEMBLY', 'PUBLISHING'].includes(status);
  }

  startEditing(): void {
    const j = this.job();
    if (!j) return;
    this.editForm = {
      topic: j.topic,
      priority: j.priority,
      workflow: j.workflow || '',
    };
    this.editing.set(true);
  }

  cancelEditing(): void {
    this.editing.set(false);
  }

  saveChanges(): void {
    const j = this.job();
    if (!j) return;
    this.saving.set(true);
    const payload: any = {
      expected_version: j.version,
      topic: this.editForm.topic,
      priority: this.editForm.priority,
    };
    if (this.editForm.workflow) {
      payload.workflow = this.editForm.workflow;
    }
    this.subs.add(
      this.api.updateJob(j.job_id, payload).subscribe({
        next: (updated) => {
          this.job.set(updated as JobDetail);
          this.editing.set(false);
          this.saving.set(false);
          this.toast.show('Завдання оновлено', 'success');
        },
        error: (err) => {
          this.toast.show(err.message || 'Помилка оновлення', 'error');
          this.saving.set(false);
        },
      }),
    );
  }

  confirmDelete(): void {
    const j = this.job();
    if (!j) return;
    this.confirm.confirm({
      title: 'Видалити завдання',
      message: `Ви впевнені, що хочете видалити "${j.topic}"?`,
    }).subscribe((ok) => {
      if (!ok) return;
      this.subs.add(
        this.api.deleteJob(j.job_id).subscribe({
          next: () => {
            this.toast.show('Завдання видалено', 'success');
            this.router.navigate(['/jobs']);
          },
          error: (err) => {
            this.toast.show(err.message || 'Помилка видалення', 'error');
          },
        }),
      );
    });
  }

  goBack(): void {
    this.router.navigate(['/jobs']);
  }
}
