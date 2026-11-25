import { TPipe } from '../../core/i18n/t.pipe';
import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { SettingsApiService } from '../../core/api/settings.api';
import { TelegramStatus, TelegramBotInfo } from '../../core/models';
import { LoadingStateComponent } from '../../shared/loading-state.component';
import { ErrorStateComponent } from '../../shared/error-state.component';
import { SecretGroup, SecretGroupsComponent } from './secret-groups.component';

@Component({
  selector: 'app-settings-telegram',
  standalone: true,
  imports: [TPipe, CommonModule, LoadingStateComponent, ErrorStateComponent, SecretGroupsComponent],
  template: `<div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-telegram"><h3 class="text-lg font-semibold mb-4">VertepTelegramBot</h3><app-loading-state *ngIf="loading()" /><app-error-state *ngIf="error()" [message]="error()!" /><div *ngIf="tgStatus()" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 text-sm"><p class="border border-slate-200 rounded-xl p-4">{{ 'Статус:' | t }} {{ (tgStatus()!.configured ? 'Налаштовано' : 'Не налаштовано') | t }}</p><p class="border border-slate-200 rounded-xl p-4">{{ 'Ім’я бота:' | t }} {{ tgStatus()!.bot_username || '-' }}</p><p class="border border-slate-200 rounded-xl p-4">{{ 'Режим:' | t }} {{ (tgStatus()!.polling_enabled ? 'Опитування' : 'Вебхук') | t }}</p><p class="border border-slate-200 rounded-xl p-4">{{ 'Дозволені chat ID:' | t }} {{ tgStatus()!.allowed_chat_ids || '-' }}</p><p class="border border-slate-200 rounded-xl p-4">{{ 'Адміністративні chat ID:' | t }} {{ tgStatus()!.admin_chat_ids || '-' }}</p></div><h4 class="text-base font-semibold mt-6 mb-1">{{ 'Токен бота' | t }}</h4><p class="text-sm text-slate-500 mb-3">{{ 'Бот для запитів користувачів, керування Vertep і сповіщень. У канали він не публікує: канали публікації задаються в «Бренди → Канали».' | t }}</p><app-secret-groups [groups]="botGroups" data-testid="telegram-bot-token" /></div>`,
})
export class TelegramSectionComponent implements OnInit {
  /** The management bot's token lives with its status here, not among the integration keys. */
  readonly botGroups: SecretGroup[] = [{ id: 'telegram', label: 'Telegram-бот', names: ['telegram_bot_token'] }];
  tgStatus = signal<TelegramStatus | null>(null);
  tgBotInfo = signal<TelegramBotInfo | null>(null);
  loading = signal(false);
  error = signal<string | null>(null);

  constructor(private settings: SettingsApiService) {}

  ngOnInit(): void {
    this.loading.set(true);
    this.settings.telegramStatus().subscribe({
      next: (status) => { this.tgStatus.set(status); this.loading.set(false); },
      error: (err) => { this.error.set(err.message); this.loading.set(false); },
    });
    this.settings.telegramBotInfo().subscribe({
      next: (info) => this.tgBotInfo.set(info),
      error: () => {},
    });
  }
}
