import { Component, OnInit, computed, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { SettingsApiService } from '../../core/api/settings.api';
import { ToastService } from '../../core/services/toast.service';
import { PolicyService } from '../../core/services/policy.service';

/** Settings → Публікація: parameters that publishing channels need from the owner. */
@Component({
  selector: 'app-settings-publishing',
  standalone: true,
  imports: [CommonModule, FormsModule],
  template: `<div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-publishing">
    <h3 class="text-lg font-semibold mb-1">Публікація</h3>
    <p class="text-sm text-slate-500 mb-4">Токени платформ задаються в розділі «Секрети».</p>
    <div *ngIf="error()" class="text-red-600 text-sm mb-2" data-testid="publishing-error">{{ error() }}</div>
    <label class="block text-sm font-medium text-slate-700 mb-1" for="publishing-public-url">Публічна адреса Vertep</label>
    <input id="publishing-public-url" data-testid="publishing-public-url" [ngModel]="publicUrl()" (ngModelChange)="publicUrl.set($event)" [disabled]="!canEdit()"
           placeholder="https://vertep.example.com" class="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm">
    <p class="text-xs text-slate-500 mt-1 mb-4">Потрібна для Threads: платформа сама завантажує відео за посиланням, тому адреса має відкриватися з інтернету. Внутрішня адреса не підійде.</p>
    <label class="block text-sm font-medium text-slate-700 mb-1" for="publishing-telegram-channel">Канал Telegram</label>
    <input id="publishing-telegram-channel" data-testid="publishing-telegram-channel" [ngModel]="telegramChannel()" (ngModelChange)="telegramChannel.set($event)" [disabled]="!canEdit()"
           placeholder="@назва_каналу" class="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm">
    <p class="text-xs text-slate-500 mt-1 mb-4">Бот має бути адміністратором цього каналу.</p>
    <button class="px-3 py-1 bg-emerald-600 text-white rounded text-sm disabled:opacity-50" data-testid="publishing-save"
            [disabled]="!canEdit() || saving()" (click)="save()">Зберегти</button>
  </div>`,
})
export class PublishingSectionComponent implements OnInit {
  publicUrl = signal('');
  telegramChannel = signal('');
  saving = signal(false);
  error = signal<string | null>(null);
  readonly canEdit = computed(() => this.policy.can('update_settings').allowed);

  constructor(private settingsApi: SettingsApiService, private toast: ToastService, private policy: PolicyService) {}

  ngOnInit(): void {
    this.settingsApi.publishingSettings().subscribe({
      next: (value) => { this.publicUrl.set(value.public_url); this.telegramChannel.set(value.telegram_channel_id); },
      error: (err) => this.error.set(err.message),
    });
  }

  save(): void {
    this.saving.set(true);
    this.error.set(null);
    this.settingsApi.savePublishingSettings({ public_url: this.publicUrl(), telegram_channel_id: this.telegramChannel() }).subscribe({
      next: (value) => {
        this.saving.set(false);
        this.publicUrl.set(value.public_url);
        this.telegramChannel.set(value.telegram_channel_id);
        this.toast.show('Налаштування публікації збережено', 'success');
      },
      error: (err) => { this.saving.set(false); this.error.set(err.message); },
    });
  }
}
