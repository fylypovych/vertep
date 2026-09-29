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
  template: `<div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-integrations"><h3 class="text-lg font-semibold mb-4">Стан підключень</h3><p class="text-sm text-slate-600 mb-4">Огляд доступності сервісів генерації та налаштування публікації. Ключі й паролі змінюються в розділі «Секрети»; збережений ключ сам по собі не підтверджує готовність сервісу.</p><p class="text-sm text-slate-600 mb-4">Telegram нижче — публікація повідомлень, а не стан бота керування.</p><app-loading-state *ngIf="loading()" /><app-error-state *ngIf="error()" [message]="error()!" /><div *ngIf="!loading() && !error()" class="space-y-2"><div class="flex justify-between"><span>Ollama</span><span>{{ integrations()?.ollama?.status || 'OFFLINE' }}</span></div><div class="flex justify-between"><span>ComfyUI</span><span>{{ integrations()?.comfyui?.status || 'OFFLINE' }}</span></div><div *ngFor="let ch of publisherChannels()" class="flex justify-between"><span>{{ ch.label }}</span><span>{{ publisherStatus(ch.val) }}</span></div></div></div>`,
})
export class IntegrationsSectionComponent implements OnInit {
  integrations = signal<IntegrationStatus | null>(null);
  loading = signal(false);
  error = signal<string | null>(null);

  constructor(private settings: SettingsApiService) {}

  ngOnInit(): void {
    this.loading.set(true);
    this.settings.integrations().subscribe({
      next: (data) => { this.integrations.set(data as unknown as IntegrationStatus); this.loading.set(false); },
      error: (err) => { this.error.set(err.message); this.loading.set(false); },
    });
  }

  publisherStatus(val: { configured: boolean; ready?: boolean; missing_scopes?: string[] }): string {
    if (!val?.configured) return 'Не налаштовано';
    if (val.missing_scopes?.length) return 'Бракує прав доступу';
    if (val.ready === false) return 'Не готово';
    return val.ready === true ? 'Готово' : 'Налаштовано; готовність не підтверджено';
  }

  publisherChannels(): { label: string; val: { configured: boolean; ready?: boolean; missing_scopes?: string[] } }[] {
    const pub = this.integrations()?.publisher;
    if (!pub) { return []; }
    const labels: Record<string, string> = {
      youtube: 'YouTube', tiktok: 'TikTok', facebook: 'Facebook',
      instagram: 'Instagram', threads: 'Threads', telegram: 'Telegram — публікація',
    };
    return Object.entries(pub).map(([key, val]) => ({
      label: labels[key] || key,
      val: val ?? { configured: false, ready: false },
    }));
  }
}
