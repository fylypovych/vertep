import { Component, OnInit, signal, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterModule } from '@angular/router';
import { forkJoin, of } from 'rxjs';
import { catchError, map } from 'rxjs/operators';
import { ResourcesApiService } from '../core/api/resources.api';
import { ToastService } from '../core/services/toast.service';
import { ConfirmService } from '../core/services/confirm.service';
import { Workflow, WorkflowValidation, WorkflowVersion, WorkflowUsage, WorkflowFormSchema } from '../core/models';
import { LoadingStateComponent } from '../shared/loading-state.component';
import { ErrorStateComponent } from '../shared/error-state.component';

function coerceInputValue(value: unknown, type?: string): unknown {
  if (type === 'str') return String(value);
  if (type === 'bool') {
    if (value === true || value === 'true') return true;
    if (value === false || value === 'false') return false;
    throw new Error('Логічне поле має містити true або false');
  }
  if (type === 'int' || type === 'float') {
    const number = Number(value);
    if (String(value).trim() === '' || !Number.isFinite(number)
        || (type === 'int' && !Number.isInteger(number))) {
      throw new Error(type === 'int' ? 'Поле має містити ціле число' : 'Поле має містити число');
    }
    return number;
  }
  return value;
}

@Component({
  selector: 'app-workflows',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterModule, LoadingStateComponent, ErrorStateComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="space-y-5" data-testid="workflows-page">
      <div class="flex items-center justify-between">
        <h2 class="text-xl font-semibold text-slate-900">Реєстр сценаріїв</h2>
        <button (click)="openEditor()" data-testid="create-workflow-button"
                class="px-4 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 text-sm font-medium">
          Новий сценарій
        </button>
      </div>

      @if (loading()) {
        <app-loading-state />
      } @else if (error()) {
        <app-error-state [message]="error()!" (retry)="loadWorkflows()" />
      } @else if (workflows().length === 0) {
        <div class="text-center text-slate-500 py-10" data-testid="workflows-empty">Сценаріїв не знайдено</div>
      } @else {
        <div class="overflow-x-auto">
          <table class="w-full text-sm text-left" data-testid="workflows-table">
            <thead class="text-xs text-slate-500 uppercase bg-slate-50">
              <tr>
                <th class="px-4 py-3">Тип</th>
                <th class="px-4 py-3">Назва</th>
                <th class="px-4 py-3">Стан валідації</th>
                <th class="px-4 py-3">Використовується</th>
                <th class="px-4 py-3">Дії</th>
              </tr>
            </thead>
            <tbody>
              @for (wf of workflows(); track wf.kind + '/' + wf.name) {
                <tr class="border-t border-slate-100">
                  <td class="px-4 py-3">{{ wf.kind }}</td>
                  <td class="px-4 py-3 font-medium">{{ wf.name }}</td>
                  <td class="px-4 py-3">
                    @if (wf.valid === false) {
                      <span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-red-100 text-red-800" data-testid="workflow-validation-status">Невалідний</span>
                    } @else {
                      <span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-emerald-100 text-emerald-800" data-testid="workflow-validation-status">Валідний</span>
                    }
                  </td>
                  <td class="px-4 py-3">
                    @if (usageMap()[wf.kind + '/' + wf.name]?.length) {
                      <span class="text-xs text-slate-600">{{ usageMap()[wf.kind + '/' + wf.name]!.join(', ') }}</span>
                    } @else {
                      <span class="text-xs text-slate-400">—</span>
                    }
                  </td>
                  <td class="px-4 py-3 space-x-2">
                    <button (click)="editWorkflow(wf)" class="text-sm text-blue-600 hover:text-blue-700 font-medium">Редагувати</button>
                    <button (click)="viewVersions(wf)" class="text-sm text-amber-600 hover:text-amber-700 font-medium">Історія</button>
                    <button (click)="deleteWorkflow(wf)" class="text-sm text-red-600 hover:text-red-700 font-medium">Видалити</button>
                  </td>
                </tr>
              }
            </tbody>
          </table>
        </div>
      }

      <!-- Editor Modal -->
      <div *ngIf="showEditor()" data-testid="workflow-editor-modal" class="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
        <div class="bg-white rounded-xl p-6 w-full max-w-4xl mx-4 max-h-[85vh] flex flex-col">
          <div class="flex items-center justify-between mb-4 border-b pb-3">
            <h3 class="text-lg font-semibold text-slate-900">{{ editingWf ? 'Редагувати' : 'Новий' }} сценарій</h3>
            <div class="flex items-center gap-2">
              <button (click)="activeTab.set('json')" [class.bg-emerald-600]="activeTab() === 'json'" [class.text-white]="activeTab() === 'json'" [class.bg-slate-100]="activeTab() !== 'json'" class="px-3 py-1.5 rounded-lg text-xs font-medium">JSON</button>
              <button (click)="activeTab.set('form')" [class.bg-emerald-600]="activeTab() === 'form'" [class.text-white]="activeTab() === 'form'" [class.bg-slate-100]="activeTab() !== 'form'" class="px-3 py-1.5 rounded-lg text-xs font-medium">Форма</button>
              <button (click)="activeTab.set('report')" [class.bg-emerald-600]="activeTab() === 'report'" [class.text-white]="activeTab() === 'report'" [class.bg-slate-100]="activeTab() !== 'report'" class="px-3 py-1.5 rounded-lg text-xs font-medium">Звіт валідації</button>
            </div>
          </div>

          <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-4">
            <div>
              <label class="block text-sm font-medium text-slate-700 mb-1">Тип</label>
              <select [(ngModel)]="editorForm.kind" [disabled]="!!editingWf" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-sm">
                <option value="image">Image</option>
                <option value="video">Video</option>
                <option value="character">Character</option>
              </select>
            </div>
            <div>
              <label class="block text-sm font-medium text-slate-700 mb-1">Назва</label>
              <input [(ngModel)]="editorForm.name" data-testid="workflow-name-input" placeholder="наприклад, demo.json" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-sm">
            </div>
          </div>

          <div class="flex-1 overflow-y-auto min-h-[300px]">
            @if (activeTab() === 'json') {
              <div>
                <label class="block text-sm font-medium text-slate-700 mb-1">Конфігурація (JSON)</label>
                <textarea [(ngModel)]="editorForm.content" (ngModelChange)="onJsonChange()" rows="18" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-xs font-mono resize-none" spellcheck="false"></textarea>
              </div>
            } @else if (activeTab() === 'form') {
              <div class="space-y-4" data-testid="workflow-form-view">
                @if (formSchema() && (formSchema()!.schema.editable_inputs || []).length > 0) {
                  <h4 class="text-sm font-semibold text-slate-800">Редаговані входи нод</h4>
                  <div class="space-y-3">
                    @for (item of formSchema()!.schema.editable_inputs; track item.node_id + '_' + item.input_name) {
                      <div class="p-3 border border-slate-200 rounded-lg bg-slate-50 text-xs">
                        <div class="font-semibold text-slate-700">Нода {{ item.node_id }} ({{ item.class_type }}) — {{ item.input_name }}</div>
                        <input [(ngModel)]="item.current_value" (change)="updateJsonFromForm()" class="mt-1 w-full px-2 py-1 border border-slate-300 rounded text-xs">
                      </div>
                    }
                  </div>
                } @else {
                  <div class="text-slate-500 text-sm py-6 text-center">Немає доступних генеративних полів у формі для цього сценарію.</div>
                }
              </div>
            } @else if (activeTab() === 'report') {
              <div class="space-y-4 text-xs" data-testid="workflow-validation-report">
                @if (validationReport()) {
                  <div class="p-3 rounded-lg border" [class.bg-emerald-50]="validationReport()!.valid" [class.border-emerald-200]="validationReport()!.valid" [class.bg-red-50]="!validationReport()!.valid" [class.border-red-200]="!validationReport()!.valid">
                    <div class="font-semibold text-sm" [class.text-emerald-800]="validationReport()!.valid" [class.text-red-800]="!validationReport()!.valid">
                      {{ validationReport()!.valid ? 'Сценарій валідний' : 'Сценарій містить помилки' }}
                    </div>
                  </div>
                  @if (validationReport()!.errors.length) {
                    <div>
                      <div class="font-semibold text-red-700 mb-1">Помилки:</div>
                      <ul class="list-disc pl-4 text-red-600 space-y-1">
                        @for (err of validationReport()!.errors; track err) {
                          <li>{{ err }}</li>
                        }
                      </ul>
                    </div>
                  }
                  @if (validationReport()!.warnings.length) {
                    <div>
                      <div class="font-semibold text-amber-700 mb-1">Попередження:</div>
                      <ul class="list-disc pl-4 text-amber-600 space-y-1">
                        @for (warn of validationReport()!.warnings; track warn) {
                          <li>{{ warn }}</li>
                        }
                      </ul>
                    </div>
                  }
                  <div class="p-3 bg-slate-50 border border-slate-200 rounded-lg space-y-1">
                    <div><span class="font-medium">Кількість нод:</span> {{ validationReport()!.schema.node_count }}</div>
                    <div><span class="font-medium">Типи нод:</span> {{ validationReport()!.schema.node_types.join(', ') || '—' }}</div>
                    <div><span class="font-medium">Плейсхолдери:</span> {{ validationReport()!.schema.has_placeholders.join(', ') || '—' }}</div>
                  </div>
                } @else if (!editorForm.content.trim()) {
                  <div class="text-slate-500 py-6 text-center">Немає вмісту для валідації. Введіть JSON сценарію.</div>
                } @else {
                  <div class="text-slate-500 py-6 text-center">Завантаження звіту валідації...</div>
                }
              </div>
            }
          </div>

          <div class="flex justify-end gap-2 mt-4 pt-3 border-t">
            <button (click)="closeEditor()" class="px-4 py-2 text-slate-600 hover:text-slate-800 text-sm font-medium">Скасувати</button>
            <button (click)="saveWorkflow()" [disabled]="saving()" class="px-4 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 text-sm font-medium disabled:opacity-50">{{ saving() ? 'Збереження...' : 'Зберегти' }}</button>
          </div>
        </div>
      </div>

      <!-- Versions Modal -->
      <div *ngIf="showVersions()" data-testid="workflow-versions-modal" class="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
        <div class="bg-white rounded-xl p-6 w-full max-w-2xl mx-4 max-h-[80vh] flex flex-col">
          <h3 class="text-lg font-semibold text-slate-900 mb-4">Історія версій: {{ activeVersionWf?.kind }}/{{ activeVersionWf?.name }}</h3>
          <div class="flex-1 overflow-y-auto">
            @if (versionsList().length === 0) {
              <div class="text-slate-500 text-center py-6 text-sm">Немає збережених попередніх версій</div>
            } @else {
              <div class="space-y-2">
                @for (ver of versionsList(); track ver.version) {
                  <div class="p-3 border border-slate-200 rounded-lg flex items-center justify-between text-xs">
                    <div>
                      <span class="font-semibold text-slate-800">Версія {{ ver.version }}</span>
                      <span class="text-slate-500 ml-2">({{ ver.size_bytes }} B)</span>
                      @if (ver.archived_at) {
                        <div class="text-slate-400 text-[11px]">{{ ver.archived_at }}</div>
                      }
                    </div>
                    <button (click)="restoreVersion(ver.version)" class="px-3 py-1 bg-amber-600 text-white rounded hover:bg-amber-700 font-medium text-xs">Відновити</button>
                  </div>
                }
              </div>
            }
          </div>
          <div class="flex justify-end gap-2 mt-4 pt-3 border-t">
            <button (click)="showVersions.set(false)" class="px-4 py-2 text-slate-600 hover:text-slate-800 text-sm font-medium">Закрити</button>
          </div>
        </div>
      </div>

      <!-- Dependency Conflict Modal -->
      <div *ngIf="showConflictModal()" data-testid="workflow-conflict-modal" class="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
        <div class="bg-white rounded-xl p-6 w-full max-w-lg mx-4 flex flex-col">
          <h3 class="text-lg font-semibold text-red-700 mb-2">Конфлікт видалення (409)</h3>
          <p class="text-sm text-slate-600 mb-4">{{ conflictMessage() }}</p>
          @if (conflictUsage()) {
            <div class="space-y-3 mb-4 text-xs">
              @if (conflictUsage()!.characters.length) {
                <div>
                  <div class="font-semibold text-slate-700">Персонажі:</div>
                  <ul class="list-disc pl-4 text-slate-600">
                    @for (char of conflictUsage()!.characters; track char.character_id) {
                      <li>{{ char.character_id }} (поле: {{ char.field }})</li>
                    }
                  </ul>
                </div>
              }
              @if (conflictUsage()!.jobs.length) {
                <div>
                  <div class="font-semibold text-slate-700">Завдання:</div>
                  <ul class="list-disc pl-4 text-slate-600">
                    @for (job of conflictUsage()!.jobs; track job.job_id) {
                      <li>{{ job.job_id }} (тема: {{ job.topic }})</li>
                    }
                  </ul>
                </div>
              }
            </div>
          }
          <div class="flex justify-end gap-2 pt-3 border-t">
            <button (click)="showConflictModal.set(false)" class="px-4 py-2 text-slate-600 hover:text-slate-800 text-sm font-medium">Скасувати</button>
            <button (click)="forceDeleteWorkflow()" class="px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700 text-sm font-medium">Видалити примусово (force)</button>
          </div>
        </div>
      </div>

    </div>
  `,
})
export class WorkflowsComponent implements OnInit {
  workflows = signal<Workflow[]>([]);
  loading = signal(false);
  error = signal<string | null>(null);
  saving = signal(false);
  showEditor = signal(false);
  activeTab = signal<'json' | 'form' | 'report'>('json');
  editingWf: Workflow | null = null;
  editorForm: { kind: string; name: string; content: string } = { kind: 'image', name: '', content: '' };
  validationReport = signal<WorkflowValidation | null>(null);
  formSchema = signal<WorkflowFormSchema | null>(null);
  private validateTimer: ReturnType<typeof setTimeout> | null = null;
  private originalContent = '';

  showVersions = signal(false);
  activeVersionWf: Workflow | null = null;
  versionsList = signal<WorkflowVersion[]>([]);

  showConflictModal = signal(false);
  conflictMessage = signal('');
  conflictUsage = signal<WorkflowUsage | null>(null);
  pendingDeleteWf: Workflow | null = null;

  constructor(
    private resources: ResourcesApiService,
    private toast: ToastService,
    private confirm: ConfirmService,
  ) {}

  ngOnInit(): void {
    this.loadWorkflows();
  }

  usageMap = signal<Record<string, string[]>>({});

  loadUsageMap(items: Workflow[]): void {
    if (items.length === 0) {
      this.usageMap.set({});
      return;
    }
    forkJoin(
      items.map((wf) =>
        this.resources.workflowUsage(wf.kind, wf.name).pipe(
          map((usage) => [wf.kind + '/' + wf.name, this.usageLabels(usage)] as const),
          catchError(() => of([wf.kind + '/' + wf.name, [] as string[]] as const))
        )
      )
    ).subscribe((entries) => this.usageMap.set(Object.fromEntries(entries)));
  }

  private usageLabels(usage: WorkflowUsage | null): string[] {
    if (!usage) return [];
    return [
      ...usage.characters.map((char) => `персонаж: ${char.character_id}`),
      ...usage.jobs.map((job) => `завдання: ${job.job_id}`),
    ];
  }

  loadWorkflows(): void {
    this.loading.set(true);
    this.error.set(null);
    this.resources.workflows().subscribe({
      next: (wfs) => {
        this.workflows.set(wfs);
        this.loading.set(false);
        this.loadUsageMap(wfs);
      },
      error: (err) => { this.error.set(err.message); this.loading.set(false); this.usageMap.set({}); },
    });
  }

  openEditor(): void {
    this.editingWf = null;
    this.editorForm = { kind: 'image', name: '', content: '' };
    this.originalContent = '';
    this.activeTab.set('json');
    this.validationReport.set(null);
    this.formSchema.set(null);
    this.showEditor.set(true);
  }

  editWorkflow(wf: Workflow): void {
    this.editingWf = wf;
    this.editorForm = { kind: wf.kind, name: wf.name, content: '' };
    this.originalContent = '';
    this.activeTab.set('json');
    this.resources.workflow(wf.kind, wf.name).subscribe({
      next: (data) => {
        this.editorForm.content = JSON.stringify(data.workflow, null, 2);
        this.originalContent = this.editorForm.content;
        this.validationReport.set(data.validation);
        this.showEditor.set(true);
        this.loadFormSchema(wf.kind, wf.name);
      },
      error: (err) => this.toast.show(err.message || 'Не вдалося завантажити сценарій', 'error'),
    });
  }

  loadFormSchema(kind: string, name: string): void {
    this.resources.workflowForm(kind, name).subscribe({
      next: (schema) => this.formSchema.set(schema),
      error: () => this.formSchema.set(null),
    });
  }

  onJsonChange(): void {
    if (this.validateTimer) clearTimeout(this.validateTimer);
    this.validateTimer = setTimeout(() => this.runValidation(), 250);
  }

  private runValidation(): void {
    const raw = this.editorForm.content.trim();
    if (!raw) {
      this.validationReport.set(null);
      return;
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch {
      this.validationReport.set({
        valid: false,
        errors: ['Невалідний формат JSON'],
        warnings: [],
        schema: { node_count: 0, node_types: [], has_placeholders: [] },
      });
      return;
    }
    this.resources.validateWorkflow(parsed).subscribe({
      next: (report) => this.validationReport.set(report),
      error: (err) => this.toast.show(err.message || 'Помилка валідації', 'error'),
    });
  }

  updateJsonFromForm(): void {
    const schema = this.formSchema();
    if (!schema) return;
    try {
      const parsed = JSON.parse(this.editorForm.content);
      for (const item of schema.schema.editable_inputs || []) {
        if (parsed[item.node_id] && parsed[item.node_id].inputs) {
          parsed[item.node_id].inputs[item.input_name] = coerceInputValue(item.current_value, item.type);
          item.current_value = parsed[item.node_id].inputs[item.input_name];
        }
      }
      this.editorForm.content = JSON.stringify(parsed, null, 2);
    } catch (error) {
      this.toast.show(error instanceof Error ? error.message : 'Некоректне значення поля', 'error');
      return;
    }
    this.runValidation();
  }

  viewVersions(wf: Workflow): void {
    this.activeVersionWf = wf;
    this.resources.workflowVersions(wf.kind, wf.name).subscribe({
      next: (list) => {
        this.versionsList.set(list);
        this.showVersions.set(true);
      },
      error: (err) => this.toast.show(err.message || 'Помилка завантаження історії', 'error'),
    });
  }

  restoreVersion(version: number): void {
    if (!this.activeVersionWf) return;
    const wf = this.activeVersionWf;
    this.resources.restoreWorkflowVersion(wf.kind, wf.name, version).subscribe({
      next: () => {
        this.toast.show(`Версію ${version} успішно відновлено`, 'success');
        this.showVersions.set(false);
        this.loadWorkflows();
      },
      error: (err) => this.toast.show(err.message || 'Помилка відновлення версії', 'error'),
    });
  }

  closeEditor(saved = false): void {
    if (!saved && this.hasUnsavedChanges()) {
      if (!confirm('Є незбережені зміни. Закрити без збереження?')) return;
    }
    this.cancelPendingValidation();
    this.showEditor.set(false);
    this.editingWf = null;
    this.editorForm = { kind: 'image', name: '', content: '' };
    this.validationReport.set(null);
    this.formSchema.set(null);
    this.activeTab.set('json');
  }

  private cancelPendingValidation(): void {
    if (this.validateTimer) {
      clearTimeout(this.validateTimer);
      this.validateTimer = null;
    }
  }

  hasUnsavedChanges(): boolean {
    if (!this.editorForm.name.trim() && !this.editorForm.content.trim()) return false;
    if (this.editingWf) {
      return this.editorForm.content.trim() !== (this.originalContent || '').trim();
    }
    return this.editorForm.name.trim().length > 0 || this.editorForm.content.trim().length > 0;
  }

  saveWorkflow(): void {
    if (!this.editorForm.name.trim()) {
      this.toast.show('Назва є обов\'язковою', 'error');
      return;
    }
    this.saving.set(true);
    let content: Record<string, unknown>;
    try {
      content = this.editorForm.content ? JSON.parse(this.editorForm.content) : {};
    } catch {
      this.toast.show('Невалідний JSON', 'error');
      this.saving.set(false);
      return;
    }

    if (this.editingWf) {
      this.resources.saveWorkflow(this.editingWf.kind, this.editingWf.name, content, true).subscribe({
        next: () => {
          this.closeEditor(true);
          this.loadWorkflows();
          this.saving.set(false);
          this.toast.show('Сценарій збережено', 'success');
        },
        error: (err) => { this.saving.set(false); this.toast.show(err.message || 'Помилка збереження', 'error'); },
      });
    } else {
      this.resources.saveWorkflow(this.editorForm.kind, this.editorForm.name, content).subscribe({
        next: () => {
          this.closeEditor(true);
          this.loadWorkflows();
          this.saving.set(false);
          this.toast.show('Сценарій створено', 'success');
        },
        error: (err) => { this.saving.set(false); this.toast.show(err.message || 'Помилка створення', 'error'); },
      });
    }
  }

  deleteWorkflow(wf: Workflow): void {
    this.confirm.confirm({ title: 'Видалити сценарій', message: `Ви впевнені, що хочете видалити ${wf.kind}/${wf.name}?` }).subscribe((ok) => {
      if (!ok) return;
      this.resources.deleteWorkflow(wf.kind, wf.name).subscribe({
        next: () => { this.loadWorkflows(); this.toast.show('Сценарій видалено', 'success'); },
        error: (err) => {
          const detail = (err as { detail?: { message?: string; usage?: WorkflowUsage } })?.detail;
          if (detail && typeof detail === 'object' && detail.usage) {
            this.pendingDeleteWf = wf;
            this.conflictMessage.set(detail.message || 'Сценарій використовується');
            this.conflictUsage.set(detail.usage);
            this.showConflictModal.set(true);
          } else if (err.status === 409) {
            this.pendingDeleteWf = wf;
            this.conflictMessage.set(detail?.message || err.message || 'Сценарій використовується у персонажах або завданнях');
            this.conflictUsage.set(null);
            this.showConflictModal.set(true);
          } else {
            this.toast.show(err.message || 'Помилка видалення', 'error');
          }
        },
      });
    });
  }

  forceDeleteWorkflow(): void {
    if (!this.pendingDeleteWf) return;
    const wf = this.pendingDeleteWf;
    this.resources.deleteWorkflow(wf.kind, wf.name, true).subscribe({
      next: () => {
        this.showConflictModal.set(false);
        this.pendingDeleteWf = null;
        this.loadWorkflows();
        this.toast.show('Сценарій видалено примусово', 'success');
      },
      error: (err) => this.toast.show(err.message || 'Помилка примусового видалення', 'error'),
    });
  }
}
