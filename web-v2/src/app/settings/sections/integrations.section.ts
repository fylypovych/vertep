import { TPipe } from '../../core/i18n/t.pipe';
import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { SettingsApiService } from '../../core/api/settings.api';
import { IntegrationStatus } from '../../core/models';
import { LoadingStateComponent } from '../../shared/loading-state.component';
import { ErrorStateComponent } from '../../shared/error-state.component';

type Tone = 'green' | 'yellow' | 'red';

@Component({
  selector: 'app-settings-integrations',
  standalone: true,
  imports: [TPipe, CommonModule, LoadingStateComponent, ErrorStateComponent],
  template: `<div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-integrations">
    <h3 class="text-lg font-semibold mb-4">{{ 'Стан підключень' | t }}</h3>
    <p class="text-sm text-slate-600 mb-4">{{ 'Ключі й паролі змінюються в розділі «Секрети»; збережений ключ сам по собі не підтверджує готовність сервісу.' | t }}</p>
    <p class="text-sm text-slate-600 mb-4">{{ 'Telegram нижче — публікація повідомлень, а не стан бота керування.' | t }}</p>
    <button type="button" (click)="ngOnInit()" [disabled]="loading()" data-testid="publisher-test" class="px-3 py-2 border rounded mb-3">{{ 'Перевірити підключення' | t }}</button>
    <app-loading-state *ngIf="loading()" /><app-error-state *ngIf="error()" [message]="error()!" />
    <div *ngIf="!loading() && !error()" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
      <div *ngFor="let svc of services()" class="flex justify-between border rounded-xl p-4" [ngClass]="toneClass(serviceTone(svc.val))" [attr.data-tone]="serviceTone(svc.val)" [attr.data-testid]="'service-status-' + svc.key"><span class="font-semibold">{{ svc.label }}</span><span>{{ serviceStatus(svc.val) | t }}</span></div>
      <div *ngFor="let ch of publisherChannels()" class="border rounded-xl p-4" [ngClass]="toneClass(publisherTone(ch.val))" [attr.data-tone]="publisherTone(ch.val)" [attr.data-testid]="'publisher-status-' + ch.key">
        <div class="flex flex-wrap justify-between items-center gap-3"><span class="font-semibold">{{ (ch.label) | t }}</span><span>{{ (publisherStatus(ch.val)) | t }}</span>
          <button *ngIf="ch.key !== 'telegram'" type="button" [disabled]="busy() !== null" (click)="reconnect(ch.key)" [attr.data-testid]="'publisher-reconnect-' + ch.key" class="px-3 py-2 border rounded">{{ 'Перепідключити' | t }}</button>
        </div>
        <p *ngIf="messages()[ch.key]" role="status" class="text-sm">{{ (messages()[ch.key]) | t }}</p>
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

  services(): { key: string; label: string; val?: { status?: string; http_status?: number } }[] {
    return [{ key: 'ollama', label: 'Ollama', val: this.integrations()?.ollama },
            { key: 'comfyui', label: 'ComfyUI', val: this.integrations()?.comfyui }];
  }

  /** Unreachable is red; an answer with an HTTP error is a problem (yellow); a clean answer is green. */
  serviceTone(val?: { status?: string; http_status?: number }): Tone {
    if (val?.status !== 'ONLINE') return 'red';
    return (val.http_status ?? 200) >= 400 ? 'yellow' : 'green';
  }

  serviceStatus(val?: { status?: string; http_status?: number }): string {
    const tone = this.serviceTone(val);
    return tone === 'red' ? 'OFFLINE' : tone === 'yellow' ? 'Відповідає з помилкою' : 'ONLINE';
  }

  /** Not configured is red; confirmed ready is green; anything in between is a problem (yellow). */
  publisherTone(val: { configured: boolean; ready?: boolean; missing_scopes?: string[] }): Tone {
    if (!val?.configured) return 'red';
    return val.ready === true && !val.missing_scopes?.length ? 'green' : 'yellow';
  }

  toneClass(tone: Tone): string {
    return { green: 'bg-emerald-50 border-emerald-300', yellow: 'bg-amber-50 border-amber-300', red: 'bg-red-50 border-red-300' }[tone];
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
