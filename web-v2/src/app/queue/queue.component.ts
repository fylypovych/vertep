import { Component, OnInit, OnDestroy, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterModule } from '@angular/router';
import { VertepApiService } from '../core/api.service';
import { ToastService } from '../core/services/toast.service';
import { VertepDatePipe } from '../shared/vertep-date.pipe';
import { Subscription, timer } from 'rxjs';
import { Job, QueueState, QueueTaskSummary, DeadLetterTask } from '../core/models';

@Component({
  selector: 'app-queue',
  standalone: true,
  imports: [CommonModule, RouterModule, VertepDatePipe],
  template: `
    <div class="space-y-6" data-testid="queue-page">
      <div class="flex items-center justify-between">
        <h3 class="text-lg font-semibold text-slate-900">Черга завдань</h3>
        <button (click)="loadQueue()" class="text-sm text-emerald-600 hover:text-emerald-700 font-medium">Оновити</button>
      </div>

      @if (loading()) {
        <div class="space-y-3">
          @for (_ of [1,2,3]; track $index) {
            <div class="animate-pulse bg-slate-100 rounded-lg h-16"></div>
          }
        </div>
      } @else if (error()) {
        <p class="text-red-600">{{ error() }}</p>
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
            <p class="text-xs text-slate-500">Dead-letter</p>
            <p class="text-2xl font-bold text-red-600">{{ deadLetterCount() }}</p>
          </div>
        </div>

        @if (scheduledJobs().length > 0) {
          <div>
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
          <div>
            <h4 class="text-sm font-medium text-slate-900 mb-2">Готові до виконання</h4>
            <div class="space-y-2">
              @for (task of readyTasks(); track task.task_id) {
                <div class="bg-slate-50 rounded-lg p-3 flex items-center justify-between">
                  <div>
                    <span class="text-sm font-medium text-slate-900">{{ task.task }}</span>
                    <p class="text-xs text-slate-500">Task: {{ task.task_id }} · Scene: {{ task.scene_id }}</p>
                  </div>
                  <a [routerLink]="['/jobs', task.job_id]" class="text-xs text-emerald-600 hover:text-emerald-700 font-medium">Відкрити</a>
                </div>
              }
            </div>
          </div>
        }

        @if (inflightTasks().length > 0) {
          <div>
            <h4 class="text-sm font-medium text-slate-900 mb-2">В процесі виконання</h4>
            <div class="space-y-2">
              @for (task of inflightTasks(); track task.task_id) {
                <div class="bg-slate-50 rounded-lg p-3 flex items-center justify-between">
                  <div>
                    <span class="text-sm font-medium text-slate-900">{{ task.task }}</span>
                    <p class="text-xs text-slate-500">Task: {{ task.task_id }} · Scene: {{ task.scene_id }}</p>
                  </div>
                  <a [routerLink]="['/jobs', task.job_id]" class="text-xs text-emerald-600 hover:text-emerald-700 font-medium">Відкрити</a>
                </div>
              }
            </div>
          </div>
        }

        @if (deadLetterTasks().length > 0) {
          <div>
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
  `,
})
export class QueueComponent implements OnInit, OnDestroy {
  loading = signal(false);
  error = signal<string | null>(null);
  readyCount = signal(0);
  inflightCount = signal(0);
  deadLetterCount = signal(0);
  scheduledJobs = signal<Job[]>([]);
  readyTasks = signal<QueueTaskSummary[]>([]);
  inflightTasks = signal<QueueTaskSummary[]>([]);
  deadLetterTasks = signal<DeadLetterTask[]>([]);
  retrying = signal<string | null>(null);
  private subs = new Subscription();
  private pollTimer: any = null;

  constructor(private api: VertepApiService, private toast: ToastService) {}

  ngOnInit(): void {
    this.loadQueue();
    this.pollTimer = timer(0, 5000).subscribe(() => this.loadQueue());
  }

  ngOnDestroy(): void {
    this.subs.unsubscribe();
    if (this.pollTimer) {
      this.pollTimer.unsubscribe();
    }
  }

  loadQueue(): void {
    this.loading.set(true);
    this.error.set(null);
    this.subs.add(
      this.api.getQueueState().subscribe({
        next: (state: QueueState) => {
          this.readyTasks.set(state.ready || []);
          this.inflightTasks.set(state.inflight || []);
          this.readyCount.set((state.ready || []).length);
          this.inflightCount.set((state.inflight || []).length);
          this.loading.set(false);
        },
        error: (err) => {
          this.error.set(err.message || 'Не вдалося завантажити чергу');
          this.loading.set(false);
        },
      }),
    );
    this.subs.add(
      this.api.getDeadLetterTasks().subscribe({
        next: (tasks) => {
          this.deadLetterTasks.set(tasks);
          this.deadLetterCount.set(tasks.length);
        },
        error: () => {},
      }),
    );
    this.subs.add(
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
    this.subs.add(
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
}
