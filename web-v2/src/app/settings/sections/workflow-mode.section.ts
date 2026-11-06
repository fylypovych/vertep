import { Component, OnInit, computed, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { SettingsApiService, WorkflowModeState } from '../../core/api/settings.api';
import { ToastService } from '../../core/services/toast.service';
import { PolicyService } from '../../core/services/policy.service';

/** Settings → Режим workflow: static pipeline (default) or configured dynamic workflows. */
@Component({
  selector: 'app-settings-workflow-mode',
  standalone: true,
  imports: [CommonModule, FormsModule],
  template: `<div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-workflow-mode">
    <h3 class="text-lg font-semibold mb-1">Режим workflow</h3>
    <p class="text-sm text-slate-500 mb-4">Визначає, як нові завдання проходять шлях від теми до готового відео.</p>
    <div *ngIf="error()" class="text-red-600 text-sm mb-2" data-testid="workflow-mode-error">{{ error() }}</div>
    <div *ngIf="state() as current" class="grid grid-cols-1 md:grid-cols-2 gap-3">
      <label class="text-sm" for="workflow-mode-choice">Режим</label>
      <div class="flex items-center gap-2">
        <select id="workflow-mode-choice" class="border rounded px-2 py-1 grow" data-testid="workflow-mode-select"
                [ngModel]="choice()" (ngModelChange)="choice.set($any($event)); infoOpen.set(false)"
                [disabled]="!canEdit() || saving()">
          <option value="static">Статичний workflow</option>
          <option value="dynamic" [disabled]="!current.dynamic_available">Динамічний workflow</option>
        </select>
        <button type="button" class="w-6 h-6 shrink-0 rounded-full border border-amber-500 text-amber-700 text-sm font-bold"
                data-testid="workflow-mode-info-toggle" aria-label="Пояснення до обраного режиму"
                [attr.aria-expanded]="infoOpen()" [title]="explanation()"
                (click)="infoOpen.set(!infoOpen())">!</button>
      </div>
      <p *ngIf="infoOpen()" class="md:col-start-2 text-sm text-slate-600 bg-slate-50 border border-slate-200 rounded p-2"
         data-testid="workflow-mode-info">{{ explanation() }}</p>
      <p *ngIf="!current.dynamic_available" class="md:col-span-2 text-sm text-amber-700" data-testid="workflow-mode-unavailable">
        Динамічний workflow недоступний: на сервері немає налаштованих шаблонів процесу.
      </p>
      <p *ngIf="blockedByActiveJobs()" class="md:col-span-2 text-sm text-amber-700" data-testid="workflow-mode-blocked">
        Повернення до статичного режиму заборонене: незавершених завдань динамічного режиму {{ current.active_dynamic_jobs }}. Спершу завершіть або скасуйте їх.
      </p>
      <div class="md:col-span-2">
        <button class="px-3 py-1 bg-emerald-600 text-white rounded text-sm" data-testid="workflow-mode-apply"
                [disabled]="!canEdit() || saving() || choice() === current.mode || blockedByActiveJobs()" (click)="apply()">Застосувати</button>
      </div>
    </div>
  </div>`,
})
export class WorkflowModeSectionComponent implements OnInit {
  state = signal<WorkflowModeState | null>(null);
  choice = signal<'static' | 'dynamic'>('static');
  infoOpen = signal(false);
  saving = signal(false);
  error = signal<string | null>(null);

  readonly blockedByActiveJobs = computed(() => this.choice() === 'static' && !!this.state()?.active_dynamic_jobs);
  readonly canEdit = computed(() => this.policy.can('update_settings').allowed);
  readonly explanation = computed(() => this.choice() === 'dynamic'
    ? 'Динамічний workflow: кроки беруться з файлу-шаблону, тому можна задавати залежності, паралельні кроки, повтори й паузу без зміни програми. Режим ще не перевірений на реальному сервері. Завдання, запущені в цьому режимі, не продовжаться після повернення до статичного, доки режим знову не ввімкнуть.'
    : 'Статичний workflow: завдання проходить фіксований шлях: сценарій, зображення, відео, монтаж, публікація. Це типовий режим, для нього не потрібні файли-шаблони.');

  constructor(
    private settingsApi: SettingsApiService,
    private toast: ToastService,
    private policy: PolicyService,
  ) {}

  ngOnInit(): void {
    this.settingsApi.workflowMode().subscribe({
      next: (state) => { this.state.set(state); this.choice.set(state.mode); },
      error: (err) => this.error.set(err?.error?.detail || err?.message || 'Не вдалося завантажити режим workflow'),
    });
  }

  apply(): void {
    this.saving.set(true);
    this.error.set(null);
    this.settingsApi.setWorkflowMode(this.choice()).subscribe({
      next: (state) => {
        this.saving.set(false);
        this.state.set(state);
        this.choice.set(state.mode);
        this.toast.show('Режим workflow змінено', 'success');
      },
      error: (err) => {
        this.saving.set(false);
        this.error.set(err?.error?.detail || err?.message || 'Не вдалося змінити режим workflow');
      },
    });
  }
}
