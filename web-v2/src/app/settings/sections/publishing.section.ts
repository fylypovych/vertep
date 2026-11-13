import { TPipe } from '../../core/i18n/t.pipe';
import { Component, OnInit, computed, signal } from '@angular/core';
import { TimeoutError, timeout } from 'rxjs';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
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
  imports: [TPipe, CommonModule, FormsModule, SecretGroupsComponent],
  template: `<div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-publishing">
    <h3 class="text-lg font-semibold mb-1">{{ 'Публікація' | t }}</h3>
    <p class="text-sm text-slate-500 mb-4">{{ 'Параметри каналів і облікові дані платформ для публікації відео.' | t }}</p>
    <div *ngIf="error()" class="text-red-600 text-sm mb-2" data-testid="publishing-error">{{ error()! | t }}</div>
    <label class="block text-sm font-medium text-slate-700 mb-1" for="publishing-public-url">{{ 'Публічна адреса Vertep' | t }}</label>
    <input id="publishing-public-url" data-testid="publishing-public-url" [ngModel]="publicUrl()" (ngModelChange)="publicUrl.set($event)" [disabled]="!canEdit()"
           placeholder="https://vertep.example.com" class="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm">
    <p class="text-xs text-slate-500 mt-1 mb-4">{{ 'Потрібна для Threads: платформа сама завантажує відео за посиланням, тому адреса має відкриватися з інтернету. Внутрішня адреса не підійде.' | t }}</p>
    <label class="block text-sm font-medium text-slate-700 mb-1" for="publishing-telegram-channel">{{ 'Канал Telegram' | t }}</label>
    <input id="publishing-telegram-channel" data-testid="publishing-telegram-channel" [ngModel]="telegramChannel()" (ngModelChange)="telegramChannel.set($event)" [disabled]="!canEdit()"
           placeholder="{{ '@назва_каналу' | t }}" class="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm">
    <p class="text-xs text-slate-500 mt-1 mb-4">{{ 'Бот має бути адміністратором цього каналу.' | t }}</p>
    <button class="px-3 py-1 bg-emerald-600 text-white rounded text-sm disabled:opacity-50" data-testid="publishing-save"
            [disabled]="!canEdit() || saving()" (click)="save()">{{ (saving() ? 'Збереження...' : 'Зберегти') | t }}</button>
    <h4 class="text-base font-semibold mt-6 mb-1">{{ 'Облікові дані платформ' | t }}</h4>
    <p class="text-sm text-slate-500 mb-3">{{ 'Значення зберігаються в захищеному сховищі й після збереження не показуються.' | t }}</p>
    <app-secret-groups [groups]="platformGroups" data-testid="publishing-credentials" />
  </div>`,
})
export class PublishingSectionComponent implements OnInit {
  publicUrl = signal('');
  telegramChannel = signal('');
  saving = signal(false);
  error = signal<string | null>(null);
  readonly canEdit = computed(() => this.policy.can('update_settings').allowed);

  /** Every social publishing credential the store knows; Telegram shares the bot token with communication. */
  readonly platformGroups: SecretGroup[] = [
    { id: 'telegram-publishing', label: 'Telegram', names: [],
      shared: { name: 'telegram_bot_token', tab: 'secrets', tabLabel: 'Ключі інтеграцій',
                note: 'Публікація в канал використовує той самий токен, що й Telegram-бот керування; він зберігається в одному місці.' } },
    { id: 'youtube', label: 'YouTube', names: ['youtube_client_id', 'youtube_client_secret', 'youtube_access_token', 'youtube_refresh_token'] },
    { id: 'facebook', label: 'Facebook', names: ['facebook_access_token'] },
    { id: 'instagram', label: 'Instagram', names: ['instagram_client_id', 'instagram_client_secret', 'instagram_access_token', 'instagram_refresh_token'] },
    { id: 'threads', label: 'Threads', names: ['threads_client_id', 'threads_client_secret', 'threads_access_token', 'threads_refresh_token'],
      hint: 'Також потрібна публічна адреса Vertep вище.' },
    { id: 'tiktok', label: 'TikTok', names: ['tiktok_client_id', 'tiktok_client_secret', 'tiktok_access_token', 'tiktok_refresh_token'] },
  ];

  constructor(private settingsApi: SettingsApiService, private toast: ToastService, private policy: PolicyService) {}

  ngOnInit(): void {
    this.settingsApi.publishingSettings().subscribe({
      next: (value) => { this.publicUrl.set(value.public_url); this.telegramChannel.set(value.telegram_channel_id); },
      error: (err) => this.error.set(err.message),
    });
  }

  save(): void {
    if (this.saving()) return;
    this.saving.set(true);
    this.error.set(null);
    this.settingsApi.savePublishingSettings({ public_url: this.publicUrl(), telegram_channel_id: this.telegramChannel() })
      // A hung save ends with a local, honest state instead of an endless "saving" (#464).
      // timeout() unsubscribes, which cancels the request: a late answer cannot replace the form.
      .pipe(timeout(PUBLISHING_SAVE_TIMEOUT_MS)).subscribe({
      next: (value) => {
        this.saving.set(false);
        this.publicUrl.set(value.public_url);
        this.telegramChannel.set(value.telegram_channel_id);
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
