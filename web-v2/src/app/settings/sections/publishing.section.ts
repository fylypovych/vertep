import { TPipe } from '../../core/i18n/t.pipe';
import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { TimeoutError, timeout } from 'rxjs';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';
import { IntegrationStatus } from '../../core/models';
import { SettingsApiService } from '../../core/api/settings.api';
import { ToastService } from '../../core/services/toast.service';
import { PolicyService } from '../../core/services/policy.service';
import { SecretGroup, SecretGroupsComponent } from './secret-groups.component';

/** How long an explicit save may wait for the server before the form is released (#464). */
export const PUBLISHING_SAVE_TIMEOUT_MS = 30_000;

/** Settings → Публікація: channel parameters and platform credentials, grouped by platform. */
@Component({
  selector: 'app-settings-publishing',
  standalone: true,
  imports: [TPipe, CommonModule, FormsModule, RouterLink, SecretGroupsComponent],
  template: `<div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-publishing">
    <h3 class="text-lg font-semibold mb-1">{{ 'Публікація' | t }}</h3>
    <p class="text-sm text-slate-600 mb-4" data-testid="publishing-brands-hint">{{ 'Куди публікувати, вирішують бренди: канал і його облікові дані задаються в' | t }}
      <a [routerLink]="[routePrefix + '/brands']" class="text-blue-700">«{{ 'Бренди → Канали' | t }}»</a>. {{ 'Тут — загальні параметри та спільні облікові дані.' | t }}</p>
    <div *ngIf="error()" class="text-red-600 text-sm mb-2" data-testid="publishing-error">{{ error()! | t }}</div>
    <label class="block text-sm font-medium text-slate-700 mb-1" for="publishing-public-url">{{ 'Публічна адреса Vertep' | t }}</label>
    <input id="publishing-public-url" data-testid="publishing-public-url" [ngModel]="publicUrl()" (ngModelChange)="publicUrl.set($event)" [disabled]="!canEdit()"
           placeholder="https://vertep.example.com" class="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm">
    <p class="text-xs text-slate-500 mt-1 mb-4">{{ 'Потрібна для Threads: платформа сама завантажує відео за посиланням, тому адреса має відкриватися з інтернету. Внутрішня адреса не підійде.' | t }}</p>
    <button class="px-3 py-1 bg-emerald-600 text-white rounded text-sm disabled:opacity-50" data-testid="publishing-save"
            [disabled]="!canEdit() || saving()" (click)="save()">{{ (saving() ? 'Збереження...' : 'Зберегти') | t }}</button>
    <h4 class="text-base font-semibold mt-6 mb-1">{{ 'Спільні облікові дані платформ (запасні)' | t }}</h4>
    <p class="text-sm text-slate-500 mb-3">{{ 'Канал публікує зі своїми обліковими даними. Ці значення використовуються лише для каналу, де адміністратор явно дозволив спільні облікові дані; кожне таке використання записується в аудит. Значення зберігаються в захищеному сховищі й після збереження не показуються.' | t }}</p>
    <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3 mb-4" data-testid="publishing-shared-status">
      <div *ngFor="let ch of publisherChannels()" class="border rounded-xl p-3 text-sm" [ngClass]="toneClass(publisherTone(ch.val))" [attr.data-tone]="publisherTone(ch.val)" [attr.data-testid]="'publisher-status-' + ch.key">
        <div class="flex flex-wrap justify-between items-center gap-2"><span class="font-semibold">{{ (ch.label) | t }}</span><span>{{ (publisherStatus(ch.val)) | t }}</span>
          <button *ngIf="ch.key !== 'telegram' && ch.val.configured" type="button" [disabled]="busy() !== null || !canEdit()" (click)="reconnect(ch.key)" [attr.data-testid]="'publisher-reconnect-' + ch.key" class="px-2 py-1 border rounded text-xs">{{ 'Перепідключити' | t }}</button>
        </div>
        <p *ngIf="messages()[ch.key]" role="status" class="text-xs">{{ (messages()[ch.key]) | t }}</p>
      </div>
    </div>
    <app-secret-groups [groups]="platformGroups" data-testid="publishing-credentials" />
  </div>`,
})
export class PublishingSectionComponent implements OnInit {
  private readonly router = inject(Router);
  get routePrefix(): string { return this.router.url.startsWith('/v3') ? '/v3' : ''; }
  publicUrl = signal('');
  saving = signal(false);
  error = signal<string | null>(null);
  readonly canEdit = computed(() => this.policy.can('update_settings').allowed);

  /** Every social publishing credential the store knows; Telegram publishes with its own bot (var.0.0.4.86). */
  readonly platformGroups: SecretGroup[] = [
    { id: 'telegram-publishing', label: 'Telegram', names: ['telegram_publisher_bot_token'],
      hint: 'Окремий бот лише для публікації в канали. Канал задається для бренду: Бренди → Канали; бот має бути адміністратором каналу з правом публікації.' },
    { id: 'youtube', label: 'YouTube', names: ['youtube_client_id', 'youtube_client_secret', 'youtube_access_token', 'youtube_refresh_token'] },
    { id: 'facebook', label: 'Facebook', names: ['facebook_access_token'] },
    { id: 'instagram', label: 'Instagram', names: ['instagram_client_id', 'instagram_client_secret', 'instagram_access_token', 'instagram_refresh_token'] },
    { id: 'threads', label: 'Threads', names: ['threads_client_id', 'threads_client_secret', 'threads_access_token', 'threads_refresh_token'],
      hint: 'Також потрібна публічна адреса Vertep вище.' },
    { id: 'tiktok', label: 'TikTok', names: ['tiktok_client_id', 'tiktok_client_secret', 'tiktok_access_token', 'tiktok_refresh_token'] },
  ];

  constructor(private settingsApi: SettingsApiService, private toast: ToastService, private policy: PolicyService) {}

  /** Readiness of the shared platform credentials (formerly «Стан підключень»). */
  integrations = signal<IntegrationStatus | null>(null);
  busy = signal<string | null>(null);
  messages = signal<Record<string, string>>({});

  loadStatus(): void {
    this.settingsApi.integrations().subscribe({ next: (data) => this.integrations.set(data as unknown as IntegrationStatus), error: () => {} });
  }

  reconnect(channel: string): void {
    this.busy.set(channel);
    this.settingsApi.reconnectPublisher(channel).subscribe({
      next: data => {
        this.messages.update(messages => ({ ...messages, [channel]: data.error || (data.reconnected && data.ready ? 'Підключення відновлено' : 'Підключення не готове') }));
        this.busy.set(null);
        this.loadStatus();
      },
      error: error => {
        this.messages.update(messages => ({ ...messages, [channel]: error.message }));
        this.busy.set(null);
      },
    });
  }

  /** Not configured is red; confirmed ready is green; anything in between is a problem (yellow). */
  publisherTone(val: { configured: boolean; ready?: boolean; missing_scopes?: string[] }): 'green' | 'yellow' | 'red' {
    if (!val?.configured) return 'red';
    return val.ready === true && !val.missing_scopes?.length ? 'green' : 'yellow';
  }

  toneClass(tone: 'green' | 'yellow' | 'red'): string {
    return { green: 'bg-emerald-50 border-emerald-300', yellow: 'bg-amber-50 border-amber-300', red: 'bg-slate-50 border-slate-200' }[tone];
  }

  publisherStatus(val: { configured: boolean; ready?: boolean; missing_scopes?: string[] }): string {
    if (!val?.configured) return 'Не задано';
    if (val.missing_scopes?.length) return 'Бракує прав доступу';
    if (val.ready === false) return 'Не готово';
    return val.ready === true ? 'Готово' : 'Налаштовано; готовність не підтверджено';
  }

  publisherChannels(): { key: string; label: string; val: { configured: boolean; ready?: boolean; missing_scopes?: string[] } }[] {
    const pub = this.integrations()?.publisher;
    if (!pub) { return []; }
    const labels: Record<string, string> = {
      youtube: 'YouTube', tiktok: 'TikTok', facebook: 'Facebook',
      instagram: 'Instagram', threads: 'Threads', telegram: 'Telegram',
    };
    return Object.entries(pub).map(([key, val]) => ({ key, label: labels[key] || key, val: val ?? { configured: false, ready: false } }));
  }

  ngOnInit(): void {
    this.loadStatus();
    this.settingsApi.publishingSettings().subscribe({
      next: (value) => this.publicUrl.set(value.public_url),
      error: (err) => this.error.set(err.message),
    });
  }

  save(): void {
    if (this.saving()) return;
    this.saving.set(true);
    this.error.set(null);
    this.settingsApi.savePublishingSettings({ public_url: this.publicUrl() })
      // A hung save ends with a local, honest state instead of an endless "saving" (#464).
      // timeout() unsubscribes, which cancels the request: a late answer cannot replace the form.
      .pipe(timeout(PUBLISHING_SAVE_TIMEOUT_MS)).subscribe({
      next: (value) => {
        this.saving.set(false);
        this.publicUrl.set(value.public_url);
        this.toast.show('Налаштування публікації збережено', 'success');
      },
      error: (err) => {
        this.saving.set(false);
        // The server may or may not have stored the values: say so, keep the draft, allow a retry.
        this.error.set(err instanceof TimeoutError
          ? 'Сервер не відповів вчасно. Невідомо, чи збережено зміни: введені значення лишилися у формі, можна повторити збереження.'
          : err.message);
      },
    });
  }
}
