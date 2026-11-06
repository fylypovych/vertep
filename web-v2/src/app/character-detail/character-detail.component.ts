import { TPipe } from '../core/i18n/t.pipe';
import { Component, OnInit, signal, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterModule, ActivatedRoute, Router } from '@angular/router';
import { CharacterReference, ResourcesApiService } from '../core/api/resources.api';
import { ToastService } from '../core/services/toast.service';
import { ConfirmService } from '../core/services/confirm.service';
import { PolicyService } from '../core/services/policy.service';
import { CanComponentDeactivate } from '../core/services/unsaved-guard.service';
import { Character, CharacterForm, CharacterVoice, CharacterVisual, CharacterGeneration, CharacterPublishing } from '../core/models';

@Component({
  selector: 'app-character-detail',
  standalone: true,
  imports: [TPipe, CommonModule, FormsModule, RouterModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="space-y-5" data-testid="character-detail-page">
      <div class="flex items-center gap-4">
        <a routerLink="/characters" class="text-emerald-600 hover:text-emerald-700 text-sm font-medium">{{ '&larr; Назад до персонажів' | t }}</a>
        <h2 class="text-xl font-semibold text-slate-900">{{ (name || 'Новий персонаж') | t }}</h2>
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
            <h3 class="text-sm font-medium text-slate-500 mb-3">{{ 'Основна інформація' | t }}</h3>
            <div class="grid grid-cols-1 md:grid-cols-2 gap-4 text-sm">
              <div>
                <label class="block text-slate-500 mb-1">ID <span class="text-red-400">*</span></label>
                <input [(ngModel)]="formId" [disabled]="isEdit" data-testid="character-id"
                  class="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm font-mono disabled:bg-slate-50 disabled:text-slate-500 focus:outline-none focus:ring-2 focus:ring-emerald-500"
                  placeholder="unicorns-narrator">
              </div>
              <div>
                <label class="block text-slate-500 mb-1">{{ 'Назва' | t }} <span class="text-red-400">*</span></label>
                <input [(ngModel)]="name" data-testid="character-name"
                  class="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500">
              </div>
              <div>
                <label class="block text-slate-500 mb-1">{{ 'Мова' | t }}</label>
                <select [(ngModel)]="language" data-testid="character-language"
                  class="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500">
                  <option value="uk">{{ 'Українська' | t }}</option><option value="en">English</option>
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
                <label for="char-enabled" class="text-sm text-slate-700">{{ 'Активний' | t }}</label>
              </div>
            </div>
          </div>
          <div class="bg-white rounded-xl border border-slate-200 p-5">
            <h3 class="text-sm font-medium text-slate-500 mb-3">{{ 'Системний промпт' | t }}</h3>
            <textarea [(ngModel)]="systemPrompt" rows="6" data-testid="character-system-prompt"
              class="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm font-mono focus:outline-none focus:ring-2 focus:ring-emerald-500"></textarea>
          </div>
          <div class="bg-white rounded-xl border border-slate-200 p-5">
            <h3 class="text-sm font-medium text-slate-500 mb-3">{{ 'Налаштування голосу' | t }}</h3>
            <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
              <div>
                <label class="block text-sm font-medium text-slate-700 mb-1">{{ 'Провайдер' | t }}</label>
                <select [(ngModel)]="voice.provider" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-sm">
                  <option value="none">{{ 'Немає (тимчасово)' | t }}</option>
                  <option value="mock">{{ 'Mock (тест)' | t }}</option>
                  <option value="piper">Piper (MIT)</option>
                  <option value="kokoro">Kokoro (Apache-2.0)</option>
                </select>
              </div>
              <div>
                <label class="block text-sm font-medium text-slate-700 mb-1">{{ 'Мова' | t }}</label>
                <select [(ngModel)]="voice.language" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-sm">
                  <option value="uk">{{ 'Українська' | t }}</option>
                  <option value="en">English</option>
                  <option value="pl">Polski</option>
                  <option value="de">Deutsch</option>
                </select>
              </div>
              <div class="md:col-span-2">
                <label class="block text-sm font-medium text-slate-700 mb-1">{{ 'Голос (voice ID)' | t }}</label>
                <input [(ngModel)]="voice.voice" placeholder="{{ 'наприклад, uk_Kyiv (або залиште порожнім для дефолту)' | t }}" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-sm">
              </div>
            </div>
          </div>
          <div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="character-references">
            <h3 class="text-sm font-medium text-slate-500 mb-1">{{ 'Референси персонажа' | t }}</h3>
            @if (isEdit) {
              <p class="text-sm text-slate-500 mb-3">{{ 'Зразки зовнішності. Вони автоматично використовуються в кожній генерації зображень цього персонажа. До 5 файлів PNG, JPEG або WebP, кожен до 10 МБ.' | t }}</p>
              <div class="flex flex-wrap gap-3 mb-3">
                @for (item of references(); track item.filename) {
                  <div class="relative w-24 h-24" data-testid="character-reference">
                    <img [src]="resources.characterReferenceUrl(formId, item.filename)" alt="Референс персонажа" class="w-24 h-24 object-cover rounded-lg border border-slate-200">
                    <button type="button" (click)="deleteReference(item.filename)" aria-label="{{ 'Видалити референс' | t }}" class="absolute top-1 right-1 w-6 h-6 rounded-full bg-white border border-slate-300 text-sm">×</button>
                  </div>
                }
              </div>
              <input type="file" accept="image/png,image/jpeg,image/webp" multiple data-testid="character-reference-input" (change)="uploadReferences($event)">
            } @else {
              <p class="text-sm text-slate-500">{{ 'Збережіть персонажа, після цього можна буде додати референси.' | t }}</p>
            }
          </div>
          <div class="bg-white rounded-xl border border-slate-200 p-5">
            <h3 class="text-sm font-medium text-slate-500 mb-3">{{ 'Візуальний стиль' | t }}</h3>
            <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
              <div>
                <label class="block text-sm font-medium text-slate-700 mb-1">{{ 'Стиль' | t }}</label>
                <input [(ngModel)]="visual.style" placeholder="{{ 'наприклад, warm documentary illustration' | t }}" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-sm">
              </div>
              <div>
                <label class="block text-sm font-medium text-slate-700 mb-1">{{ 'Співвідношення сторін' | t }}</label>
                <select [(ngModel)]="visual.aspect_ratio" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-sm">
                  <option value="16:9">{{ '16:9 (YouTube, горизонтальне)' | t }}</option>
                  <option value="9:16">{{ '9:16 (Shorts/Reels/TikTok, вертикальне)' | t }}</option>
                  <option value="1:1">{{ '1:1 (квадратне)' | t }}</option>
                  <option value="4:3">4:3</option>
                </select>
              </div>
              <div class="md:col-span-2">
                <label class="block text-sm font-medium text-slate-700 mb-1">{{ 'Пресет виводу' | t }}</label>
                <select [(ngModel)]="visual.output_preset" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-sm">
                  <option value="youtube">YouTube (1080p/720p)</option>
                  <option value="shorts">YouTube Shorts</option>
                  <option value="tiktok">TikTok</option>
                  <option value="reels">Instagram Reels</option>
                </select>
              </div>
            </div>
          </div>
          <div class="bg-white rounded-xl border border-slate-200 p-5">
            <h3 class="text-sm font-medium text-slate-500 mb-3">{{ 'Налаштування генерації' | t }}</h3>
            <div class="grid grid-cols-1 md:grid-cols-3 gap-3">
              <div class="md:col-span-2">
                <label class="block text-sm font-medium text-slate-700 mb-1">{{ 'Workflow (шлях до JSON)' | t }}</label>
                <input [(ngModel)]="generation.workflow" placeholder="workflows/image/demo.json" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-sm font-mono">
              </div>
              <div>
                <label class="block text-sm font-medium text-slate-700 mb-1">{{ 'Мін. VRAM (МБ)' | t }}</label>
                <input type="number" [(ngModel)]="generation.min_vram_mb" min="0" step="512" placeholder="4096" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-sm">
              </div>
              <div>
                <label class="block text-sm font-medium text-slate-700 mb-1">{{ 'Макс. спроб' | t }}</label>
                <input type="number" [(ngModel)]="generation.max_retries" min="0" max="10" placeholder="3" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-sm">
              </div>
            </div>
          </div>
          <div class="bg-white rounded-xl border border-slate-200 p-5">
            <h3 class="text-sm font-medium text-slate-500 mb-3">{{ 'Публікація' | t }}</h3>
            <div class="flex items-center gap-2 mb-3">
              <input type="checkbox" [(ngModel)]="publishing.enabled" id="pub-enabled">
              <label for="pub-enabled" class="text-sm text-slate-700">{{ 'Увімкнути автопублікацію' | t }}</label>
            </div>
            <div>
              <label class="block text-sm font-medium text-slate-700 mb-1">{{ 'Канали (через кому)' | t }}</label>
              <input [(ngModel)]="publishing.channels" placeholder="youtube,tiktok,facebook,instagram,threads" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-sm">
              <p class="text-xs text-slate-500 mt-1">{{ 'Доступні: youtube, tiktok, facebook, instagram, threads' | t }}</p>
            </div>
          </div>
          <div class="flex items-center justify-between pt-2">
            <div class="flex gap-2">
              <button (click)="save()" [disabled]="saving() || !formId || !name || !canSave" [title]="(saveReason || 'Зберегти') | t" data-testid="character-save-btn"
                class="px-4 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 disabled:opacity-50 text-sm font-medium">
                {{ (saving() ? 'Збереження...' : (isEdit ? 'Зберегти' : 'Створити')) | t }}
              </button>
              <a routerLink="/characters" class="px-4 py-2 text-sm text-slate-600 border border-slate-200 rounded-lg">{{ 'Скасувати' | t }}</a>
            </div>
            @if (isEdit) {
              <button (click)="deleteCharacter()" [disabled]="!canDelete" [title]="(deleteReason || 'Видалити') | t" data-testid="character-delete-btn"
                class="px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700 text-sm font-medium disabled:opacity-50">{{ 'Видалити' | t }}</button>
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
  voice: CharacterVoice = { provider: 'none', voice: '' };
  references = signal<CharacterReference[]>([]);
  visual: CharacterVisual = { style: '', aspect_ratio: '16:9', output_preset: 'youtube' };
  generation: CharacterGeneration = { workflow: 'workflows/image/demo.json', min_vram_mb: 4096, max_retries: 3 };
  publishing: CharacterPublishing = { enabled: false, channels: '' };
  voiceJson = '{}';
  visualJson = '{}';
  generationJson = '{}';
  publishingJson = '{}';
  private originalSnapshot = '';

  private emptyForm(): CharacterForm {
    return {
      name: 'Новий персонаж', language: 'uk', enabled: true, system_prompt: '',
      voice: { provider: 'none', voice: '' },
      visual: { style: '', aspect_ratio: '16:9', output_preset: 'youtube' },
      generation: { workflow: 'workflows/image/demo.json', min_vram_mb: 4096, max_retries: 3 },
      publishing: { enabled: false, channels: '' },
    };
  }

  constructor(
    private route: ActivatedRoute,
    private router: Router,
    readonly resources: ResourcesApiService,
    private toast: ToastService,
    private confirm: ConfirmService,
    public policy: PolicyService,
  ) {}

  get canSave(): boolean { return this.policy.can(this.isEdit ? 'update_character' : 'create_character').allowed; }
  get saveReason(): string | null { return this.policy.disabledReason(this.isEdit ? 'update_character' : 'create_character'); }
  get canDelete(): boolean { return this.policy.can('delete_character').allowed; }
  get deleteReason(): string | null { return this.policy.disabledReason('delete_character'); }

  ngOnInit(): void {
    const id = this.route.snapshot.paramMap.get('id');
    if (id) { this.isEdit = true; this.loadCharacter(id); } else { this.loading.set(false); }
  }

  loadReferences(id: string): void {
    this.resources.characterReferences(id).subscribe({ next: (items) => this.references.set(items), error: () => {} });
  }

  uploadReferences(event: Event): void {
    const input = event.target as HTMLInputElement;
    for (const file of Array.from(input.files ?? [])) {
      this.resources.uploadCharacterReference(this.formId, file).subscribe({
        next: () => this.loadReferences(this.formId),
        error: (err) => this.toast.show(err?.error?.detail || err.message || 'Не вдалося завантажити зображення', 'error'),
      });
    }
    input.value = '';
  }

  deleteReference(name: string): void {
    this.resources.deleteCharacterReference(this.formId, name).subscribe({
      next: () => this.loadReferences(this.formId),
      error: (err) => this.toast.show(err?.error?.detail || err.message || 'Не вдалося видалити зображення', 'error'),
    });
  }

  loadCharacter(id: string): void {
    this.loadReferences(id);
    this.loading.set(true);
    this.error.set(null);
    this.resources.character(id).subscribe({
      next: (c) => {
        this.formId = c.id ?? ''; this.name = c.name; this.language = c.language || 'uk';
        this.enabled = c.enabled !== false; this.workflow = c.workflow || '';
        this.systemPrompt = c.system_prompt || '';
        this.voice = { provider: 'none', voice: '', ...(c.voice || {}) };
        this.visual = { style: '', aspect_ratio: '16:9', output_preset: 'youtube', ...(c.visual || {}) };
        this.generation = { workflow: 'workflows/image/demo.json', min_vram_mb: 4096, max_retries: 3, ...(c.generation || {}) };
        this.publishing = { enabled: false, channels: '', ...(c.publishing || {}) };
        this.voiceJson = this.stringify(c.voice); this.visualJson = this.stringify(c.visual);
        this.generationJson = this.stringify(c.generation); this.publishingJson = this.stringify(c.publishing);
        this.originalSnapshot = this.snapshot(); this.loading.set(false);
      },
      error: (err) => { this.error.set(err.message || 'Не вдалося завантажити персонажа'); this.loading.set(false); },
    });
  }

  save(): void {
    if (!this.canSave) return;
    if (!this.formId || !this.name) { this.toast.show('ID та назва обов\'язкові', 'error'); return; }
    this.saving.set(true);
    const payload: Character = {
      name: this.name, language: this.language, enabled: this.enabled,
      workflow: this.workflow || undefined, system_prompt: this.systemPrompt,
      voice: this.voice, visual: this.visual,
      generation: this.generation, publishing: this.publishing,
    };
    const req = this.isEdit
      ? this.resources.updateCharacter(this.formId, payload)
      : this.resources.createCharacter(payload);
    req.subscribe({
      next: () => {
        this.saving.set(false);
        this.voiceJson = this.stringify(this.voice); this.visualJson = this.stringify(this.visual);
        this.generationJson = this.stringify(this.generation); this.publishingJson = this.stringify(this.publishing);
        this.toast.show(this.isEdit ? 'Персонаж збережено' : 'Персонаж створено', 'success');
        if (!this.isEdit) { this.router.navigate(['/characters', this.formId]); } else { this.originalSnapshot = this.snapshot(); }
      },
      error: (err) => { this.saving.set(false); this.toast.show(err.message || 'Помилка збереження', 'error'); },
    });
  }

  deleteCharacter(): void {
    if (!this.canDelete) return;
    this.confirm.confirm({ title: 'Видалити персонажа', message: `Видалити ${this.formId}?` }).subscribe((ok) => {
      if (!ok) return;
      this.resources.deleteCharacter(this.formId).subscribe({
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
