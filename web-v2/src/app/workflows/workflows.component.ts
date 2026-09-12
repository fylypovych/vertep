import { Component, OnInit, signal, computed, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterModule } from '@angular/router';
import { VertepApiService } from '../core/api.service';
import { ToastService } from '../core/services/toast.service';
import { ConfirmService } from '../core/services/confirm.service';
import { Workflow, Character } from '../core/models';

@Component({
  selector: 'app-workflows',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterModule],
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
        <div class="animate-pulse space-y-3">
          <div class="h-5 bg-slate-200 rounded w-full"></div>
          <div class="h-5 bg-slate-200 rounded w-3/4"></div>
          <div class="h-5 bg-slate-200 rounded w-1/2"></div>
        </div>
      } @else if (error()) {
        <div class="bg-red-50 border border-red-200 rounded-xl p-4">
          <p class="text-red-700">{{ error() }}</p>
          <button (click)="loadWorkflows()" class="mt-2 text-sm text-red-600 hover:text-red-700 font-medium">Повторити</button>
        </div>
      } @else if (workflows().length === 0) {
        <div class="text-center text-slate-500 py-10" data-testid="workflows-empty">Сценаріїв не знайдено</div>
      } @else {
        <div class="overflow-x-auto">
          <table class="w-full text-sm text-left" data-testid="workflows-table">
            <thead class="text-xs text-slate-500 uppercase bg-slate-50">
              <tr>
                <th class="px-4 py-3">Тип</th>
                <th class="px-4 py-3">Назва</th>
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
                    @if (usageMap()[wf.kind + '/' + wf.name]?.length) {
                      <span class="text-xs text-slate-600">{{ usageMap()[wf.kind + '/' + wf.name]!.join(', ') }}</span>
                    } @else {
                      <span class="text-xs text-slate-400">—</span>
                    }
                  </td>
                  <td class="px-4 py-3 space-x-2">
                    <button (click)="editWorkflow(wf)" class="text-sm text-blue-600 hover:text-blue-700 font-medium">Редагувати</button>
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
        <div class="bg-white rounded-xl p-6 w-full max-w-3xl mx-4 max-h-[85vh] flex flex-col">
          <h3 class="text-lg font-semibold text-slate-900 mb-4">{{ editingWf ? 'Редагувати' : 'Новий' }} сценарій</h3>
          <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-4">
            <div>
              <label class="block text-sm font-medium text-slate-700 mb-1">Тип</label>
              <select [(ngModel)]="editorForm.kind" [disabled]="!!editingWf" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-sm">
                <option value="image">Image</option>
                <option value="video">Video</option>
                <option value="audio">Audio</option>
              </select>
            </div>
            <div>
              <label class="block text-sm font-medium text-slate-700 mb-1">Назва</label>
              <input [(ngModel)]="editorForm.name" placeholder="наприклад, demo" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-sm">
            </div>
          </div>
          <div class="flex-1 overflow-y-auto">
            <label class="block text-sm font-medium text-slate-700 mb-1">Конфігурація (JSON)</label>
            <textarea [(ngModel)]="editorForm.content" rows="25" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-xs font-mono resize-none" spellcheck="false"></textarea>
          </div>
          <div class="flex justify-end gap-2 mt-4">
            <button (click)="closeEditor()" class="px-4 py-2 text-slate-600 hover:text-slate-800 text-sm font-medium">Скасувати</button>
            <button (click)="saveWorkflow()" [disabled]="saving()" class="px-4 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 text-sm font-medium disabled:opacity-50">{{ saving() ? 'Збереження...' : 'Зберегти' }}</button>
          </div>
        </div>
      </div>
    </div>
  `,
})
export class WorkflowsComponent implements OnInit {
  workflows = signal<Workflow[]>([]);
  usage = signal<Character[]>([]);
  loading = signal(false);
  error = signal<string | null>(null);
  saving = signal(false);
  showEditor = signal(false);
  editingWf: Workflow | null = null;
  editorForm: { kind: string; name: string; content: string } = { kind: 'image', name: '', content: '' };

  constructor(
    private api: VertepApiService,
    private toast: ToastService,
    private confirm: ConfirmService,
  ) {}

  ngOnInit(): void {
    this.loadWorkflows();
  }

  usageMap = computed(() => {
    const map: Record<string, string[]> = {};
    for (const char of this.usage()) {
      const key = char.workflow;
      if (key && key.startsWith('workflows/')) {
        const ref = key.replace('workflows/', '');
        const [kind, name] = ref.split('/');
        const fullKey = kind + '/' + (name || '');
        if (!map[fullKey]) map[fullKey] = [];
        map[fullKey].push(char.name);
      }
    }
    return map;
  });

  loadWorkflows(): void {
    this.loading.set(true);
    this.error.set(null);
    this.api.getWorkflows().subscribe({
      next: (wfs) => { this.workflows.set(wfs); this.loading.set(false); },
      error: (err) => { this.error.set(err.message); this.loading.set(false); },
    });
    this.api.getCharacters().subscribe({
      next: (chars) => this.usage.set(chars),
      error: () => this.usage.set([]),
    });
  }

  openEditor(): void {
    this.editingWf = null;
    this.editorForm = { kind: 'image', name: '', content: '' };
    this.showEditor.set(true);
  }

  editWorkflow(wf: Workflow): void {
    this.editingWf = wf;
    this.editorForm = { kind: wf.kind, name: wf.name, content: '' };
    this.api.getWorkflow(wf.kind, wf.name).subscribe({
      next: (data) => {
        this.editorForm.content = JSON.stringify(data, null, 2);
        this.showEditor.set(true);
      },
      error: (err) => this.toast.show(err.message || 'Не вдалося завантажити сценарій', 'error'),
    });
  }

  closeEditor(): void {
    if (this.hasUnsavedChanges()) {
      if (!confirm('Є незбережені зміни. Закрити без збереження?')) return;
    }
    this.showEditor.set(false);
    this.editingWf = null;
    this.editorForm = { kind: 'image', name: '', content: '' };
  }

  hasUnsavedChanges(): boolean {
    if (!this.editorForm.name.trim() && !this.editorForm.content.trim()) return false;
    if (this.editingWf) {
      return this.editorForm.content.trim() !== JSON.stringify(this.editingWf, null, 2).trim();
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
      this.api.saveWorkflow(this.editingWf.kind, this.editingWf.name, content).subscribe({
        next: () => {
          this.closeEditor();
          this.loadWorkflows();
          this.saving.set(false);
          this.toast.show('Сценарій збережено', 'success');
        },
        error: (err) => { this.saving.set(false); this.toast.show(err.message || 'Помилка збереження', 'error'); },
      });
    } else {
      this.api.saveWorkflow(this.editorForm.kind, this.editorForm.name, content).subscribe({
        next: () => {
          this.closeEditor();
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
      this.api.deleteWorkflow(wf.kind, wf.name).subscribe({
        next: () => { this.loadWorkflows(); this.toast.show('Сценарій видалено', 'success'); },
        error: (err) => {
          if (err.message?.includes('409')) {
            this.toast.show('Сценарій використовується персонажами або завданнями', 'error');
          } else {
            this.toast.show(err.message || 'Помилка видалення', 'error');
          }
        },
      });
    });
  }
}
