import { Component, OnInit, OnDestroy, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterModule } from '@angular/router';
import { VertepApiService } from '../core/api.service';
import { ToastService } from '../core/services/toast.service';
import { ConfirmService } from '../core/services/confirm.service';
import { Character } from '../core/models';
import { LoadingStateComponent } from '../shared/loading-state.component';
import { ErrorStateComponent } from '../shared/error-state.component';
import { EmptyStateComponent } from '../shared/empty-state.component';

@Component({
  selector: 'app-characters',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterModule, LoadingStateComponent, ErrorStateComponent, EmptyStateComponent],
  template: `
    <div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="characters-page">
      <div class="flex items-center justify-between mb-4">
        <h3 class="text-lg font-semibold text-slate-900">Персонажі</h3>
        <button (click)="openCreateModal()" data-testid="create-character-button" class="px-4 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 text-sm font-medium">
          Новий персонаж
        </button>
      </div>

      <div class="mb-4">
        <input [(ngModel)]="search" (input)="onSearch()" data-testid="characters-search" placeholder="Пошук за ім'ям або ID..." class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-sm">
      </div>

      @if (loading()) {
        <app-loading-state />
      } @else if (error()) {
        <app-error-state [message]="error()!" (retry)="loadCharacters()" />
      } @else {
        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          @for (character of pagedCharacters; track character.id) {
            <div class="border border-slate-200 rounded-lg p-4 hover:shadow-md transition-shadow">
              <div class="flex items-center justify-between mb-2">
                <h4 class="font-semibold text-slate-900">{{ character.name }}</h4>
                <span class="text-xs text-slate-500">{{ character.language }}</span>
              </div>
              <p class="text-sm text-slate-600 mb-3">{{ character.id }}</p>
              <div class="flex gap-2">
                <button (click)="editCharacter(character)" class="text-sm text-blue-600 hover:text-blue-700 font-medium" data-testid="edit-character-button">Редагувати</button>
                <button (click)="deleteCharacter(character.id!)" class="text-sm text-red-600 hover:text-red-700 font-medium">Видалити</button>
              </div>
            </div>
          } @empty {
            <div class="col-span-full text-center text-slate-500 py-6" data-testid="characters-empty">Персонажів не знайдено</div>
          }
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

    <!-- Character Form Modal -->
    <div *ngIf="showModal()" data-testid="character-modal" class="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div class="bg-white rounded-xl p-6 w-full max-w-2xl mx-4 max-h-[90vh] overflow-y-auto">
        <h3 class="text-lg font-semibold text-slate-900 mb-4">{{ editingId ? 'Редагувати персонажа' : 'Новий персонаж' }}</h3>
        <div class="space-y-4 max-h-[70vh] overflow-y-auto">
          <!-- Base metadata -->
          <div class="border border-slate-200 rounded-lg p-3">
            <h4 class="text-xs font-medium text-slate-500 mb-2">Основна інформація</h4>
            <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
              <div>
                <label class="block text-sm font-medium text-slate-700 mb-1">Назва</label>
                <input [(ngModel)]="form.name" data-testid="character-name-input" placeholder="Наприклад, Дід Самогонщик" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-sm">
              </div>
              <div>
                <label class="block text-sm font-medium text-slate-700 mb-1">Системний ID</label>
                <input [(ngModel)]="form.id" data-testid="character-id-input" [disabled]="!editingId" placeholder="did_samogon" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-sm disabled:bg-slate-100">
              </div>
              <div>
                <label class="block text-sm font-medium text-slate-700 mb-1">Мова</label>
                <select [(ngModel)]="form.language" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-sm">
                  <option value="uk">Українська</option>
                  <option value="en">Англійська</option>
                  <option value="pl">Польська</option>
                  <option value="de">Німецька</option>
                </select>
              </div>
              <div class="flex items-end">
                <div class="flex items-center gap-2">
                  <input type="checkbox" [(ngModel)]="form.enabled" id="enabled">
                  <label for="enabled" class="text-sm text-slate-700">Персонаж активний</label>
                </div>
              </div>
              <div class="md:col-span-2">
                <label class="block text-sm font-medium text-slate-700 mb-1">Workflow</label>
                <input [(ngModel)]="form.workflow" placeholder="workflows/image/demo.json" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-sm">
              </div>
            </div>
          </div>

          <!-- System prompt -->
          <div class="border border-slate-200 rounded-lg p-3">
            <h4 class="text-xs font-medium text-slate-500 mb-2">Системний промпт</h4>
            <textarea [(ngModel)]="form.system_prompt" placeholder="Опишіть стиль мовлення, характер, знання та обмеження персонажа." rows="6" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-sm resize-y"></textarea>
          </div>

          <!-- Voice config -->
          <div class="border border-slate-200 rounded-lg p-3">
            <h4 class="text-xs font-medium text-slate-500 mb-2">Голос (JSON)</h4>
            <textarea [(ngModel)]="voiceJson" rows="4" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-xs font-mono resize-y" spellcheck="false">{{ voiceJson }}</textarea>
          </div>

          <!-- Visual config -->
          <div class="border border-slate-200 rounded-lg p-3">
            <h4 class="text-xs font-medium text-slate-500 mb-2">Візуал (JSON)</h4>
            <textarea [(ngModel)]="visualJson" rows="4" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-xs font-mono resize-y" spellcheck="false">{{ visualJson }}</textarea>
          </div>

          <!-- Generation config -->
          <div class="border border-slate-200 rounded-lg p-3">
            <h4 class="text-xs font-medium text-slate-500 mb-2">Генерація (JSON)</h4>
            <textarea [(ngModel)]="generationJson" rows="4" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-xs font-mono resize-y" spellcheck="false">{{ generationJson }}</textarea>
            <p class="text-xs text-slate-500 mt-1">Приклад: workflow, min_vram_mb, max_retries</p>
          </div>

          <!-- Publishing config -->
          <div class="border border-slate-200 rounded-lg p-3">
            <h4 class="text-xs font-medium text-slate-500 mb-2">Публікація (JSON)</h4>
            <textarea [(ngModel)]="publishingJson" rows="4" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-xs font-mono resize-y" spellcheck="false">{{ publishingJson }}</textarea>
          </div>
        </div>
        <div class="flex justify-end gap-2 mt-6">
          <button (click)="closeModal()" class="px-4 py-2 text-slate-600 hover:text-slate-800 text-sm font-medium">Скасувати</button>
          <button (click)="saveCharacter()" [disabled]="saving()" class="px-4 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 text-sm font-medium disabled:opacity-50">{{ saving() ? 'Збереження...' : 'Зберегти' }}</button>
        </div>
      </div>
    </div>
  `,
})
export class CharactersComponent implements OnInit {
  characters: Character[] = [];
  loading = signal(false);
  error = signal<string | null>(null);
  showModal = signal(false);
  editingId: string | null = null;
  form: Partial<Character> = {};
  private originalForm: Partial<Character> | null = null;
  private originalVoiceJson = '';
  private originalVisualJson = '';
  private originalGenerationJson = '';
  private originalPublishingJson = '';
  saving = signal(false);
  search = '';
  page = 1;
  pageSize = 10;
  voiceJson = '';
  visualJson = '';
  generationJson = '';
  publishingJson = '';

  constructor(private api: VertepApiService, private toast: ToastService, private confirm: ConfirmService) {}

  ngOnInit(): void {
    this.loadCharacters();
  }

  get filteredCharacters(): Character[] {
    if (!this.search.trim()) return this.characters;
    const term = this.search.toLowerCase();
    return this.characters.filter(c =>
      (c.name || '').toLowerCase().includes(term) ||
      (c.id || '').toLowerCase().includes(term)
    );
  }

  get pagedCharacters(): Character[] {
    const start = (this.page - 1) * this.pageSize;
    return this.filteredCharacters.slice(start, start + this.pageSize);
  }

  get pages(): number {
    return Math.max(1, Math.ceil(this.filteredCharacters.length / this.pageSize));
  }

  loadCharacters(): void {
    this.loading.set(true);
    this.error.set(null);
    this.api.getCharacters().subscribe({
      next: (characters) => {
        this.characters = characters.map(c => ({
          ...c,
          voice: typeof c.voice === 'object' ? c.voice : {},
          visual: typeof c.visual === 'object' ? c.visual : {},
          generation: typeof c.generation === 'object' ? c.generation : {},
          publishing: typeof c.publishing === 'object' ? c.publishing : {},
        }));
        this.loading.set(false);
      },
      error: (err) => { this.error.set(err.message); this.loading.set(false); },
    });
  }

  openCreateModal(): void {
    this.editingId = null;
    this.form = {
      name: 'Новий персонаж',
      language: 'uk',
      enabled: true,
      system_prompt: '',
      workflow: undefined,
      voice: { provider: 'none', voice: '' },
      visual: { style: '', aspect_ratio: '16:9' },
      generation: { workflow: 'workflows/image/demo.json', min_vram_mb: 4096, max_retries: 3 },
      publishing: { enabled: false },
    };
    this.syncJsonFields();
    this.showModal.set(true);
  }

  private generateId(): string {
    return `character_${crypto.randomUUID().replace(/-/g, '').slice(0, 12)}`;
  }

  editCharacter(character: Character): void {
    this.editingId = character.id ?? null;
    this.form = { ...character };
    this.originalForm = { ...character };
    this.syncJsonFields();
    this.originalVoiceJson = this.voiceJson;
    this.originalVisualJson = this.visualJson;
    this.originalGenerationJson = this.generationJson;
    this.originalPublishingJson = this.publishingJson;
    this.showModal.set(true);
  }

  closeModal(): void {
    if (this.editingId && this.hasUnsavedChanges()) {
      if (!confirm('Є незбережені зміни. Закрити без збереження?')) return;
    }
    this.showModal.set(false);
    this.editingId = null;
    this.form = {};
    this.saving.set(false);
  }

  private hasUnsavedChanges(): boolean {
    if (!this.editingId || !this.originalForm) return false;
    return this.form.system_prompt !== this.originalForm.system_prompt
      || this.voiceJson !== this.originalVoiceJson
      || this.visualJson !== this.originalVisualJson
      || this.generationJson !== this.originalGenerationJson
      || this.publishingJson !== this.originalPublishingJson;
  }

  private syncJsonFields(): void {
    this.voiceJson = JSON.stringify(this.form.voice || {}, null, 2);
    this.visualJson = JSON.stringify(this.form.visual || {}, null, 2);
    this.generationJson = JSON.stringify(this.form.generation || {}, null, 2);
    this.publishingJson = JSON.stringify(this.form.publishing || {}, null, 2);
  }

  private parseJsonOrEmpty(text: string): Record<string, unknown> {
    if (!text.trim()) return {};
    try {
      const parsed = JSON.parse(text);
      return typeof parsed === 'object' && parsed !== null ? parsed : {};
    } catch {
      return {};
    }
  }

  saveCharacter(): void {
    if (!this.form.name) {
      this.toast.show('Назва є обов\'язковою', 'error');
      return;
    }
    this.saving.set(true);
    const payload: Character = {
      name: this.form.name,
      language: this.form.language || 'uk',
      enabled: this.form.enabled !== false,
      workflow: this.form.workflow || undefined,
      system_prompt: this.form.system_prompt || '',
      voice: this.parseJsonOrEmpty(this.voiceJson),
      visual: this.parseJsonOrEmpty(this.visualJson),
      generation: this.parseJsonOrEmpty(this.generationJson),
      publishing: this.parseJsonOrEmpty(this.publishingJson),
    };

    const request = this.editingId
      ? this.api.updateCharacter(this.editingId, payload)
      : this.api.createCharacter(payload);

    request.subscribe({
      next: () => {
        this.closeModal();
        this.loadCharacters();
        this.toast.show('Персонаж збережено', 'success');
      },
      error: (err) => {
        this.error.set(err.message);
        this.saving.set(false);
        this.toast.show(err.message || 'Помилка збереження', 'error');
      },
    });
  }

  deleteCharacter(id: string): void {
    this.confirm.confirm({ title: 'Видалити персонажа', message: `Ви впевнені, що хочете видалити ${id}?` }).subscribe((ok) => {
      if (!ok) return;
      this.api.deleteCharacter(id).subscribe({
        next: () => {
          this.loadCharacters();
          this.toast.show('Персонаж видалено', 'success');
        },
        error: (err) => this.toast.show(err.message || 'Помилка видалення', 'error'),
      });
    });
  }

  onSearch(): void {
    this.page = 1;
  }

  prevPage(): void {
    if (this.page > 1) this.page--;
  }

  nextPage(): void {
    if (this.page < this.pages) this.page++;
  }
}
