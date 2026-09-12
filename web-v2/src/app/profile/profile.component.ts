import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormBuilder, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { Router } from '@angular/router';
import { VertepApiService } from '../core/api.service';
import { ToastService } from '../core/services/toast.service';
import { PolicyService } from '../core/services/policy.service';
import { UserProfile, ChangePasswordRequest } from '../core/models';

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
            <p class="text-slate-900 dark:text-slate-100 font-mono">{{ profile()?.user }}</p>
          </div>
          <div>
            <label class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">Роль</label>
            <span class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium"
                  [class.bg-emerald-100]="profile()?.role === 'admin'"
                  [class.text-emerald-800]="profile()?.role === 'admin'"
                  [class.bg-blue-100]="profile()?.role === 'viewer'"
                  [class.text-blue-800]="profile()?.role === 'viewer'">
              {{ profile()?.role === 'admin' ? 'Адміністратор' : 'Переглядач' }}
            </span>
          </div>
        </div>
      </div>

      <div class="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-6">
        <h3 class="text-lg font-semibold text-slate-900 dark:text-slate-100 mb-4">Зміна пароля</h3>
        <form [formGroup]="passwordForm" (ngSubmit)="changePassword()" class="space-y-4 max-w-md">
          <div>
            <label class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">Поточний пароль</label>
            <input type="password" formControlName="old_password"
                   class="w-full rounded-lg border border-slate-300 dark:border-slate-600 px-3 py-2 text-sm bg-white dark:bg-slate-700 focus:outline-none focus:ring-2 focus:ring-emerald-500" />
            <p class="text-xs text-red-600 mt-1" *ngIf="passwordForm.controls['old_password'].touched && passwordForm.controls['old_password'].invalid">Обов'язкове поле</p>
          </div>
          <div>
            <label class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">Новий пароль</label>
            <input type="password" formControlName="new_password"
                   class="w-full rounded-lg border border-slate-300 dark:border-slate-600 px-3 py-2 text-sm bg-white dark:bg-slate-700 focus:outline-none focus:ring-2 focus:ring-emerald-500" />
            <p class="text-xs text-red-600 mt-1" *ngIf="passwordForm.controls['new_password'].touched && passwordForm.controls['new_password'].invalid">
              Пароль має містити щонайменше 12 символів
            </p>
          </div>
          <div>
            <label class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">Підтвердження нового пароля</label>
            <input type="password" formControlName="confirm_password"
                   class="w-full rounded-lg border border-slate-300 dark:border-slate-600 px-3 py-2 text-sm bg-white dark:bg-slate-700 focus:outline-none focus:ring-2 focus:ring-emerald-500" />
            <p class="text-xs text-red-600 mt-1" *ngIf="passwordForm.hasError('mismatch') && passwordForm.controls['confirm_password'].touched">Паролі не співпадають</p>
          </div>
          <p class="text-xs text-slate-500 dark:text-slate-400">Пароль має містити щонайменше 12 символів</p>
          <button type="submit" [disabled]="passwordForm.invalid || loading()"
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
  passwordForm: FormGroup;
  permittedActions: string[] = [];

  constructor(
    private fb: FormBuilder,
    private api: VertepApiService,
    private toast: ToastService,
    private policy: PolicyService,
    private router: Router
  ) {
    this.passwordForm = this.fb.group({
      old_password: ['', Validators.required],
      new_password: ['', [Validators.required, Validators.minLength(12)]],
      confirm_password: ['', Validators.required],
    }, { validators: this.passwordMatchValidator });
  }

  ngOnInit(): void {
    this.loadProfile();
    this.permittedActions = this.getPermittedActions();
  }

  private loadProfile(): void {
    this.api.getUserProfile().subscribe({
      next: (p) => this.profile.set(p),
      error: () => this.toast.show('Не вдалося завантажити профіль', 'error'),
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
    const payload: ChangePasswordRequest = {
      old_password: this.passwordForm.value.old_password,
      new_password: this.passwordForm.value.new_password,
    };
    this.api.changePassword(payload).subscribe({
      next: () => {
        this.toast.show('Пароль успішно змінено', 'success');
        this.passwordForm.reset();
        this.loading.set(false);
      },
      error: (err) => {
        this.toast.show(err.message || 'Помилка зміни пароля', 'error');
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