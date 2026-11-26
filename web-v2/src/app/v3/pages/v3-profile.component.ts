import { ChangeDetectionStrategy, Component, OnInit, computed, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormBuilder, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { TPipe } from '../../core/i18n/t.pipe';
import { AuthApiService } from '../../core/api/auth.api';
import { ResourcesApiService } from '../../core/api/resources.api';
import { ToastService } from '../../core/services/toast.service';
import { DEFAULT_PASSWORD_POLICY } from '../../core/session-identity';
import { Brand, ChangePasswordRequest, UpdateProfileRequest, UserProfile } from '../../core/models';
import { EntityProfileLayoutComponent, ProfileBadge, ProfileStat } from '../ui/entity-profile-layout.component';
import { PaperCardComponent } from '../ui/paper-card.component';

@Component({
  selector: 'app-v3-profile',
  standalone: true,
  imports: [CommonModule, ReactiveFormsModule, TPipe, EntityProfileLayoutComponent, PaperCardComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <app-v3-entity-profile
      [displayName]="displayName()"
      [subtitle]="'@' + (profile()?.user || 'user')"
      [description]="profile()?.email || ''"
      [statusLabel]="isAdmin() ? 'Адміністратор' : 'Модератор'"
      statusTone="ok"
      accent="emerald"
      [badges]="badges()"
      [stats]="stats()"
      testId="v3-profile-page">

      <!-- Left extra card: Permitted actions -->
      <app-v3-paper-card profile-left-extra title="Дозволені дії" eyebrow="RBAC" testId="v3-profile-permissions">
        <ul class="space-y-1.5 pt-1">
          @for (action of permittedActions(); track action) {
            <li class="flex items-center gap-2 text-xs py-1 px-2.5 rounded-lg" style="background-color: var(--v3-surface-subtle); color: var(--v3-text-muted)">
              <span class="w-1.5 h-1.5 rounded-full flex-shrink-0" style="background-color: var(--v3-accent)"></span>
              <span>{{ action | t }}</span>
            </li>
          }
        </ul>
      </app-v3-paper-card>

      <!-- Right column: Account details -->
      <app-v3-paper-card id="account" title="Обліковий запис" subtitle="Відображуване ім'я та контактний email" eyebrow="Профіль" [headerDivider]="true">
        <form [formGroup]="accountForm" (ngSubmit)="saveAccount()" class="space-y-4 pt-3" data-testid="v3-account-form">
          <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label class="block text-xs font-semibold uppercase tracking-wide mb-1.5" style="color: var(--v3-text-muted)">
                {{ 'Логін' | t }}
              </label>
              <input type="text" [value]="profile()?.user || ''" disabled class="v3-input font-mono" data-testid="v3-profile-login">
            </div>
            <div>
              <label class="block text-xs font-semibold uppercase tracking-wide mb-1.5" style="color: var(--v3-text-muted)">
                {{ 'Роль' | t }}
              </label>
              <input type="text" [value]="(isAdmin() ? 'Адміністратор' : 'Модератор') | t" disabled class="v3-input" data-testid="v3-profile-role">
            </div>
            <div>
              <label for="v3-display-name" class="block text-xs font-semibold uppercase tracking-wide mb-1.5" style="color: var(--v3-text-muted)">
                {{ 'Ім\\'я' | t }}
              </label>
              <input id="v3-display-name" type="text" formControlName="display_name" class="v3-input" data-testid="v3-display-name-input">
              @if (accountForm.controls['display_name'].touched && accountForm.controls['display_name'].invalid) {
                <p class="text-xs mt-1" style="color: var(--v3-danger)">{{ 'Ім\\'я не може бути довшим за 80 символів' | t }}</p>
              }
            </div>
            <div>
              <label for="v3-email" class="block text-xs font-semibold uppercase tracking-wide mb-1.5" style="color: var(--v3-text-muted)">
                Email
              </label>
              <input id="v3-email" type="email" formControlName="email" class="v3-input" data-testid="v3-email-input">
              @if (accountForm.controls['email'].touched && accountForm.controls['email'].invalid) {
                <p class="text-xs mt-1" style="color: var(--v3-danger)">{{ 'Невірний формат email' | t }}</p>
              }
            </div>
          </div>

          @if (accountError()) {
            <p class="text-xs font-medium" style="color: var(--v3-danger)" data-testid="v3-account-error">{{ accountError()! | t }}</p>
          }

          <div class="flex justify-end pt-2">
            <button type="submit" class="v3-btn-primary" [disabled]="accountForm.invalid || accountSaving()" data-testid="v3-account-save">
              {{ (accountSaving() ? 'Збереження...' : 'Зберегти') | t }}
            </button>
          </div>
        </form>
      </app-v3-paper-card>

      <!-- Right column: Password change -->
      <app-v3-paper-card id="password" title="Зміна пароля" eyebrow="Безпека" [headerDivider]="true">
        <form [formGroup]="passwordForm" (ngSubmit)="changePassword()" class="space-y-4 pt-3" data-testid="v3-password-form">
          <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div>
              <label for="v3-old-password" class="block text-xs font-semibold uppercase tracking-wide mb-1.5" style="color: var(--v3-text-muted)">
                {{ 'Поточний пароль' | t }}
              </label>
              <input id="v3-old-password" type="password" formControlName="old_password" class="v3-input" data-testid="v3-old-password-input">
            </div>
            <div>
              <label for="v3-new-password" class="block text-xs font-semibold uppercase tracking-wide mb-1.5" style="color: var(--v3-text-muted)">
                {{ 'Новий пароль' | t }}
              </label>
              <input id="v3-new-password" type="password" formControlName="new_password" class="v3-input" data-testid="v3-new-password-input">
            </div>
            <div>
              <label for="v3-confirm-password" class="block text-xs font-semibold uppercase tracking-wide mb-1.5" style="color: var(--v3-text-muted)">
                {{ 'Підтвердження нового пароля' | t }}
              </label>
              <input id="v3-confirm-password" type="password" formControlName="confirm_password" class="v3-input" data-testid="v3-confirm-password-input">
            </div>
          </div>

          <p class="text-xs" style="color: var(--v3-text-subtle)" data-testid="v3-password-policy-hint">
            {{ 'Пароль має містити щонайменше' | t }} {{ minPasswordLength() }} {{ 'символів і відрізнятися від поточного' | t }}
          </p>

          @if (passwordForm.hasError('mismatch') && passwordForm.controls['confirm_password'].touched) {
            <p class="text-xs font-medium" style="color: var(--v3-danger)">{{ 'Паролі не співпадають' | t }}</p>
          }
          @if (passwordError()) {
            <p class="text-xs font-medium" style="color: var(--v3-danger)" data-testid="v3-password-error">{{ passwordError()! | t }}</p>
          }

          <div class="flex justify-end pt-1">
            <button type="submit" class="v3-btn-primary" [disabled]="passwordForm.invalid || passwordSaving()" data-testid="v3-password-save">
              {{ (passwordSaving() ? 'Збереження...' : 'Змінити пароль') | t }}
            </button>
          </div>
        </form>
      </app-v3-paper-card>
    </app-v3-entity-profile>
  `,
})
export class V3ProfileComponent implements OnInit {
  profile = signal<UserProfile | null>(null);
  accessibleBrands = signal<Brand[]>([]);
  accountSaving = signal(false);
  accountError = signal<string | null>(null);
  passwordSaving = signal(false);
  passwordError = signal<string | null>(null);
  minPasswordLength = signal(DEFAULT_PASSWORD_POLICY.min_length);

  accountForm: FormGroup;
  passwordForm: FormGroup;

  isAdmin = computed(() => this.profile()?.role === 'admin');
  displayName = computed(() => this.profile()?.display_name || this.profile()?.user || 'Користувач');

  badges = computed<ProfileBadge[]>(() => {
    const p = this.profile();
    if (!p) return [];
    const list: ProfileBadge[] = [{ label: p.role === 'admin' ? 'Адміністратор' : 'Модератор', tone: p.role === 'admin' ? 'emerald' : 'sky' }];
    this.accessibleBrands().slice(0, 4).forEach(b => list.push({ label: b.name || b.id, tone: 'slate' }));
    return list;
  });

  permittedActions = computed<string[]>(() => {
    if (this.isAdmin()) {
      return [
        'Створення завдань', 'Видалення завдань', 'Затвердження завдань', 'Публікація завдань',
        'Управління воркерами', 'Створення персонажів', 'Редагування персонажів', 'Управління брендами',
        'Зміна налаштувань', 'Запуск оновлень', 'Створення бекапів', 'Управління секретами',
      ];
    }
    return [
      'Створення завдань для своїх брендів', 'Керування персонажами своїх брендів',
      'Перегляд дашборду', 'Перегляд завдань', 'Перегляд опублікованих',
      'Перегляд воркерів', 'Перегляд персонажів', 'Перегляд брендів',
      'Перегляд алертів', 'Перегляд логів', 'Перегляд стану системи',
    ];
  });

  stats = computed<ProfileStat[]>(() => {
    const brandsCount = this.isAdmin() ? 'Усі' : String(this.accessibleBrands().length);
    return [
      { label: 'Бренди', value: brandsCount },
      { label: 'Права', value: String(this.permittedActions().length) },
      { label: 'Мін. пароль', value: String(this.minPasswordLength()) },
    ];
  });

  constructor(
    private fb: FormBuilder,
    private auth: AuthApiService,
    private resources: ResourcesApiService,
    private toast: ToastService,
  ) {
    this.accountForm = this.fb.group({
      display_name: ['', [Validators.maxLength(80)]],
      email: ['', [Validators.email]],
    });
    this.passwordForm = this.fb.group({
      old_password: ['', Validators.required],
      new_password: ['', [Validators.required, Validators.minLength(this.minPasswordLength())]],
      confirm_password: ['', Validators.required],
    }, { validators: this.passwordMatchValidator });
  }

  ngOnInit(): void {
    this.auth.getUserProfile().subscribe({
      next: p => {
        this.profile.set(p);
        this.minPasswordLength.set(p.password_policy.min_length);
        const ctrl = this.passwordForm.controls['new_password'];
        ctrl.setValidators([Validators.required, Validators.minLength(p.password_policy.min_length)]);
        ctrl.updateValueAndValidity();
        this.accountForm.patchValue({ display_name: p.display_name ?? '', email: p.email ?? '' });
      },
      error: err => this.toast.show(err?.message || 'Не вдалося завантажити профіль', 'error'),
    });
    this.resources.brands().subscribe({
      next: items => this.accessibleBrands.set(Array.isArray(items) ? items : []),
      error: () => {},
    });
  }

  saveAccount(): void {
    if (this.accountForm.invalid) return;
    this.accountSaving.set(true);
    this.accountError.set(null);
    const payload: UpdateProfileRequest = {
      display_name: this.accountForm.value.display_name ?? '',
      email: this.accountForm.value.email ?? '',
    };
    this.auth.updateProfile(payload).subscribe({
      next: p => {
        this.profile.set(p);
        this.accountSaving.set(false);
        this.toast.show('Профіль збережено', 'success');
      },
      error: err => {
        this.accountError.set(err?.message || 'Не вдалося зберегти профіль');
        this.accountSaving.set(false);
      },
    });
  }

  changePassword(): void {
    if (this.passwordForm.invalid) return;
    this.passwordSaving.set(true);
    this.passwordError.set(null);
    const payload: ChangePasswordRequest = {
      old_password: this.passwordForm.value.old_password,
      new_password: this.passwordForm.value.new_password,
    };
    this.auth.changePassword(payload).subscribe({
      next: () => {
        this.toast.show('Пароль успішно змінено', 'success');
        this.passwordForm.reset();
        this.passwordSaving.set(false);
      },
      error: err => {
        this.passwordError.set(err?.message || 'Помилка зміни пароля');
        this.passwordSaving.set(false);
      },
    });
  }

  private passwordMatchValidator(form: FormGroup) {
    const a = form.get('new_password')?.value;
    const b = form.get('confirm_password')?.value;
    return a && b && a !== b ? { mismatch: true } : null;
  }
}
