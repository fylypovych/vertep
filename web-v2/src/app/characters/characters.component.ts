import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { VertepApiService } from '../core/api.service';
import { ToastService } from '../core/services/toast.service';
import { ConfirmService } from '../core/services/confirm.service';
import { Character } from '../core/models';

@Component({
  selector: 'app-characters',
  standalone: true,
  imports: [CommonModule, FormsModule],
  template: `
    <div class="bg-white rounded-xl border border-slate-200 p-5">
      <div class="flex items-center justify-between mb-4">
        <h3 class="text-lg font-semibold text-slate-900">Персонажі</h3>
        <button (click)="openCreateModal()" class="px-4 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 text-sm font-medium">
          Новий персонаж
        </button>
      </div>

      <div class="mb-4">
        <input [(ngModel)]="search" placeholder="Пошук за ім'ям або ID..." class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-sm">
      </div>

      @if (loading) {
        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          @for (_ of [1,2,3,4,5,6]; track $index) {
            <div class="animate-pulse bg-slate-100 rounded-lg h-32"></div>
          }
        </div>
      } @else if (error) {
        <div class="bg-red-50 border border-red-200 rounded-xl p-5">
          <p class="text-red-700">{{ error }}</p>
          <button (click)="loadCharacters()" class="mt-2 text-sm text-red-600 hover:text-red-700 font-medium">Повторити</button>
        </div>
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
                <button (click)="editCharacter(character)" class="text-sm text-emerald-600 hover:text-emerald-700 font-medium">Редагувати</button>
                <button (click)="deleteCharacter(character.id)" class="text-sm text-red-600 hover:text-red-700 font-medium">Видалити</button>
              </div>
            </div>
          } @empty {
            <div class="col-span-full text-center text-slate-500 py-6">Персонажів не знайдено</div>
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
    <div *ngIf="showModal" class="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div class="bg-white rounded-xl p-6 w-full max-w-lg mx-4 max-h-[90vh] overflow-y-auto">
        <h3 class="text-lg font-semibold text-slate-900 mb-4">{{ editingCharacter ? 'Редагувати персонажа' : 'Новий персонаж' }}</h3>
        <div class="space-y-4">
          <div>
            <label class="block text-sm font-medium text-slate-700 mb-1">Ім'я персонажа</label>
            <input [(ngModel)]="form.name" placeholder="Наприклад, Дід Самогонщик" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500">
          </div>
          <div>
            <label class="block text-sm font-medium text-slate-700 mb-1">Системний ідентифікатор</label>
            <input [(ngModel)]="form.id" placeholder="did_samogon" [disabled]="editingCharacter !== null" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 disabled:bg-slate-100">
          </div>
          <div>
            <label class="block text-sm font-medium text-slate-700 mb-1">Мова</label>
            <select [(ngModel)]="form.language" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500">
              <option value="uk">Українська</option>
              <option value="en">Англійська</option>
              <option value="pl">Польська</option>
              <option value="de">Німецька</option>
            </select>
          </div>
          <div>
            <label class="block text-sm font-medium text-slate-700 mb-1">Опис характеру</label>
            <textarea [(ngModel)]="form.system_prompt" placeholder="Опишіть стиль мовлення, характер, знання та обмеження персонажа." rows="4" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500"></textarea>
          </div>
          <div class="flex items-center gap-2">
            <input type="checkbox" [(ngModel)]="form.enabled" id="enabled">
            <label for="enabled" class="text-sm text-slate-700">Персонаж активний</label>
          </div>
        </div>
        <div class="flex justify-end gap-2 mt-6">
          <button (click)="closeModal()" class="px-4 py-2 text-slate-600 hover:text-slate-800 text-sm font-medium">Скасувати</button>
          <button (click)="saveCharacter()" [disabled]="saving" class="px-4 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 text-sm font-medium disabled:opacity-50">{{ saving ? 'Збереження...' : 'Зберегти' }}</button>
        </div>
      </div>
    </div>
  `,
})
export class CharactersComponent implements OnInit {
  characters: Character[] = [];
  loading = false;
  error: string | null = null;
  showModal = false;
  editingCharacter: Character | null = null;
  form: Partial<Character> = {};
  saving = false;
  search = '';
  page = 1;
  pageSize = 10;

  constructor(private api: VertepApiService, private toast: ToastService, private confirm: ConfirmService) {}

  ngOnInit(): void {
    this.loadCharacters();
  }

  get filteredCharacters(): Character[] {
    if (!this.search.trim()) return this.characters;
    const term = this.search.toLowerCase();
    return this.characters.filter(c =>
      c.name.toLowerCase().includes(term) ||
      c.id.toLowerCase().includes(term)
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
    this.loading = true;
    this.error = null;
    this.api.getCharacters().subscribe({
      next: (characters) => { this.characters = characters; this.loading = false; },
      error: (err) => { this.error = err.message; this.loading = false; },
    });
  }

  openCreateModal(): void {
    this.editingCharacter = null;
    this.form = {
      id: '',
      name: 'Новий персонаж',
      language: 'uk',
      enabled: true,
      system_prompt: '',
      voice: { provider: 'none', voice: '' },
      visual: { style: '', aspect_ratio: '16:9' },
      generation: { workflow: 'workflows/image/demo.json', min_vram_mb: 4096, max_retries: 3 },
      publishing: { enabled: false },
    };
    this.showModal = true;
  }

  editCharacter(character: Character): void {
    this.editingCharacter = character;
    this.form = { ...character };
    this.showModal = true;
  }

  closeModal(): void {
    this.showModal = false;
    this.editingCharacter = null;
    this.form = {};
    this.saving = false;
  }

  saveCharacter(): void {
    if (!this.form.id || !this.form.name) return;
    this.saving = true;
    const payload = {
      ...this.form,
      id: this.form.id,
      name: this.form.name,
      language: this.form.language || 'uk',
      enabled: this.form.enabled !== false,
      system_prompt: this.form.system_prompt || '',
      voice: this.form.voice || { provider: 'none', voice: '' },
      visual: this.form.visual || { style: '', aspect_ratio: '16:9' },
      generation: this.form.generation || { workflow: 'workflows/image/demo.json', min_vram_mb: 4096, max_retries: 3 },
      publishing: this.form.publishing || { enabled: false },
    };

    const request = this.editingCharacter
      ? this.api.updateCharacter(this.form.id, payload)
      : this.api.createCharacter(payload);

    request.subscribe({
      next: () => {
        this.closeModal();
        this.loadCharacters();
        this.toast.show('Персонаж збережено', 'success');
      },
      error: (err) => {
        this.error = err.message;
        this.saving = false;
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

  prevPage(): void {
    if (this.page > 1) this.page--;
  }

  nextPage(): void {
    if (this.page < this.pages) this.page++;
  }
}
