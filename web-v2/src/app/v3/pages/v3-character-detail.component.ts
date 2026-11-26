import { ChangeDetectionStrategy, Component, OnInit, computed, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, Router, RouterModule } from '@angular/router';
import { catchError } from 'rxjs/operators';
import { TPipe } from '../../core/i18n/t.pipe';
import { CharacterReference, ResourcesApiService } from '../../core/api/resources.api';
import { ToastService } from '../../core/services/toast.service';
import { ConfirmService } from '../../core/services/confirm.service';
import { PolicyService } from '../../core/services/policy.service';
import { CanComponentDeactivate } from '../../core/services/unsaved-guard.service';
import {
  Character,
  CharacterGeneration,
  CharacterPublishing,
  CharacterVisual,
  CharacterVoice,
} from '../../core/models';
import { EntityProfileLayoutComponent, ProfileBadge, ProfileStat } from '../ui/entity-profile-layout.component';
import { PaperCardComponent } from '../ui/paper-card.component';
import { LoadingStateComponent } from '../../shared/loading-state.component';
import { ErrorStateComponent } from '../../shared/error-state.component';

@Component({
  selector: 'app-v3-character-detail',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    RouterModule,
    TPipe,
    EntityProfileLayoutComponent,
    PaperCardComponent,
    LoadingStateComponent,
    ErrorStateComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="space-y-5" data-testid="v3-character-detail-page">
      <div class="flex items-center justify-between gap-3">
        <a routerLink="/v3/characters" class="inline-flex items-center gap-1.5 text-sm font-medium hover:underline" style="color: var(--v3-accent)">
          ← {{ 'Назад до персонажів' | t }}
        </a>
        @if (isEdit()) {
          <button type="button" (click)="deleteCharacter()"
                  [disabled]="!canDelete" [title]="(deleteReason || 'Видалити') | t"
                  class="v3-btn-danger" data-testid="v3-character-delete-btn">
            {{ 'Видалити' | t }}
          </button>
        }
      </div>

      @if (loading()) {
        <app-loading-state />
      } @else if (error()) {
        <app-error-state [message]="error()!" (retry)="reload()" />
      } @else {
        <app-v3-entity-profile
          [displayName]="name || 'Новий персонаж'"
          [subtitle]="formId ? '@' + formId : 'Новий запис'"
          [avatarUrl]="primaryAvatarUrl()"
          [statusLabel]="enabled ? 'Активний' : 'Неактивний'"
          [statusTone]="enabled ? 'ok' : 'muted'"
          accent="coral"
          [badges]="badges()"
          [stats]="stats()"
          testId="v3-character-profile">

          <!-- Left extra card: Quick visual references preview -->
          <app-v3-paper-card profile-left-extra title="Референси персонажа" eyebrow="Зовнішність" testId="v3-character-refs-summary">
            @if (isEdit()) {
              @if (references().length > 0) {
                <div class="grid grid-cols-3 gap-2 pt-1 mb-3">
                  @for (ref of references(); track ref.filename) {
                    <div class="relative aspect-square rounded-lg overflow-hidden border" style="border-color: var(--v3-border)" data-testid="v3-character-ref-thumb">
                      <img [src]="referenceUrl(ref.filename)" [alt]="ref.filename" class="w-full h-full object-cover">
                      <button type="button" (click)="deleteReference(ref.filename)"
                              [disabled]="!canSave"
                              aria-label="{{ 'Видалити референс' | t }}"
                              class="absolute top-1 right-1 w-5 h-5 rounded-full bg-white/95 border text-xs leading-none flex items-center justify-center"
                              style="border-color: var(--v3-border-strong); color: var(--v3-text)">×</button>
                    </div>
                  }
                </div>
              } @else {
                <p class="text-xs py-2" style="color: var(--v3-text-subtle)">{{ 'Референси ще не завантажені' | t }}</p>
              }
              <input type="file" accept="image/png,image/jpeg,image/webp" multiple
                     [disabled]="!canSave || saving()"
                     (change)="uploadReferences($event)"
                     data-testid="v3-character-reference-input"
                     class="text-xs w-full">
            } @else {
              <p class="text-xs py-2" style="color: var(--v3-text-subtle)">
                {{ 'Збережіть персонажа, після цього можна буде додати референси.' | t }}
              </p>
            }
          </app-v3-paper-card>

          <!-- Right column: Structured configuration sections -->
          <app-v3-paper-card title="Основна інформація" eyebrow="Профіль персонажа" [headerDivider]="true">
            <div class="grid grid-cols-1 md:grid-cols-2 gap-4 pt-3">
              <div>
                <label class="block text-xs font-semibold uppercase tracking-wide mb-1.5" style="color: var(--v3-text-muted)">
                  ID <span style="color: var(--v3-danger)">*</span>
                </label>
                <input [(ngModel)]="formId" [disabled]="isEdit()" data-testid="v3-character-id"
                       class="v3-input font-mono" placeholder="unicorns-narrator">
              </div>
              <div>
                <label class="block text-xs font-semibold uppercase tracking-wide mb-1.5" style="color: var(--v3-text-muted)">
                  {{ 'Назва' | t }} <span style="color: var(--v3-danger)">*</span>
                </label>
                <input [(ngModel)]="name" data-testid="v3-character-name" class="v3-input">
              </div>
              <div>
                <label class="block text-xs font-semibold uppercase tracking-wide mb-1.5" style="color: var(--v3-text-muted)">
                  {{ 'Мова' | t }}
                </label>
                <select [(ngModel)]="language" data-testid="v3-character-language" class="v3-input">
                  <option value="uk">{{ 'Українська' | t }}</option>
                  <option value="en">English</option>
                  <option value="pl">Polski</option>
                  <option value="de">Deutsch</option>
                </select>
              </div>
              <div>
                <label class="block text-xs font-semibold uppercase tracking-wide mb-1.5" style="color: var(--v3-text-muted)">
                  {{ 'Власник (бренд)' | t }}
                </label>
                <input [(ngModel)]="ownerBrandId" data-testid="v3-character-brand" class="v3-input font-mono" placeholder="vertep">
              </div>
              <div>
                <label class="block text-xs font-semibold uppercase tracking-wide mb-1.5" style="color: var(--v3-text-muted)">
                  Workflow
                </label>
                <input [(ngModel)]="workflow" data-testid="v3-character-workflow" class="v3-input font-mono" placeholder="workflows/image/demo.json">
              </div>
              <div class="flex items-end pb-2">
                <label class="inline-flex items-center gap-2 text-sm cursor-pointer" style="color: var(--v3-text)">
                  <input type="checkbox" [(ngModel)]="enabled" data-testid="v3-character-enabled" class="rounded">
                  <span>{{ 'Активний' | t }}</span>
                </label>
              </div>
            </div>
          </app-v3-paper-card>

          <app-v3-paper-card title="Системний промпт" eyebrow="Сценарій і характер" [headerDivider]="true">
            <div class="pt-3">
              <textarea [(ngModel)]="systemPrompt" rows="6" data-testid="v3-character-system-prompt"
                        class="v3-input font-mono resize-y"
                        placeholder="{{ 'Опишіть стиль мовлення, характер, знання та обмеження персонажа.' | t }}"></textarea>
            </div>
          </app-v3-paper-card>

          <app-v3-paper-card title="Налаштування голосу" eyebrow="TTS" [headerDivider]="true">
            <div class="grid grid-cols-1 md:grid-cols-3 gap-4 pt-3">
              <div>
                <label class="block text-xs font-semibold uppercase tracking-wide mb-1.5" style="color: var(--v3-text-muted)">{{ 'Провайдер' | t }}</label>
                <select [(ngModel)]="voice.provider" class="v3-input">
                  <option value="none">{{ 'Немає (тимчасово)' | t }}</option>
                  <option value="mock">{{ 'Mock (тест)' | t }}</option>
                  <option value="piper">Piper (MIT)</option>
                  <option value="kokoro">Kokoro (Apache-2.0)</option>
                </select>
              </div>
              <div>
                <label class="block text-xs font-semibold uppercase tracking-wide mb-1.5" style="color: var(--v3-text-muted)">{{ 'Мова' | t }}</label>
                <select [(ngModel)]="voice.language" class="v3-input">
                  <option value="uk">{{ 'Українська' | t }}</option>
                  <option value="en">English</option>
                  <option value="pl">Polski</option>
                  <option value="de">Deutsch</option>
                </select>
              </div>
              <div>
                <label class="block text-xs font-semibold uppercase tracking-wide mb-1.5" style="color: var(--v3-text-muted)">{{ 'Голос (voice ID)' | t }}</label>
                <input [(ngModel)]="voice.voice" class="v3-input" placeholder="uk_Kyiv">
              </div>
            </div>
          </app-v3-paper-card>

          <app-v3-paper-card title="Візуальний стиль і генерація" eyebrow="Медіаконвеєр" [headerDivider]="true">
            <div class="grid grid-cols-1 md:grid-cols-2 gap-4 pt-3">
              <div>
                <label class="block text-xs font-semibold uppercase tracking-wide mb-1.5" style="color: var(--v3-text-muted)">{{ 'Стиль' | t }}</label>
                <input [(ngModel)]="visual.style" class="v3-input" placeholder="warm documentary illustration">
              </div>
              <div>
                <label class="block text-xs font-semibold uppercase tracking-wide mb-1.5" style="color: var(--v3-text-muted)">{{ 'Співвідношення сторін' | t }}</label>
                <select [(ngModel)]="visual.aspect_ratio" class="v3-input">
                  <option value="16:9">{{ '16:9 (YouTube, горизонтальне)' | t }}</option>
                  <option value="9:16">{{ '9:16 (Shorts/Reels/TikTok, вертикальне)' | t }}</option>
                  <option value="1:1">{{ '1:1 (квадратне)' | t }}</option>
                  <option value="4:3">4:3</option>
                </select>
              </div>
              <div>
                <label class="block text-xs font-semibold uppercase tracking-wide mb-1.5" style="color: var(--v3-text-muted)">{{ 'Пресет виводу' | t }}</label>
                <select [(ngModel)]="visual.output_preset" class="v3-input">
                  <option value="youtube">YouTube (1080p/720p)</option>
                  <option value="shorts">YouTube Shorts</option>
                  <option value="tiktok">TikTok</option>
                  <option value="reels">Instagram Reels</option>
                </select>
              </div>
              <div>
                <label class="block text-xs font-semibold uppercase tracking-wide mb-1.5" style="color: var(--v3-text-muted)">{{ 'Workflow (шлях до JSON)' | t }}</label>
                <input [(ngModel)]="generation.workflow" class="v3-input font-mono" placeholder="workflows/image/demo.json">
              </div>
              <div>
                <label class="block text-xs font-semibold uppercase tracking-wide mb-1.5" style="color: var(--v3-text-muted)">{{ 'Мін. VRAM (МБ)' | t }}</label>
                <input type="number" [(ngModel)]="generation.min_vram_mb" min="0" step="512" class="v3-input">
              </div>
              <div>
                <label class="block text-xs font-semibold uppercase tracking-wide mb-1.5" style="color: var(--v3-text-muted)">{{ 'Макс. спроб' | t }}</label>
                <input type="number" [(ngModel)]="generation.max_retries" min="0" max="10" class="v3-input">
              </div>
            </div>
          </app-v3-paper-card>

          <app-v3-paper-card title="Публікація" eyebrow="Канали" [headerDivider]="true">
            <div class="space-y-3 pt-3">
              <label class="inline-flex items-center gap-2 text-sm cursor-pointer" style="color: var(--v3-text)">
                <input type="checkbox" [(ngModel)]="publishing.enabled" class="rounded">
                <span>{{ 'Увімкнути автопублікацію' | t }}</span>
              </label>
              <div>
                <label class="block text-xs font-semibold uppercase tracking-wide mb-1.5" style="color: var(--v3-text-muted)">{{ 'Канали (через кому)' | t }}</label>
                <input [(ngModel)]="publishing.channels" class="v3-input" placeholder="youtube,tiktok,facebook,instagram,threads">
              </div>
            </div>
          </app-v3-paper-card>

          <!-- Action footer -->
          <div class="flex items-center justify-end gap-3 pt-1">
            <a routerLink="/v3/characters" class="v3-btn-secondary">{{ 'Скасувати' | t }}</a>
            <button type="button" (click)="save()"
                    [disabled]="saving() || !formId || !name || !canSave"
                    [title]="(saveReason || 'Зберегти') | t"
                    data-testid="v3-character-save-btn"
                    class="v3-btn-primary">
              {{ (saving() ? 'Збереження...' : (isEdit() ? 'Зберегти' : 'Створити')) | t }}
            </button>
          </div>
        </app-v3-entity-profile>
      }
    </div>
  `,
})
export class V3CharacterDetailComponent implements OnInit, CanComponentDeactivate {
  loading = signal(true);
  error = signal<string | null>(null);
  saving = signal(false);
  isEdit = signal(false);
  useModuleApi = signal(false);
  references = signal<CharacterReference[]>([]);

  formId = '';
  name = '';
  language = 'uk';
  ownerBrandId = '';
  enabled = true;
  workflow = '';
  systemPrompt = '';
  voice: CharacterVoice = { provider: 'none', voice: '' };
  visual: CharacterVisual = { style: '', aspect_ratio: '16:9', output_preset: 'youtube' };
  generation: CharacterGeneration = { workflow: 'workflows/image/demo.json', min_vram_mb: 4096, max_retries: 3 };
  publishing: CharacterPublishing = { enabled: false, channels: '' };
  private originalSnapshot = '';

  primaryAvatarUrl = computed(() => {
    const first = this.references()[0];
    return first && this.formId ? this.referenceUrl(first.filename) : null;
  });

  badges = computed<ProfileBadge[]>(() => {
    const list: ProfileBadge[] = [{ label: (this.language || 'uk').toUpperCase(), tone: 'sky' }];
    if (this.ownerBrandId) list.push({ label: this.ownerBrandId, tone: 'coral' });
    if (this.voice.provider && this.voice.provider !== 'none') list.push({ label: this.voice.provider, tone: 'slate' });
    return list;
  });

  stats = computed<ProfileStat[]>(() => [
    { label: 'Мова', value: (this.language || 'uk').toUpperCase() },
    { label: 'Голос', value: this.voice.provider || 'none' },
    { label: 'Референси', value: String(this.references().length) },
  ]);

  constructor(
    private route: ActivatedRoute,
    private router: Router,
    readonly resources: ResourcesApiService,
    private toast: ToastService,
    private confirm: ConfirmService,
    public policy: PolicyService,
  ) {}

  get canSave(): boolean { return this.policy.can(this.isEdit() ? 'update_character' : 'create_character').allowed; }
  get saveReason(): string | null { return this.policy.disabledReason(this.isEdit() ? 'update_character' : 'create_character'); }
  get canDelete(): boolean { return this.policy.can('delete_character').allowed; }
  get deleteReason(): string | null { return this.policy.disabledReason('delete_character'); }

  ngOnInit(): void {
    this.reload();
  }

  reload(): void {
    const id = this.route.snapshot.paramMap.get('id');
    if (id && id !== 'new') {
      this.isEdit.set(true);
      this.loadCharacter(id);
    } else {
      this.isEdit.set(false);
      this.originalSnapshot = this.snapshot();
      this.loading.set(false);
    }
  }

  referenceUrl(filename: string): string {
    return this.useModuleApi()
      ? this.resources.moduleCharacterReferenceUrl(this.formId, filename)
      : this.resources.characterReferenceUrl(this.formId, filename);
  }

  private loadCharacter(id: string): void {
    this.loading.set(true);
    this.error.set(null);
    this.resources.moduleCharacter(id).pipe(
      catchError(() => {
        this.useModuleApi.set(false);
        return this.resources.character(id);
      }),
    ).subscribe({
      next: c => {
        const isMod = 'is_owner' in c || 'collaborations' in c;
        this.useModuleApi.set(isMod);
        this.formId = c.id ?? id;
        this.name = c.name || '';
        this.language = c.language || 'uk';
        this.ownerBrandId = c.owner_brand_id || '';
        this.enabled = c.enabled !== false;
        this.workflow = c.workflow || '';
        this.systemPrompt = c.system_prompt || '';
        this.voice = { provider: 'none', voice: '', ...(c.voice || {}) };
        this.visual = { style: '', aspect_ratio: '16:9', output_preset: 'youtube', ...(c.visual || {}) };
        this.generation = { workflow: 'workflows/image/demo.json', min_vram_mb: 4096, max_retries: 3, ...(c.generation || {}) };
        this.publishing = { enabled: false, channels: '', ...(c.publishing || {}) };
        if ('references' in c && Array.isArray(c.references)) {
          this.references.set(c.references);
        } else {
          this.loadReferences(this.formId);
        }
        this.originalSnapshot = this.snapshot();
        this.loading.set(false);
      },
      error: err => {
        this.error.set(err?.message || 'Не вдалося завантажити персонажа');
        this.loading.set(false);
      },
    });
  }

  private loadReferences(id: string): void {
    const req = this.useModuleApi()
      ? this.resources.moduleCharacterReferences(id)
      : this.resources.characterReferences(id);
    req.subscribe({
      next: items => this.references.set(items),
      error: () => {},
    });
  }

  uploadReferences(event: Event): void {
    const input = event.target as HTMLInputElement;
    for (const file of Array.from(input.files ?? [])) {
      const req = this.useModuleApi()
        ? this.resources.moduleUploadCharacterReference(this.formId, file, this.ownerBrandId || undefined)
        : this.resources.uploadCharacterReference(this.formId, file);
      req.subscribe({
        next: () => this.loadReferences(this.formId),
        error: err => this.toast.show(err?.error?.detail || err?.message || 'Не вдалося завантажити зображення', 'error'),
      });
    }
    input.value = '';
  }

  deleteReference(filename: string): void {
    const req = this.useModuleApi()
      ? this.resources.moduleDeleteCharacterReference(this.formId, filename, this.ownerBrandId || undefined)
      : this.resources.deleteCharacterReference(this.formId, filename);
    req.subscribe({
      next: () => this.loadReferences(this.formId),
      error: err => this.toast.show(err?.error?.detail || err?.message || 'Не вдалося видалити зображення', 'error'),
    });
  }

  save(): void {
    if (!this.canSave) return;
    if (!this.formId || !this.name) {
      this.toast.show('ID та назва обов\'язкові', 'error');
      return;
    }
    this.saving.set(true);
    const payload: Character = {
      id: this.formId,
      name: this.name,
      language: this.language,
      enabled: this.enabled,
      owner_brand_id: this.ownerBrandId || undefined,
      workflow: this.workflow || undefined,
      system_prompt: this.systemPrompt,
      voice: this.voice,
      visual: this.visual,
      generation: this.generation,
      publishing: this.publishing,
    };

    const req = this.isEdit()
      ? (this.useModuleApi()
          ? this.resources.moduleUpdateCharacter(this.formId, payload, this.ownerBrandId || undefined)
          : this.resources.updateCharacter(this.formId, payload))
      : this.resources.moduleCreateCharacter(payload, this.ownerBrandId || 'vertep').pipe(
          catchError(() => this.resources.createCharacter(payload)),
        );

    req.subscribe({
      next: () => {
        this.saving.set(false);
        this.originalSnapshot = this.snapshot();
        this.toast.show(this.isEdit() ? 'Персонаж збережено' : 'Персонаж створено', 'success');
        if (!this.isEdit()) {
          this.router.navigate(['/v3/characters', this.formId]);
        }
      },
      error: err => {
        this.saving.set(false);
        this.toast.show(err?.message || 'Помилка збереження', 'error');
      },
    });
  }

  deleteCharacter(): void {
    if (!this.canDelete) return;
    this.confirm.confirm({ title: 'Видалити персонажа', message: `Видалити ${this.formId}?` }).subscribe(ok => {
      if (!ok) return;
      const req = this.useModuleApi()
        ? this.resources.moduleDeleteCharacter(this.formId, this.ownerBrandId || undefined)
        : this.resources.deleteCharacter(this.formId);
      req.subscribe({
        next: () => {
          this.originalSnapshot = this.snapshot();
          this.toast.show('Персонаж видалено', 'success');
          this.router.navigate(['/v3/characters']);
        },
        error: err => this.toast.show(err?.message || 'Помилка видалення', 'error'),
      });
    });
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
    return JSON.stringify({
      id: this.formId,
      name: this.name,
      language: this.language,
      ownerBrandId: this.ownerBrandId,
      enabled: this.enabled,
      workflow: this.workflow,
      systemPrompt: this.systemPrompt,
      voice: this.voice,
      visual: this.visual,
      generation: this.generation,
      publishing: this.publishing,
    });
  }
}
