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
  /* template: `
    <div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-telegram">
      <h3 class="text-lg font-semibold text-slate-900 mb-4">Telegram</h3>
      @if (loading()) {
        <app-loading-state />
      } @else if (error()) {
        <app-error-state [message]="error()!" />
      } @else if (tgStatus()) {
        <div class="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-2 text-sm">
          <div class="flex justify-between py-2 border-b border-slate-100">
            <span class="text-slate-500">�����</span>
            <span class="font-medium" [class.text-emerald-600]="tgStatus()!.configured" [class.text-slate-400]="!tgStatus()!.configured">
              {{ tgStatus()!.configured ? '�����⮢���' : '�� �����⮢���' }}
            </span>
          </div>
          <div class="flex justify-between py-2 border-b border-slate-100">
            <span class="text-slate-500">Bot username</span>
            <span class="font-medium">{{ tgStatus()!.bot_username || '-' }}</span>
          </div>
          <div class="flex justify-between py-2 border-b border-slate-100">
            <span class="text-slate-500">�����</span>
            <span class="font-medium">{{ tgStatus()!.polling_enabled ? 'Polling' : 'Webhook' }}</span>
          </div>
          <div class="flex justify-between py-2 border-b border-slate-100">
            <span class="text-slate-500">Webhook URL</span>
            <span class="font-mono text-xs break-all">{{ tgStatus()!.webhook_url || '-' }}</span>
          </div>
          <div class="flex justify-between py-2 border-b border-slate-100">
            <span class="text-slate-500">Allowed chat IDs</span>
            <span class="font-mono text-xs break-all">{{ tgStatus()!.allowed_chat_ids || '-' }}</span>
          </div>
          <div class="flex justify-between py-2 border-b border-slate-100">
            <span class="text-slate-500">Admin chat IDs</span>
            <span class="font-mono text-xs break-all">{{ tgStatus()!.admin_chat_ids || '-' }}</span>
          </div>
          @if (tgBotInfo()) {
            <div class="flex justify-between py-2 border-b border-slate-100">
              <span class="text-slate-500">Bot info</span>
              <span class="font-mono text-xs">{{ tgBotInfo()!['first_name'] || '-' }}</span>
            </div>
          }
        }
      }
    </div>
  `, */
  template: `<div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-telegram"><h3 class="text-lg font-semibold mb-4">Telegram</h3><app-loading-state *ngIf="loading()" /><app-error-state *ngIf="error()" [message]="error()!" /><div *ngIf="tgStatus()" class="grid grid-cols-1 md:grid-cols-2 gap-3 text-sm"><p>Статус: {{ tgStatus()!.configured ? 'Налаштовано' : 'Не налаштовано' }}</p><p>Bot username: {{ tgStatus()!.bot_username || '-' }}</p><p>Режим: {{ tgStatus()!.polling_enabled ? 'Polling' : 'Webhook' }}</p><p>Allowed chat IDs: {{ tgStatus()!.allowed_chat_ids || '-' }}</p><p>Admin chat IDs: {{ tgStatus()!.admin_chat_ids || '-' }}</p></div></div>`,
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
