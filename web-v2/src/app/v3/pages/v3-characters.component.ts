import { ChangeDetectionStrategy, Component, OnInit, computed, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterModule } from '@angular/router';
import { catchError } from 'rxjs/operators';
import { TPipe } from '../../core/i18n/t.pipe';
import { ModuleCharacter, ResourcesApiService } from '../../core/api/resources.api';
import { ToastService } from '../../core/services/toast.service';
import { ConfirmService } from '../../core/services/confirm.service';
import { PolicyService } from '../../core/services/policy.service';
import { Character } from '../../core/models';
import { LoadingStateComponent } from '../../shared/loading-state.component';
import { ErrorStateComponent } from '../../shared/error-state.component';
import { PaperCardComponent } from '../ui/paper-card.component';

type CharacterCardItem = Character & { references?: { filename: string }[] };

@Component({
  selector: 'app-v3-characters',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterModule, TPipe, LoadingStateComponent, ErrorStateComponent, PaperCardComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="space-y-5" data-testid="v3-characters-page">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p class="text-xs font-semibold uppercase tracking-wider" style="color: var(--v3-text-subtle)">{{ 'Студія' | t }}</p>
          <h2 class="text-xl font-semibold" style="color: var(--v3-text)">{{ 'Персонажі' | t }}</h2>
        </div>
        <div class="flex items-center gap-2">
          <a routerLink="/v3/characters/new"
             [class.pointer-events-none]="!canCreateCharacter"
             [class.opacity-50]="!canCreateCharacter"
             data-testid="v3-create-character-button"
             class="v3-btn-primary">
            + {{ 'Новий персонаж' | t }}
          </a>
        </div>
      </div>

      <!-- Search + Brand filter bar -->
      <app-v3-paper-card>
        <div class="flex flex-col md:flex-row gap-3 pt-1">
          <input [ngModel]="search()" (ngModelChange)="search.set($event)"
                 data-testid="v3-characters-search"
                 placeholder="{{ 'Пошук за ім\\'ям або ID...' | t }}"
                 class="v3-input flex-1">
          @if (brandFilters().length > 1) {
            <div class="flex flex-wrap items-center gap-1.5">
              <button type="button" (click)="selectedBrand.set('')"
                      class="px-3 py-1.5 rounded-full text-xs font-medium border transition-colors"
                      [style.background-color]="selectedBrand() === '' ? 'var(--v3-accent-soft)' : '#ffffff'"
                      [style.color]="selectedBrand() === '' ? 'var(--v3-accent)' : 'var(--v3-text-muted)'"
                      [style.border-color]="selectedBrand() === '' ? 'transparent' : 'var(--v3-border-strong)'">
                {{ 'Усі бренди' | t }}
              </button>
              @for (b of brandFilters(); track b) {
                <button type="button" (click)="selectedBrand.set(b)"
                        class="px-3 py-1.5 rounded-full text-xs font-medium border transition-colors"
                        [style.background-color]="selectedBrand() === b ? 'var(--v3-coral-soft)' : '#ffffff'"
                        [style.color]="selectedBrand() === b ? '#b8512b' : 'var(--v3-text-muted)'"
                        [style.border-color]="selectedBrand() === b ? 'transparent' : 'var(--v3-border-strong)'">
                  {{ b }}
                </button>
              }
            </div>
          }
        </div>
      </app-v3-paper-card>

      @if (loading()) {
        <app-loading-state />
      } @else if (error()) {
        <app-error-state [message]="error()!" (retry)="loadCharacters()" />
      } @else if (filtered().length === 0) {
        <app-v3-paper-card testId="v3-characters-empty">
          <div class="text-center py-8 text-sm" style="color: var(--v3-text-muted)">{{ 'Персонажів не знайдено' | t }}</div>
        </app-v3-paper-card>
      } @else {
        <div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-5">
          @for (character of filtered(); track character.id) {
            <article class="v3-paper-card overflow-hidden flex flex-col" [attr.data-testid]="'v3-character-card-' + character.id">
              <div class="p-5 flex items-start gap-4 flex-1">
                <div class="w-14 h-14 rounded-2xl overflow-hidden flex items-center justify-center text-base font-bold flex-shrink-0 border"
                     style="background-color: var(--v3-coral-soft); border-color: var(--v3-border); color: #b8512b">
                  @if (avatarFor(character) && !failedAvatars.has(character.id || '')) {
                    <img [src]="avatarFor(character)!" [alt]="character.name" (error)="markAvatarFailed(character.id || '')"
                         class="w-full h-full object-cover">
                  } @else {
                    <span>{{ initialsFor(character) }}</span>
                  }
                </div>

                <div class="min-w-0 flex-1">
                  <div class="flex items-start justify-between gap-2">
                    <a [routerLink]="['/v3/characters', character.id]" class="text-base font-semibold truncate hover:underline" style="color: var(--v3-text)">
                      {{ character.name }}
                    </a>
                    <span class="px-2 py-0.5 rounded-full text-[11px] font-medium flex-shrink-0"
                          [style.background-color]="isActive(character) ? 'var(--v3-accent-soft)' : '#efede7'"
                          [style.color]="isActive(character) ? 'var(--v3-accent)' : 'var(--v3-text-muted)'">
                      {{ (isActive(character) ? 'Активний' : 'Неактивний') | t }}
                    </span>
                  </div>
                  <p class="text-xs font-mono truncate mt-0.5" style="color: var(--v3-text-subtle)">{{ character.id }}</p>

                  <div class="flex flex-wrap items-center gap-1.5 mt-3">
                    <span class="px-2 py-0.5 rounded text-[11px] font-medium" style="background-color: var(--v3-sky-soft); color: #236e73">
                      {{ (character.language || 'uk').toUpperCase() }}
                    </span>
                    @if (character.owner_brand_id) {
                      <span class="px-2 py-0.5 rounded text-[11px] font-medium" style="background-color: var(--v3-coral-soft); color: #b8512b">
                        {{ character.owner_brand_id }}
                      </span>
                    }
                    @if (character.voice.provider && character.voice.provider !== 'none') {
                      <span class="px-2 py-0.5 rounded text-[11px]" style="background-color: #efede7; color: var(--v3-text-muted)">
                        {{ character.voice.provider }}
                      </span>
                    }
                  </div>
                </div>
              </div>

              <div class="px-5 py-3 border-t flex items-center justify-between gap-2"
                   style="border-color: var(--v3-border); background-color: var(--v3-surface-subtle)">
                <a [routerLink]="['/v3/characters', character.id]"
                   class="text-xs font-semibold hover:underline"
                   style="color: var(--v3-accent)"
                   [attr.data-testid]="'v3-open-character-' + character.id">
                  {{ 'Відкрити профіль' | t }} →
                </a>
                <button type="button" (click)="deleteCharacter(character)"
                        [disabled]="!canDeleteCharacter"
                        class="text-xs font-medium disabled:opacity-40 hover:underline"
                        style="color: var(--v3-danger)">
                  {{ 'Видалити' | t }}
                </button>
              </div>
            </article>
          }
        </div>
      }
    </div>
  `,
})
export class V3CharactersComponent implements OnInit {
  characters = signal<CharacterCardItem[]>([]);
  loading = signal(true);
  error = signal<string | null>(null);
  search = signal('');
  selectedBrand = signal('');
  useModuleApi = signal(false);
  failedAvatars = new Set<string>();

  brandFilters = computed(() => {
    const ids = new Set<string>();
    this.characters().forEach(c => { if (c.owner_brand_id) ids.add(c.owner_brand_id); });
    return Array.from(ids).sort();
  });

  filtered = computed(() => {
    const q = this.search().trim().toLowerCase();
    const brand = this.selectedBrand();
    return this.characters().filter(c => {
      if (brand && c.owner_brand_id !== brand) return false;
      if (!q) return true;
      return (c.name || '').toLowerCase().includes(q) || (c.id || '').toLowerCase().includes(q);
    });
  });

  constructor(
    private resources: ResourcesApiService,
    private toast: ToastService,
    private confirm: ConfirmService,
    public policy: PolicyService,
  ) {}

  get canCreateCharacter(): boolean { return this.policy.can('create_character').allowed; }
  get canDeleteCharacter(): boolean { return this.policy.can('delete_character').allowed; }

  ngOnInit(): void {
    this.loadCharacters();
  }

  loadCharacters(): void {
    this.loading.set(true);
    this.error.set(null);
    this.resources.moduleCharacters().pipe(
      catchError(() => {
        this.useModuleApi.set(false);
        return this.resources.characters();
      }),
    ).subscribe({
      next: items => {
        const list = Array.isArray(items) ? items : [];
        if (list.length && ('is_owner' in list[0] || 'references' in list[0])) {
          this.useModuleApi.set(true);
        }
        this.characters.set(list as (ModuleCharacter | Character)[]);
        this.loading.set(false);
      },
      error: err => {
        this.error.set(err?.message || 'Не вдалося завантажити персонажів');
        this.loading.set(false);
      },
    });
  }

  isActive(c: CharacterCardItem): boolean {
    return c.active !== undefined ? c.active : c.enabled !== false;
  }

  avatarFor(c: CharacterCardItem): string | null {
    const first = c.references?.[0]?.filename;
    if (!first || !c.id) return null;
    return this.useModuleApi()
      ? this.resources.moduleCharacterReferenceUrl(c.id, first)
      : this.resources.characterReferenceUrl(c.id, first);
  }

  markAvatarFailed(id: string): void { this.failedAvatars.add(id); }

  initialsFor(c: CharacterCardItem): string {
    const parts = (c.name || c.id || 'C').trim().split(/\s+/);
    if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
    return (parts[0]?.slice(0, 2) || 'C').toUpperCase();
  }

  deleteCharacter(c: CharacterCardItem): void {
    if (!this.canDeleteCharacter || !c.id) return;
    this.confirm.confirm({ title: 'Видалити персонажа', message: `Видалити ${c.name || c.id}?` }).subscribe(ok => {
      if (!ok) return;
      const req = this.useModuleApi()
        ? this.resources.moduleDeleteCharacter(c.id!, c.owner_brand_id || undefined)
        : this.resources.deleteCharacter(c.id!);
      req.subscribe({
        next: () => { this.toast.show('Персонаж видалено', 'success'); this.loadCharacters(); },
        error: err => this.toast.show(err?.message || 'Помилка видалення', 'error'),
      });
    });
  }
}
