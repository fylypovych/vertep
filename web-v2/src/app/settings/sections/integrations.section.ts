import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { SettingsApiService } from '../../core/api/settings.api';
import { IntegrationStatus } from '../../core/models';
import { LoadingStateComponent } from '../../shared/loading-state.component';
import { ErrorStateComponent } from '../../shared/error-state.component';

@Component({
  selector: 'app-settings-integrations',
  standalone: true,
  imports: [CommonModule, LoadingStateComponent, ErrorStateComponent],
  template: `<div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-integrations">
    <h3 class="text-lg font-semibold mb-4">Стан підключень</h3>
    <p class="text-sm text-slate-600 mb-4">Ключі й паролі змінюються в розділі «Секрети»; збережений ключ сам по собі не підтверджує готовність сервісу.</p>
    <p class="text-sm text-slate-600 mb-4">Telegram нижче — публікація повідомлень, а не стан бота керування.</p>
    <button type="button" (click)="ngOnInit()" [disabled]="loading()" data-testid="publisher-test" class="px-3 py-2 border rounded mb-3">Перевірити підключення</button>
    <app-loading-state *ngIf="loading()" /><app-error-state *ngIf="error()" [message]="error()!" />
    <div *ngIf="!loading() && !error()" class="space-y-2">
      <div class="flex justify-between"><span>Ollama</span><span>{{ integrations()?.ollama?.status || 'OFFLINE' }}</span></div>
      <div class="flex justify-between"><span>ComfyUI</span><span>{{ integrations()?.comfyui?.status || 'OFFLINE' }}</span></div>
      <div *ngFor="let ch of publisherChannels()" [attr.data-testid]="'publisher-status-' + ch.key">
        <div class="flex justify-between items-center gap-3"><span>{{ ch.label }}</span><span>{{ publisherStatus(ch.val) }}</span>
          <button *ngIf="ch.key !== 'telegram'" type="button" [disabled]="busy() !== null" (click)="reconnect(ch.key)" [attr.data-testid]="'publisher-reconnect-' + ch.key" class="px-3 py-2 border rounded">Перепідключити</button>
        </div>
        <p *ngIf="messages()[ch.key]" role="status" class="text-sm">{{ messages()[ch.key] }}</p>
      </div>
    </div>
  </div>`,
})
export class IntegrationsSectionComponent implements OnInit {
  integrations = signal<IntegrationStatus | null>(null);
  loading = signal(false);
  error = signal<string | null>(null);
  busy = signal<string | null>(null);
  messages = signal<Record<string, string>>({});

  constructor(private settings: SettingsApiService) {}

  ngOnInit(): void {
    this.error.set(null);
    this.loading.set(true);
    this.settings.integrations().subscribe({
      next: (data) => { this.integrations.set(data as unknown as IntegrationStatus); this.loading.set(false); },
      error: (err) => { this.error.set(err.message); this.loading.set(false); },
    });
  }

  reconnect(channel: string): void {
    this.busy.set(channel);
    this.settings.reconnectPublisher(channel).subscribe({
      next: data => {
        this.messages.update(messages => ({ ...messages, [channel]: data.error || (data.reconnected && data.ready ? 'Підключення відновлено' : 'Підключення не готове') }));
        this.busy.set(null);
        this.ngOnInit();
      },
      error: error => {
        this.messages.update(messages => ({ ...messages, [channel]: error.message }));
        this.busy.set(null);
      },
    });
  }

  publisherStatus(val: { configured: boolean; ready?: boolean; missing_scopes?: string[] }): string {
    if (!val?.configured) return 'Не налаштовано';
    if (val.missing_scopes?.length) return 'Бракує прав доступу';
    if (val.ready === false) return 'Не готово';
    return val.ready === true ? 'Готово' : 'Налаштовано; готовність не підтверджено';
  }

  publisherChannels(): { key: string; label: string; val: { configured: boolean; ready?: boolean; missing_scopes?: string[] } }[] {
    const pub = this.integrations()?.publisher;
    if (!pub) { return []; }
    const labels: Record<string, string> = {
      youtube: 'YouTube', tiktok: 'TikTok', facebook: 'Facebook',
      instagram: 'Instagram', threads: 'Threads', telegram: 'Telegram — публікація',
    };
    return Object.entries(pub).map(([key, val]) => ({
      key,
      label: labels[key] || key,
      val: val ?? { configured: false, ready: false },
    }));
  }
}
