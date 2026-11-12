import { TPipe } from '../../core/i18n/t.pipe';
import { Component, ViewChild, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { SettingsApiService } from '../../core/api/settings.api';
import { ToastService } from '../../core/services/toast.service';
import { SecretGroup, SecretGroupsComponent } from './secret-groups.component';

/** Settings → Ключі інтеграцій: keys of services Vertep itself uses; social publishing lives in «Публікація». */
@Component({
  selector: 'app-settings-secrets',
  standalone: true,
  imports: [TPipe, CommonModule, FormsModule, RouterLink, SecretGroupsComponent],
  template: `
    <div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-secrets">
      <h3 class="text-lg font-semibold mb-4">{{ 'Ключі інтеграцій' | t }}</h3>
      <p class="text-sm text-slate-600 mb-2">{{ 'Тут зберігаються токени, API keys і паролі сервісів, з якими працює Vertep. «Збережено» означає наявність значення, а не перевірку його дійсності чи готовність підключення. Доступність сервісів показано в розділі «Стан підключень».' | t }}</p>
      <p class="text-sm text-slate-600 mb-4" data-testid="secrets-publishing-moved">{{ 'Облікові дані соцмереж для публікації задаються у вкладці' | t }}
        <a [routerLink]="['/settings']" [queryParams]="{ tab: 'publishing' }" class="text-blue-700">«{{ 'Публікація' | t }}»</a>.</p>
      <app-secret-groups [groups]="serviceGroups" />
      <div class="mt-4 pt-4 border-t border-slate-200">
        <h4 class="text-sm font-medium text-slate-700 mb-2">{{ 'Новий секрет' | t }}</h4>
        <div class="flex flex-wrap gap-2">
          <label class="sr-only" for="new-secret-name">{{ 'Назва секрету' | t }}</label>
          <input id="new-secret-name" [(ngModel)]="newSecretName" placeholder="{{ 'Назва' | t }}"
                 class="px-3 py-2 border border-slate-200 rounded-lg text-sm w-48">
          <label class="sr-only" for="new-secret-value">{{ 'Значення секрету' | t }}</label>
          <input id="new-secret-value" [(ngModel)]="newSecretValue" type="password" placeholder="{{ 'Значення' | t }}"
                 class="px-3 py-2 border border-slate-200 rounded-lg text-sm flex-1 min-w-48">
          <button (click)="createSecret()" [disabled]="!newSecretName || !newSecretValue || secretSaving()"
                  class="px-3 py-2 bg-emerald-600 text-white rounded-lg text-sm disabled:opacity-50">
            {{ 'Створити' | t }}
          </button>
        </div>
      </div>
    </div>`,
})
export class SecretsSectionComponent {
  @ViewChild(SecretGroupsComponent) private groups?: SecretGroupsComponent;
  secretSaving = signal(false);
  newSecretName = '';
  newSecretValue = '';

  readonly serviceGroups: SecretGroup[] = [
    { id: 'telegram', label: 'Telegram-бот', names: ['telegram_bot_token'],
      hint: 'Один бот для керування Vertep, сповіщень і публікації в канал Telegram. Канал задається у вкладці «Публікація».' },
    { id: 'moneyprinter', label: 'MoneyPrinterTurbo', names: ['money_printer_token'] },
    { id: 'ai', label: 'Зовнішній AI', names: ['external_ai_api_key'] },
    { id: 'email', label: 'Пошта (SMTP)', names: ['smtp_password'] },
  ];

  constructor(private settings: SettingsApiService, private toast: ToastService) {}

  createSecret(): void {
    if (!this.newSecretName || !this.newSecretValue) return;
    this.secretSaving.set(true);
    this.settings.updateSecret(this.newSecretName, this.newSecretValue).subscribe({
      next: () => { this.toast.show('Секрет створено', 'success'); this.newSecretName = ''; this.newSecretValue = ''; this.secretSaving.set(false); this.groups?.load(); },
      error: (err) => { this.secretSaving.set(false); this.toast.show(err.message || 'Помилка', 'error'); },
    });
  }
}
