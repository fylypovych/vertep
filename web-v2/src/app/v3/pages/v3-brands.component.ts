import { ChangeDetectionStrategy, Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterModule } from '@angular/router';
import { HttpClient, HttpContext } from '@angular/common/http';
import { catchError, of } from 'rxjs';
import { TPipe } from '../../core/i18n/t.pipe';
import { ModuleCharacter, ResourcesApiService } from '../../core/api/resources.api';
import { BaseApiService } from '../../core/api/base-api.service';
import { ToastService } from '../../core/services/toast.service';
import { ConfirmService } from '../../core/services/confirm.service';
import { PolicyService } from '../../core/services/policy.service';
import { BACKGROUND_REQUEST } from '../../core/interceptors/auth.interceptor';
import { Brand, Channel, Character } from '../../core/models';
import { RemoteState } from '../../core/state/remote-state';
import { BrandChannelsComponent } from '../../brands/brand-channels.component';
import { BrandSharedContentComponent } from '../../brands/brand-shared-content.component';
import { LoadingStateComponent } from '../../shared/loading-state.component';
import { ErrorStateComponent } from '../../shared/error-state.component';
import { PaperCardComponent } from '../ui/paper-card.component';
import { MemberListItem, MembersListCardComponent } from '../ui/members-list-card.component';

interface AccessUserSummary {
  user: string;
  role: 'admin' | 'moderator';
  brands: string[] | null;
  display_name: string | null;
  email: string | null;
}

@Component({
  selector: 'app-v3-brands',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    RouterModule,
    TPipe,
    BrandChannelsComponent,
    BrandSharedContentComponent,
    LoadingStateComponent,
    ErrorStateComponent,
    PaperCardComponent,
    MembersListCardComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="space-y-5" data-testid="v3-brands-page">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p class="text-xs font-semibold uppercase tracking-wider" style="color: var(--v3-text-subtle)">{{ 'Студія' | t }}</p>
          <h2 class="text-xl font-semibold" style="color: var(--v3-text)">{{ 'Бренди' | t }}</h2>
        </div>
        <button type="button" (click)="openEditor()" data-testid="v3-create-brand-button"
                [disabled]="!canCreateBrand" [title]="(createBrandReason || 'Новий бренд') | t"
                class="v3-btn-primary">
          + {{ 'Новий бренд' | t }}
        </button>
      </div>

      @if (loading()) {
        <app-loading-state />
      } @else if (error()) {
        <app-error-state [message]="error()!" (retry)="loadAll()" />
      } @else if (brands().length === 0) {
        <app-v3-paper-card testId="v3-brands-empty">
          <div class="text-center py-8 text-sm" style="color: var(--v3-text-muted)">{{ 'Брендів не знайдено' | t }}</div>
        </app-v3-paper-card>
      } @else {
        <div class="space-y-6">
          @for (brand of brands(); track brand.id) {
            <app-v3-paper-card [title]="brand.name" [subtitle]="brand.id" [headerDivider]="true" [testId]="'v3-brand-card-' + brand.id">
              <div card-actions class="flex items-center gap-2">
                <span class="px-2.5 py-0.5 rounded-full text-xs font-medium"
                      [style.background-color]="brand.enabled ? 'var(--v3-accent-soft)' : '#efede7'"
                      [style.color]="brand.enabled ? 'var(--v3-accent)' : 'var(--v3-text-muted)'">
                  {{ (brand.enabled ? 'Активний' : 'Неактивний') | t }}
                </span>
                <button type="button" (click)="editBrand(brand)" [disabled]="!canUpdateBrand"
                        class="v3-btn-secondary !py-1 !px-3 text-xs">{{ 'Редагувати' | t }}</button>
                <button type="button" (click)="deleteBrand(brand)" [disabled]="!canDeleteBrand"
                        class="v3-btn-danger !py-1 !px-3 text-xs">{{ 'Видалити' | t }}</button>
              </div>

              <div class="grid grid-cols-1 lg:grid-cols-12 gap-5 pt-3">
                <!-- Team Members column: Brand Characters + (for admin) Brand Moderators -->
                <div class="lg:col-span-5 space-y-4">
                  <div class="rounded-xl border p-4" style="background-color: var(--v3-surface-subtle); border-color: var(--v3-border)">
                    <app-v3-members-list
                      title="Персонажі бренду"
                      emptyText="До цього бренду ще не прив'язано персонажів"
                      [items]="charactersForBrand(brand.id)"
                      [embedded]="true"
                      [testId]="'v3-brand-characters-' + brand.id" />
                  </div>

                  @if (isAdmin()) {
                    <div class="rounded-xl border p-4" style="background-color: var(--v3-surface-subtle); border-color: var(--v3-border)">
                      <app-v3-members-list
                        title="Модератори бренду"
                        emptyText="Закріплених модераторів немає"
                        [items]="moderatorsForBrand(brand.id)"
                        [embedded]="true"
                        [testId]="'v3-brand-moderators-' + brand.id" />
                    </div>
                  }
                </div>

                <!-- Channels & Shared Content column -->
                <div class="lg:col-span-7 space-y-4 min-w-0">
                  <app-brand-channels
                    [brandId]="brand.id"
                    [channels]="channelStates[brand.id]?.data() || []"
                    [loading]="channelStates[brand.id]?.loading() || false"
                    [error]="channelStates[brand.id]?.error() || null"
                    (retry)="loadChannels(brand.id)"
                    (channelAdded)="loadChannels(brand.id)"
                    (channelChanged)="loadChannels(brand.id)" />
                  <app-brand-shared-content [brandId]="brand.id" />
                </div>
              </div>
            </app-v3-paper-card>
          }
        </div>
      }

      <!-- Brand Editor Modal -->
      @if (showEditor()) {
        <div data-testid="v3-brand-editor-modal" class="fixed inset-0 bg-black/45 flex items-center justify-center z-50 p-4">
          <div class="v3-paper-card w-full max-w-xl p-6 max-h-[90vh] overflow-y-auto">
            <h3 class="text-lg font-semibold mb-4" style="color: var(--v3-text)">
              {{ (editingBrand() ? 'Редагувати бренд' : 'Новий бренд') | t }}
            </h3>
            <div class="space-y-4">
              <div>
                <label class="block text-xs font-semibold uppercase tracking-wide mb-1.5" style="color: var(--v3-text-muted)">ID</label>
                <input [(ngModel)]="editorForm.id" [disabled]="!!editingBrand()" class="v3-input font-mono" placeholder="my_brand">
              </div>
              <div>
                <label class="block text-xs font-semibold uppercase tracking-wide mb-1.5" style="color: var(--v3-text-muted)">{{ 'Назва' | t }}</label>
                <input [(ngModel)]="editorForm.name" class="v3-input" placeholder="{{ 'Назва бренду' | t }}">
              </div>
              <label class="inline-flex items-center gap-2 text-sm cursor-pointer" style="color: var(--v3-text)">
                <input type="checkbox" [(ngModel)]="editorForm.enabled" class="rounded">
                <span>{{ 'Бренд активний' | t }}</span>
              </label>
              <div>
                <label class="block text-xs font-semibold uppercase tracking-wide mb-1.5" style="color: var(--v3-text-muted)">{{ 'Метадані (JSON)' | t }}</label>
                <textarea [(ngModel)]="metadataJson" rows="4" class="v3-input font-mono text-xs" spellcheck="false"></textarea>
              </div>
              <div>
                <label class="block text-xs font-semibold uppercase tracking-wide mb-1.5" style="color: var(--v3-text-muted)">{{ 'Налаштування публікації (JSON)' | t }}</label>
                <textarea [(ngModel)]="publishingJson" rows="4" class="v3-input font-mono text-xs" spellcheck="false"></textarea>
              </div>
            </div>
            <div class="flex justify-end gap-2 mt-6">
              <button type="button" (click)="closeEditor()" class="v3-btn-secondary">{{ 'Скасувати' | t }}</button>
              <button type="button" (click)="saveBrand()" [disabled]="saving() || !canSaveBrand" class="v3-btn-primary">
                {{ (saving() ? 'Збереження...' : 'Зберегти') | t }}
              </button>
            </div>
          </div>
        </div>
      }
    </div>
  `,
})
export class V3BrandsComponent implements OnInit {
  brands = signal<Brand[]>([]);
  characters = signal<(Character & { references?: { filename: string }[] })[]>([]);
  users = signal<AccessUserSummary[]>([]);
  loading = signal(false);
  error = signal<string | null>(null);
  saving = signal(false);
  showEditor = signal(false);
  editingBrand = signal<Brand | null>(null);
  useModuleCharacters = signal(false);

  editorForm = { id: '', name: '', enabled: true };
  metadataJson = '{}';
  publishingJson = '{}';
  channelStates: Record<string, RemoteState<Channel[]>> = {};

  constructor(
    private resources: ResourcesApiService,
    private http: HttpClient,
    private base: BaseApiService,
    private toast: ToastService,
    private confirm: ConfirmService,
    public policy: PolicyService,
  ) {}

  isAdmin(): boolean { return this.policy.userRole() === 'admin'; }
  get canCreateBrand(): boolean { return this.policy.can('create_brand').allowed; }
  get createBrandReason(): string | null { return this.policy.disabledReason('create_brand'); }
  get canUpdateBrand(): boolean { return this.policy.can('update_brand').allowed; }
  get canDeleteBrand(): boolean { return this.policy.can('delete_brand').allowed; }
  get canSaveBrand(): boolean { return this.policy.can(this.editingBrand() ? 'update_brand' : 'create_brand').allowed; }

  ngOnInit(): void {
    this.loadAll();
  }

  loadAll(): void {
    this.loading.set(true);
    this.error.set(null);

    this.resources.brands().subscribe({
      next: brands => {
        this.brands.set(Array.isArray(brands) ? brands : []);
        brands.forEach(b => this.loadChannels(b.id));
        this.loading.set(false);
      },
      error: err => {
        this.error.set(err?.message || 'Не вдалося завантажити бренди');
        this.loading.set(false);
      },
    });

    this.resources.moduleCharacters().pipe(
      catchError(() => {
        this.useModuleCharacters.set(false);
        return this.resources.characters().pipe(catchError(() => of([] as Character[])));
      }),
    ).subscribe(items => {
      const list = Array.isArray(items) ? items : [];
      if (list.length && ('is_owner' in list[0] || 'references' in list[0])) {
        this.useModuleCharacters.set(true);
      }
      this.characters.set(list as (ModuleCharacter | Character)[]);
    });

    if (this.isAdmin()) {
      this.http.get<{ users: AccessUserSummary[] }>(`${this.base.url}/settings/users`, {
        headers: this.base.headers(),
        context: new HttpContext().set(BACKGROUND_REQUEST, true),
      }).pipe(catchError(() => of({ users: [] }))).subscribe(res => {
        this.users.set(Array.isArray(res?.users) ? res.users : []);
      });
    }
  }

  loadChannels(brandId: string): void {
    const state = this.channelStates[brandId] ||= new RemoteState<Channel[]>([]);
    state.run(() => this.resources.channels(brandId), 'Не вдалося завантажити канали');
  }

  charactersForBrand(brandId: string): MemberListItem[] {
    return this.characters()
      .filter(c => c.owner_brand_id === brandId)
      .map(c => {
        const id = c.id || c.name;
        const firstRef = c.references?.[0]?.filename;
        const avatarUrl = firstRef
          ? (this.useModuleCharacters()
              ? this.resources.moduleCharacterReferenceUrl(id, firstRef)
              : this.resources.characterReferenceUrl(id, firstRef))
          : null;
        const active = c.active !== undefined ? c.active : c.enabled !== false;
        return {
          id,
          name: c.name || id,
          subtitle: c.language ? `${id} · ${c.language.toUpperCase()}` : id,
          avatarUrl,
          statusLabel: active ? 'Активний' : 'Неактивний',
          statusTone: active ? 'ok' : 'muted',
          route: ['/v3/characters', id],
          actionLabel: 'Профіль',
        };
      });
  }

  moderatorsForBrand(brandId: string): MemberListItem[] {
    return this.users()
      .filter(u => u.role === 'moderator' && Array.isArray(u.brands) && u.brands.includes(brandId))
      .map(u => ({
        id: u.user,
        name: u.display_name || u.user,
        subtitle: u.email || `@${u.user}`,
        statusLabel: 'Модератор',
        statusTone: 'ok',
        route: ['/v3/settings'],
        actionLabel: 'Доступ',
      }));
  }

  openEditor(): void {
    this.editingBrand.set(null);
    this.editorForm = { id: '', name: '', enabled: true };
    this.metadataJson = '{}';
    this.publishingJson = '{}';
    this.showEditor.set(true);
  }

  editBrand(brand: Brand): void {
    this.editingBrand.set(brand);
    this.editorForm = { id: brand.id, name: brand.name, enabled: brand.enabled };
    this.metadataJson = JSON.stringify(brand.metadata || {}, null, 2);
    this.publishingJson = JSON.stringify(brand.publishing || {}, null, 2);
    this.showEditor.set(true);
  }

  closeEditor(): void {
    this.showEditor.set(false);
    this.editingBrand.set(null);
    this.saving.set(false);
  }

  saveBrand(): void {
    if (!this.canSaveBrand) return;
    if (!this.editorForm.id || !this.editorForm.name) {
      this.toast.show('ID та назва є обов\'язковими', 'error');
      return;
    }
    this.saving.set(true);
    const payload = {
      id: this.editorForm.id,
      name: this.editorForm.name,
      enabled: this.editorForm.enabled,
      metadata: this.parseJson(this.metadataJson),
      publishing: this.parseJson(this.publishingJson),
    };
    const req = this.editingBrand()
      ? this.resources.updateBrand(this.editorForm.id, payload)
      : this.resources.createBrand(payload);
    req.subscribe({
      next: () => {
        this.closeEditor();
        this.loadAll();
        this.toast.show(this.editingBrand() ? 'Бренд збережено' : 'Бренд створено', 'success');
      },
      error: err => {
        this.saving.set(false);
        this.toast.show(err?.message || 'Помилка збереження', 'error');
      },
    });
  }

  deleteBrand(brand: Brand): void {
    if (!this.canDeleteBrand) return;
    this.confirm.confirm({ title: 'Видалити бренд', message: `Ви впевнені, що хочете видалити бренд ${brand.name} (${brand.id})?` }).subscribe(ok => {
      if (!ok) return;
      this.resources.deleteBrand(brand.id).subscribe({
        next: () => { this.loadAll(); this.toast.show('Бренд видалено', 'success'); },
        error: err => this.toast.show(err?.message || 'Помилка видалення', 'error'),
      });
    });
  }

  private parseJson(text: string): Record<string, unknown> {
    try {
      const parsed = JSON.parse(text);
      return typeof parsed === 'object' && parsed !== null ? parsed : {};
    } catch {
      return {};
    }
  }
}
