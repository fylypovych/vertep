import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { SettingsApiService } from '../../core/api/settings.api';
import { TelegramStatus, TelegramBotInfo } from '../../core/models';
import { LoadingStateComponent } from '../../shared/loading-state.component';
import { ErrorStateComponent } from '../../shared/error-state.component';

@Component({
  selector: 'app-settings-telegram',
  standalone: true,
  imports: [CommonModule, LoadingStateComponent, ErrorStateComponent],
  template: `<div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-telegram"><h3 class="text-lg font-semibold mb-4">Telegram</h3><app-loading-state *ngIf="loading()" /><app-error-state *ngIf="error()" [message]="error()!" /><div *ngIf="tgStatus()" class="grid grid-cols-1 md:grid-cols-2 gap-3 text-sm"><p>Статус: {{ tgStatus()!.configured ? 'Налаштовано' : 'Не налаштовано' }}</p><p>Ім’я бота: {{ tgStatus()!.bot_username || '-' }}</p><p>Режим: {{ tgStatus()!.polling_enabled ? 'Опитування' : 'Вебхук' }}</p><p>Дозволені chat ID: {{ tgStatus()!.allowed_chat_ids || '-' }}</p><p>Адміністративні chat ID: {{ tgStatus()!.admin_chat_ids || '-' }}</p></div></div>`,
})
export class TelegramSectionComponent implements OnInit {
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
