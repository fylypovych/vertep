import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { VertepApiService } from '../../core/api.service';
import { TelegramStatus, TelegramBotInfo } from '../../core/models';

@Component({
  selector: 'app-settings-telegram',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-telegram">
      <h3 class="text-lg font-semibold text-slate-900 mb-4">Telegram</h3>
      @if (loading()) {
        <div class="animate-pulse space-y-2">
          <div class="h-5 bg-slate-200 rounded w-full"></div>
          <div class="h-5 bg-slate-200 rounded w-1/2"></div>
        </div>
      } @else if (error()) {
        <p class="text-red-600">{{ error() }}</p>
      } @else if (tgStatus()) {
        <div class="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-2 text-sm">
          <div class="flex justify-between py-2 border-b border-slate-100">
            <span class="text-slate-500">Статус</span>
            <span class="font-medium" [class.text-emerald-600]="tgStatus()!.configured" [class.text-slate-400]="!tgStatus()!.configured">
              {{ tgStatus()!.configured ? 'Налаштовано' : 'Не налаштовано' }}
            </span>
          </div>
          <div class="flex justify-between py-2 border-b border-slate-100">
            <span class="text-slate-500">Bot username</span>
            <span class="font-medium">{{ tgStatus()!.bot_username || '—' }}</span>
          </div>
          <div class="flex justify-between py-2 border-b border-slate-100">
            <span class="text-slate-500">Режим</span>
            <span class="font-medium">{{ tgStatus()!.polling_enabled ? 'Polling' : 'Webhook' }}</span>
          </div>
          <div class="flex justify-between py-2 border-b border-slate-100">
            <span class="text-slate-500">Webhook URL</span>
            <span class="font-mono text-xs break-all">{{ tgStatus()!.webhook_url || '—' }}</span>
          </div>
          <div class="flex justify-between py-2 border-b border-slate-100">
            <span class="text-slate-500">Allowed chat IDs</span>
            <span class="font-mono text-xs break-all">{{ tgStatus()!.allowed_chat_ids || '—' }}</span>
          </div>
          <div class="flex justify-between py-2 border-b border-slate-100">
            <span class="text-slate-500">Admin chat IDs</span>
            <span class="font-mono text-xs break-all">{{ tgStatus()!.admin_chat_ids || '—' }}</span>
          </div>
          @if (tgBotInfo()) {
            <div class="flex justify-between py-2 border-b border-slate-100">
              <span class="text-slate-500">Bot info</span>
              <span class="font-mono text-xs">{{ tgBotInfo()!['first_name'] || '—' }}</span>
            </div>
          }
        </div>
      }
    </div>
  `,
})
export class TelegramSectionComponent implements OnInit {
  tgStatus = signal<TelegramStatus | null>(null);
  tgBotInfo = signal<TelegramBotInfo | null>(null);
  loading = signal(false);
  error = signal<string | null>(null);

  constructor(private api: VertepApiService) {}

  ngOnInit(): void {
    this.loading.set(true);
    this.api.getTelegramStatus().subscribe({
      next: (status) => { this.tgStatus.set(status); this.loading.set(false); },
      error: (err) => { this.error.set(err.message); this.loading.set(false); },
    });
    this.api.getTelegramBotInfo().subscribe({
      next: (info) => this.tgBotInfo.set(info),
      error: () => {},
    });
  }
}
