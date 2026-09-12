import { Component, OnInit, signal, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterModule, ActivatedRoute, Router } from '@angular/router';
import { VertepApiService } from '../core/api.service';
import { ToastService } from '../core/services/toast.service';
import { ConfirmService } from '../core/services/confirm.service';
import { CanComponentDeactivate } from '../core/services/unsaved-guard.service';
import { Character } from '../core/models';

@Component({
  selector: 'app-character-detail',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="space-y-5" data-testid="character-detail-page">
      <div class="flex items-center gap-4">
        <a routerLink="/characters" class="text-emerald-600 hover:text-emerald-700 text-sm font-medium">&larr; Назад до персонажів</a>
        <h2 class="text-xl font-semibold text-slate-900">{{ name || 'Новий персонаж' }}</h2>
      </div>

      @if (loading()) {
        <div class="animate-pulse space-y-4">
          <div class="h-6 bg-slate-200 rounded w-3/4"></div>
          <div class="h-4 bg-slate-200 rounded w-1/2"></div>
          <div class="h-64 bg-slate-200 rounded"></div>
        </div>
      } @else if (error()) {
        <div class="bg-red-50 border border-red-200 rounded-xl p-4">
          <p class="text-red-700">{{ error() }}</p>
        </div>
      } @else {
        <div class="space-y-5">
          <div class="bg-white rounded-xl border border-slate-200 p-5">
            <h3 class="text-sm font-medium text-slate-500 mb-3">Основна інформація</h3>
            <div class="grid grid-cols-1 md:grid-cols-2 gap-4 text-sm">
              <div>
                <label class="block text-slate-500 mb-1">ID <span class="text-red-400">*</span></label>
                <input [(ngModel)]="formId" [disabled]="isEdit" data-testid="character-id"
                  class="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm font-mono disabled:bg-slate-50 disabled:text-slate-500 focus:outline-none focus:ring-2 focus:ring-emerald-500"
                  placeholder="unicorns-narrator">
              </div>
              <div>
                <label class="block text-slate-500 mb-1">Назва <span class="text-red-400">*</span></label>
                <input [(ngModel)]="name" data-testid="character-name"
                  class="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500">
              </div>
              <div>
                <label class="block text-slate-500 mb-1">Мова</label>
                <select [(ngModel)]="language" data-testid="character-language"
                  class="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500">
                  <option value="uk">Українська</option><option value="en">English</option>
                </select>
              </div>
              <div>
                <label class="block text-slate-500 mb-1">Workflow</label>
                <input [(ngModel)]="workflow" data-testid="character-workflow"
                  class="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500">
              </div>
              <div class="flex items-center gap-2 mt-4">
                <input type="checkbox" [(ngModel)]="enabled" id="char-enabled" data-testid="character-enabled"
                  class="rounded border-slate-300 text-emerald-600 focus:ring-emerald-500">
                <label for="char-enabled" class="text-sm text-slate-700">Активний</label>
              </div>
            </div>
          </div>
          <div class="bg-white rounded-xl border border-slate-200 p-5">
            <h3 class="text-sm font-medium text-slate-500 mb-3">Системний промпт</h3>
            <textarea [(ngModel)]="systemPrompt" rows="6" data-testid="character-system-prompt"
              class="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm font-mono focus:outline-none focus:ring-2 focus:ring-emerald-500"></textarea>
          </div>
          <div class="bg-white rounded-xl border border-slate-200 p-5">
            <div class="flex items-center justify-between mb-2">
              <h3 class="text-sm font-medium text-slate-500">Голос (voice.json)</h3>
              @if (voiceError()) { <span class="text-xs text-red-500">{{ voiceError() }}</span> }
            </div>
            <textarea [(ngModel)]="voiceJson" rows="6" data-testid="character-voice"
              class="w-full px-3 py-2 border border-slate-200 rounded-lg text-xs font-mono focus:outline-none focus:ring-2 focus:ring-emerald-500"
              (blur)="validateJson('voice')"></textarea>
          </div>
          <div class="bg-white rounded-xl border border-slate-200 p-5">
            <div class="flex items-center justify-between mb-2">
              <h3 class="text-sm font-medium text-slate-500">Візуал (visual.json)</h3>
              @if (visualError()) { <span class="text-xs text-red-500">{{ visualError() }}</span> }
            </div>
            <textarea [(ngModel)]="visualJson" rows="6" data-testid="character-visual"
              class="w-full px-3 py-2 border border-slate-200 rounded-lg text-xs font-mono focus:outline-none focus:ring-2 focus:ring-emerald-500"
              (blur)="validateJson('visual')"></textarea>
          </div>
          <div class="bg-white rounded-xl border border-slate-200 p-5">
            <div class="flex items-center justify-between mb-2">
              <h3 class="text-sm font-medium text-slate-500">Генерація (generation.json)</h3>
              @if (generationError()) { <span class="text-xs text-red-500">{{ generationError() }}</span> }
            </div>
            <textarea [(ngModel)]="generationJson" rows="6" data-testid="character-generation"
              class="w-full px-3 py-2 border border-slate-200 rounded-lg text-xs font-mono focus:outline-none focus:ring-2 focus:ring-emerald-500"
              (blur)="validateJson('generation')"></textarea>
          </div>
          <div class="bg-white rounded-xl border border-slate-200 p-5">
            <div class="flex items-center justify-between mb-2">
              <h3 class="text-sm font-medium text-slate-500">Публікація (publishing.json)</h3>
              @if (publishingError()) { <span class="text-xs text-red-500">{{ publishingError() }}</span> }
            </div>
            <textarea [(ngModel)]="publishingJson" rows="6" data-testid="character-publishing"
              class="w-full px-3 py-2 border border-slate-200 rounded-lg text-xs font-mono focus:outline-none focus:ring-2 focus:ring-emerald-500"
              (blur)="validateJson('publishing')"></textarea>
          </div>
          <div class="flex items-center justify-between pt-2">
            <div class="flex gap-2">
              <button (click)="save()" [disabled]="saving() || !formId || !name" data-testid="character-save-btn"
                class="px-4 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 disabled:opacity-50 text-sm font-medium">
                {{ saving() ? 'Збереження...' : (isEdit ? 'Зберегти' : 'Створити') }}
              </button>
              <a routerLink="/characters" class="px-4 py-2 text-sm text-slate-600 border border-slate-200 rounded-lg">Скасувати</a>
            </div>
            @if (isEdit) {
              <button (click)="deleteCharacter()" data-testid="character-delete-btn"
                class="px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700 text-sm font-medium">Видалити</button>
            }
          </div>
        </div>
      }
    </div>
  `,
})
export class CharacterDetailComponent implements OnInit, CanComponentDeactivate {
  loading = signal(true);
  error = signal<string | null>(null);
  saving = signal(false);
  voiceError = signal<string | null>(null);
  visualError = signal<string | null>(null);
  generationError = signal<string | null>(null);
  publishingError = signal<string | null>(null);

  isEdit = false;
  formId = '';
  name = '';
  language = 'uk';
  enabled = true;
  workflow = '';
  systemPrompt = '';
  voiceJson = '{}';
  visualJson = '{}';
  generationJson = '{}';
  publishingJson = '{}';
  private originalSnapshot = '';

  constructor(
    private route: ActivatedRoute,
    private router: Router,
    private api: VertepApiService,
    private toast: ToastService,
    private confirm: ConfirmService,
  ) {}

  ngOnInit(): void {
    const id = this.route.snapshot.paramMap.get('id');
    if (id) { this.isEdit = true; this.loadCharacter(id); } else { this.loading.set(false); }
  }

  loadCharacter(id: string): void {
    this.loading.set(true);
    this.error.set(null);
    this.api.getCharacter(id).subscribe({
      next: (c) => {
        this.formId = c.id ?? ''; this.name = c.name; this.language = c.language || 'uk';
        this.enabled = c.enabled !== false; this.workflow = c.workflow || '';
        this.systemPrompt = c.system_prompt || '';
        this.voiceJson = this.stringify(c.voice); this.visualJson = this.stringify(c.visual);
        this.generationJson = this.stringify(c.generation); this.publishingJson = this.stringify(c.publishing);
        this.originalSnapshot = this.snapshot(); this.loading.set(false);
      },
      error: (err) => { this.error.set(err.message || 'Не вдалося завантажити персонажа'); this.loading.set(false); },
    });
  }

  save(): void {
    if (!this.formId || !this.name) { this.toast.show('ID та назва обов\'язкові', 'error'); return; }
    this.saving.set(true);
    const payload: Character = {
      name: this.name, language: this.language, enabled: this.enabled,
      workflow: this.workflow || undefined, system_prompt: this.systemPrompt,
      voice: this.parseJson(this.voiceJson), visual: this.parseJson(this.visualJson),
      generation: this.parseJson(this.generationJson), publishing: this.parseJson(this.publishingJson),
    };
    const req = this.isEdit
      ? this.api.updateCharacter(this.formId, payload)
      : this.api.createCharacter(payload);
    req.subscribe({
      next: () => {
        this.saving.set(false);
        this.toast.show(this.isEdit ? 'Персонаж збережено' : 'Персонаж створено', 'success');
        if (!this.isEdit) { this.router.navigate(['/characters', this.formId]); } else { this.originalSnapshot = this.snapshot(); }
      },
      error: (err) => { this.saving.set(false); this.toast.show(err.message || 'Помилка збереження', 'error'); },
    });
  }

  deleteCharacter(): void {
    this.confirm.confirm({ title: 'Видалити персонажа', message: `Видалити ${this.formId}?` }).subscribe((ok) => {
      if (!ok) return;
      this.api.deleteCharacter(this.formId).subscribe({
        next: () => { this.toast.show('Персонаж видалено', 'success'); this.router.navigate(['/characters']); },
        error: (err) => this.toast.show(err.message || 'Помилка видалення', 'error'),
      });
    });
  }

  validateJson(field: 'voice' | 'visual' | 'generation' | 'publishing'): void {
    const map = { voice: this.voiceJson, visual: this.visualJson, generation: this.generationJson, publishing: this.publishingJson };
    const errMap = { voice: this.voiceError, visual: this.visualError, generation: this.generationError, publishing: this.publishingError };
    try { const p = JSON.parse(map[field]); if (typeof p !== 'object' || p === null) throw new Error(); errMap[field].set(null); }
    catch { errMap[field].set('Невалідний JSON'); }
  }

  hasUnsavedChanges(): boolean {
    return this.snapshot() !== this.originalSnapshot;
  }

  canDeactivate(): boolean {
    if (this.hasUnsavedChanges()) {
      return confirm('Є незбережені зміни. Закрити без збереження?');
    }
    return true;
  }
  private snapshot(): string {
    return JSON.stringify({ id: this.formId, name: this.name, language: this.language, enabled: this.enabled,
      workflow: this.workflow, sp: this.systemPrompt, vj: this.voiceJson, vij: this.visualJson,
      gj: this.generationJson, pj: this.publishingJson });
  }
  private stringify(val: unknown): string {
    if (!val || (typeof val === 'object' && Object.keys(val as Record<string, unknown>).length === 0)) return '{}';
    try { return JSON.stringify(val, null, 2); } catch { return '{}'; }
  }
  private parseJson(text: string): Record<string, unknown> {
    try { const p = JSON.parse(text); return typeof p === 'object' && p !== null ? p : {}; } catch { return {}; }
  }
}
