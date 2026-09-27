import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormBuilder, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { Router } from '@angular/router';
import { AuthApiService } from '../core/api/auth.api';
import { ToastService } from '../core/services/toast.service';
import { PolicyService } from '../core/services/policy.service';
import { DEFAULT_PASSWORD_POLICY } from '../core/session-identity';
import { UserProfile, ChangePasswordRequest, UpdateProfileRequest } from '../core/models';

@Component({
  selector: 'app-profile',
  standalone: true,
  imports: [CommonModule, ReactiveFormsModule],
  template: `
    <div class="space-y-6" data-testid="profile-page">
      <div class="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-6">
        <h2 class="text-xl font-semibold text-slate-900 dark:text-slate-100 mb-4">Профіль користувача</h2>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
          <div>
            <label class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">Логін</label>
            <p class="text-slate-900 dark:text-slate-100 font-mono" data-testid="profile-login">{{ profile()?.user }}</p>
          </div>
          <div>
            <label class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">Роль</label>
            <span class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium" data-testid="profile-role"
                  [class.bg-emerald-100]="profile()?.role === 'admin'"
                  [class.text-emerald-800]="profile()?.role === 'admin'"
                  [class.bg-blue-100]="profile()?.role === 'viewer'"
                  [class.text-blue-800]="profile()?.role === 'viewer'">
              {{ profile()?.role === 'admin' ? 'Адміністратор' : 'Переглядач' }}
            </span>
          </div>
        </div>
      </div>

      <div id="account" class="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-6">
        <h3 class="text-lg font-semibold text-slate-900 dark:text-slate-100 mb-4">Обліковий запис</h3>
        <form [formGroup]="accountForm" (ngSubmit)="saveAccount()" class="space-y-4 max-w-md" data-testid="account-form">
          <div>
            <label for="profile-display-name" class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">Ім'я</label>
            <input id="profile-display-name" type="text" formControlName="display_name" data-testid="display-name-input"
                   class="w-full rounded-lg border border-slate-300 dark:border-slate-600 px-3 py-2 text-sm bg-white dark:bg-slate-700 focus:outline-none focus:ring-2 focus:ring-emerald-500" />
            <p class="text-xs text-red-600 mt-1" *ngIf="accountForm.controls['display_name'].touched && accountForm.controls['display_name'].invalid">
              Ім'я не може бути довшим за 80 символів
            </p>
          </div>
          <div>
            <label for="profile-email" class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">Email</label>
            <input id="profile-email" type="email" formControlName="email" data-testid="email-input"
                   class="w-full rounded-lg border border-slate-300 dark:border-slate-600 px-3 py-2 text-sm bg-white dark:bg-slate-700 focus:outline-none focus:ring-2 focus:ring-emerald-500" />
            <p class="text-xs text-red-600 mt-1" *ngIf="accountForm.controls['email'].touched && accountForm.controls['email'].invalid">
              Невірний формат email
            </p>
          </div>
          <p class="text-sm text-emerald-700 dark:text-emerald-400" *ngIf="accountError()" data-testid="account-error">{{ accountError() }}</p>
          <button type="submit" data-testid="account-save"
                  [disabled]="accountForm.invalid || accountSaving()"
                  class="px-4 py-2 bg-emerald-600 text-white rounded-lg text-sm font-medium hover:bg-emerald-700 disabled:opacity-50">
            {{ accountSaving() ? 'Збереження...' : 'Зберегти' }}
          </button>
        </form>
      </div>

      <div id="password" class="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-6">
        <h3 class="text-lg font-semibold text-slate-900 dark:text-slate-100 mb-4">Зміна пароля</h3>
        <form [formGroup]="passwordForm" (ngSubmit)="changePassword()" class="space-y-4 max-w-md" data-testid="password-form">
          <div>
            <label for="profile-old-password" class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">Поточний пароль</label>
            <input id="profile-old-password" type="password" formControlName="old_password" data-testid="old-password-input"
                   class="w-full rounded-lg border border-slate-300 dark:border-slate-600 px-3 py-2 text-sm bg-white dark:bg-slate-700 focus:outline-none focus:ring-2 focus:ring-emerald-500" />
            <p class="text-xs text-red-600 mt-1" *ngIf="passwordForm.controls['old_password'].touched && passwordForm.controls['old_password'].invalid">Обов'язкове поле</p>
          </div>
          <div>
            <label for="profile-new-password" class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">Новий пароль</label>
            <input id="profile-new-password" type="password" formControlName="new_password" data-testid="new-password-input"
                   class="w-full rounded-lg border border-slate-300 dark:border-slate-600 px-3 py-2 text-sm bg-white dark:bg-slate-700 focus:outline-none focus:ring-2 focus:ring-emerald-500" />
            <p class="text-xs text-red-600 mt-1" *ngIf="passwordForm.controls['new_password'].touched && passwordForm.controls['new_password'].invalid">
              Пароль має містити щонайменше {{ minPasswordLength() }} символів
            </p>
          </div>
          <div>
            <label for="profile-confirm-password" class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">Підтвердження нового пароля</label>
            <input id="profile-confirm-password" type="password" formControlName="confirm_password" data-testid="confirm-password-input"
                   class="w-full rounded-lg border border-slate-300 dark:border-slate-600 px-3 py-2 text-sm bg-white dark:bg-slate-700 focus:outline-none focus:ring-2 focus:ring-emerald-500" />
            <p class="text-xs text-red-600 mt-1" *ngIf="passwordForm.hasError('mismatch') && passwordForm.controls['confirm_password'].touched">Паролі не співпадають</p>
          </div>
          <p class="text-xs text-slate-500 dark:text-slate-400" data-testid="password-policy-hint">
            Пароль має містити щонайменше {{ minPasswordLength() }} символів і відрізнятися від поточного
          </p>
          <p class="text-sm text-red-600" *ngIf="passwordError()" data-testid="password-error">{{ passwordError() }}</p>
          <button type="submit" data-testid="password-save"
                  [disabled]="passwordForm.invalid || loading()"
                  class="px-4 py-2 bg-emerald-600 text-white rounded-lg text-sm font-medium hover:bg-emerald-700 disabled:opacity-50">
            {{ loading() ? 'Збереження...' : 'Змінити пароль' }}
          </button>
        </form>
      </div>

      <div class="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-6">
        <h3 class="text-lg font-semibold text-slate-900 dark:text-slate-100 mb-4">Дозволені дії</h3>
        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3 text-sm">
          @for (action of permittedActions; track action) {
            <div class="flex items-center gap-2 px-3 py-2 bg-slate-50 dark:bg-slate-700 rounded-lg">
              <span class="w-2 h-2 rounded-full bg-emerald-500"></span>
              <span class="text-slate-700 dark:text-slate-300">{{ action }}</span>
            </div>
          }
        </div>
      </div>
    </div>
  `,
})
export class ProfileComponent implements OnInit {
  profile = signal<UserProfile | null>(null);
  loading = signal(false);
  accountSaving = signal(false);
  passwordError = signal<string | null>(null);
  accountError = signal<string | null>(null);
  minPasswordLength = signal(DEFAULT_PASSWORD_POLICY.min_length);
  passwordForm: FormGroup;
  accountForm: FormGroup;
  permittedActions: string[] = [];

  constructor(
    private fb: FormBuilder,
    private auth: AuthApiService,
    private toast: ToastService,
    private policy: PolicyService,
    private router: Router
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
    this.loadProfile();
    this.permittedActions = this.getPermittedActions();
  }

  private loadProfile(): void {
    this.auth.getUserProfile().subscribe({
      next: (p) => {
        this.profile.set(p);
        this.minPasswordLength.set(p.password_policy.min_length);
        const newPassword = this.passwordForm.controls['new_password'];
        newPassword.setValidators([Validators.required, Validators.minLength(p.password_policy.min_length)]);
        newPassword.updateValueAndValidity();
        this.accountForm.patchValue({ display_name: p.display_name ?? '', email: p.email ?? '' });
      },
      error: (err) => this.toast.show(err?.message || 'Не вдалося завантажити профіль', 'error'),
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
      next: (p) => {
        this.profile.set(p);
        this.accountSaving.set(false);
        this.toast.show('Профіль збережено', 'success');
      },
      error: (err) => {
        this.accountError.set(err?.message || 'Не вдалося зберегти профіль');
        this.accountSaving.set(false);
      },
    });
  }

  private passwordMatchValidator(form: FormGroup) {
    const newPass = form.get('new_password')?.value;
    const confirmPass = form.get('confirm_password')?.value;
    return newPass && confirmPass && newPass !== confirmPass ? { mismatch: true } : null;
  }

  changePassword(): void {
    if (this.passwordForm.invalid) return;
    this.loading.set(true);
    this.passwordError.set(null);
    const payload: ChangePasswordRequest = {
      old_password: this.passwordForm.value.old_password,
      new_password: this.passwordForm.value.new_password,
    };
    this.auth.changePassword(payload).subscribe({
      next: () => {
        this.toast.show('Пароль успішно змінено', 'success');
        this.passwordForm.reset();
        this.loading.set(false);
      },
      error: (err) => {
        // Невірний поточний пароль лишається відновлюваною помилкою: сесія
        // чинна, форма доступна для повторної спроби.
        this.passwordError.set(err?.message || 'Помилка зміни пароля');
        this.loading.set(false);
      },
    });
  }

  private getPermittedActions(): string[] {
    const role = this.profile()?.role || 'viewer';
    if (role === 'admin') {
      return [
        'Створення завдань',
        'Видалення завдань',
        'Затвердження завдань',
        'Публікація завдань',
        'Управління воркерами',
        'Створення персонажів',
        'Редагування персонажів',
        'Управління брендами',
        'Зміна налаштувань',
        'Запуск оновлень',
        'Створення бекапів',
        'Відновлення бекапів',
        'Управління секретами',
      ];
    }
    return [
      'Перегляд дашборду',
      'Перегляд завдань',
      'Перегляд опублікованих',
      'Перегляд воркерів',
      'Перегляд персонажів',
      'Перегляд сценаріїв',
      'Перегляд брендів',
      'Перегляд алертів',
      'Перегляд логів',
      'Перегляд стану системи',
      'Перегляд налаштувань',
    ];
  }
}
