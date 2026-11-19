import { TPipe } from '../core/i18n/t.pipe';
import { Component, OnInit, Input, Output, EventEmitter, ChangeDetectionStrategy, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { BaseApiService } from '../core/api/base-api.service';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ResourcesApiService } from '../core/api/resources.api';
import { ToastService } from '../core/services/toast.service';
import { ConfirmService } from '../core/services/confirm.service';
import { PolicyService } from '../core/services/policy.service';
import { Channel } from '../core/models';
import { LoadingStateComponent } from '../shared/loading-state.component';
import { ErrorStateComponent } from '../shared/error-state.component';

interface ChannelCredentials { channel_id: string; mode: 'own' | 'global' | 'missing'; configured: string[]; use_global_credentials: boolean; warning?: string; }

@Component({
  selector: 'app-brand-channels',
  standalone: true,
  imports: [TPipe, CommonModule, FormsModule, LoadingStateComponent, ErrorStateComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="mt-3 pt-3 border-t border-slate-100" [attr.data-testid]="'brand-channels-' + brandId">
      <div class="flex items-center justify-between mb-2">
        <h4 class="text-xs font-medium text-slate-500">{{ 'Канали бренду' | t }}</h4>
        <button (click)="showCreate = true" [disabled]="!canCreateChannel" [title]="(createChannelReason || 'Додати канал') | t" class="text-xs text-emerald-600 hover:text-emerald-700 font-medium disabled:opacity-40">{{ 'Додати канал' | t }}</button>
      </div>
      @if (loading) {
        <app-loading-state />
      } @else if (error) {
        <app-error-state [message]="error" (retry)="retry.emit()" />
      } @else if (channels.length === 0) {
        <p class="text-xs text-slate-400">{{ 'Каналів не налаштовано' | t }}</p>
      } @else {
        <div class="space-y-1">
          @for (ch of channels; track ch.channel_id) {
            <div class="flex items-center justify-between text-sm py-1">
              <span>{{ ch.channel_type }}: {{ ch.target }}</span>
              <div class="flex items-center gap-2">
                <span class="text-xs" [class.text-emerald-600]="ch.enabled" [class.text-slate-400]="!ch.enabled">
                  {{ (ch.enabled ? 'Активний' : 'Неактивний') | t }}
                </span>
                <button (click)="toggleChannel(ch)" [disabled]="!canUpdateChannel" [title]="(updateChannelReason || (ch.enabled ? 'Вимкнути' : 'Увімкнути')) | t" class="text-xs text-blue-600 hover:text-blue-700 disabled:opacity-40" data-testid="channel-toggle">{{ (ch.enabled ? 'Вимкнути' : 'Увімкнути') | t }}</button>
                <button (click)="deleteChannel(ch)" [disabled]="!canDeleteChannel" [title]="(deleteChannelReason || 'Видалити') | t" class="text-xs text-red-600 hover:text-red-700 disabled:opacity-40" data-testid="channel-delete">{{ 'Видалити' | t }}</button>
                <button (click)="openCredentials(ch)" [disabled]="!canUpdateChannel" class="text-xs text-indigo-700 disabled:opacity-40" [attr.data-testid]="'channel-credentials-' + ch.channel_id">{{ 'Облікові дані' | t }}</button>
              </div>
            </div>
            @if (credentialsFor() === ch.channel_id) {
              <div class="text-xs bg-slate-50 rounded p-2 space-y-2" [attr.data-testid]="'channel-credentials-panel-' + ch.channel_id">
                <div [attr.data-testid]="'channel-credentials-state-' + ch.channel_id">{{ credentialsText() | t }}</div>
                @if (credentials()?.warning) { <div class="text-amber-700">{{ credentials()!.warning! | t }}</div> }
                <div class="flex gap-2">
                  <input type="password" class="border rounded px-2 py-1 grow" placeholder="{{ 'Токен каналу' | t }}" [(ngModel)]="channelToken" [attr.data-testid]="'channel-token-' + ch.channel_id">
                  <button class="px-2 py-1 bg-emerald-600 text-white rounded" (click)="saveToken(ch)" [attr.data-testid]="'channel-token-save-' + ch.channel_id">{{ 'Зберегти токен' | t }}</button>
                </div>
                <label class="inline-flex items-center gap-1">
                  <input type="checkbox" [checked]="credentials()?.use_global_credentials" (change)="toggleGlobal(ch, $any($event.target).checked)" [attr.data-testid]="'channel-global-' + ch.channel_id">
                  {{ 'Дозволити глобальні облікові дані платформи (тимчасово)' | t }}
                </label>
              </div>
            }
          }
        </div>
      }

      <div *ngIf="showCreate" class="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
        <div class="bg-white rounded-xl p-6 w-full max-w-md mx-4">
          <h4 class="text-lg font-semibold text-slate-900 mb-4">{{ 'Новий канал' | t }}</h4>
          <div class="space-y-4">
            <div>
              <label class="block text-sm font-medium text-slate-700 mb-1">{{ 'Тип каналу' | t }}</label>
              <select [(ngModel)]="newChannel.channel_type" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-sm">
                @for (type of channelTypes; track type) {
                  <option [value]="type">{{ type }}</option>
                }
              </select>
            </div>
            <div>
              <label class="block text-sm font-medium text-slate-700 mb-1">{{ 'Ціль' | t }}</label>
              <input [(ngModel)]="newChannel.target" placeholder="{{ 'наприклад, @mychannel або channel_id' | t }}" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-sm">
            </div>
          </div>
          <div class="flex justify-end gap-2 mt-6">
            <button (click)="showCreate = false" class="px-4 py-2 text-slate-600 hover:text-slate-800 text-sm font-medium">{{ 'Скасувати' | t }}</button>
            <button (click)="createChannel()" [disabled]="!canCreateChannel" [title]="(createChannelReason || 'Створити') | t" class="px-4 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 text-sm font-medium disabled:opacity-50">{{ 'Створити' | t }}</button>
          </div>
        </div>
      </div>
    </div>
  `,
})
export class BrandChannelsComponent implements OnInit {
  /** #502: one credential set per channel; values are never shown. */
  credentialsFor = signal<string | null>(null);
  credentials = signal<ChannelCredentials | null>(null);
  channelToken = '';

  private credentialName(type: string): string {
    return ({ telegram: 'telegram_publisher_bot_token', youtube: 'youtube_access_token', tiktok: 'tiktok_access_token',
              facebook: 'facebook_access_token', instagram: 'instagram_access_token', threads: 'threads_access_token' } as Record<string, string>)[type] || `${type}_access_token`;
  }

  credentialsText(): string {
    const mode = this.credentials()?.mode;
    return mode === 'own' ? 'Власний токен каналу задано' : mode === 'global' ? 'Використовуються глобальні облікові дані платформи' : 'Облікових даних немає — публікацію заблоковано';
  }

  openCredentials(ch: Channel): void {
    this.channelToken = '';
    this.credentialsFor.set(ch.channel_id);
    this.http.get<ChannelCredentials>(`${this.base.url}/channels/${encodeURIComponent(ch.channel_id)}/credentials`, { headers: this.base.headers() })
      .subscribe({ next: (value) => this.credentials.set(value), error: (error) => this.toast.show(error?.message || 'Помилка', 'error') });
  }

  private putCredentials(ch: Channel, body: Record<string, unknown>): void {
    this.http.put<ChannelCredentials>(`${this.base.url}/channels/${encodeURIComponent(ch.channel_id)}/credentials`, body, { headers: this.base.headers() })
      .subscribe({ next: (value) => { this.credentials.set(value); this.channelToken = ''; },
                   error: (error) => this.toast.show(error?.error?.detail || error?.message || 'Помилка', 'error') });
  }

  saveToken(ch: Channel): void {
    if (!this.channelToken.trim()) return;
    this.putCredentials(ch, { secrets: { [this.credentialName(ch.channel_type)]: this.channelToken.trim() } });
  }

  toggleGlobal(ch: Channel, allowed: boolean): void {
    this.putCredentials(ch, { use_global_credentials: allowed });
  }

  @Input() brandId = '';
  @Input() channels: Channel[] = [];
  @Input() loading = false;
  @Input() error: string | null = null;
  @Output() retry = new EventEmitter<void>();
  @Output() channelAdded = new EventEmitter<Channel>();
  @Output() channelChanged = new EventEmitter<void>();
  showCreate = false;
  newChannel: { channel_type: string; target: string } = { channel_type: '', target: '' };
  channelTypes: string[] = [];

  constructor(
    private resources: ResourcesApiService,
    private toast: ToastService,
    private confirm: ConfirmService,
    public policy: PolicyService,
    private http: HttpClient,
    private base: BaseApiService,
  ) {}

  get canCreateChannel(): boolean { return this.policy.can('create_channel').allowed; }
  get createChannelReason(): string | null { return this.policy.disabledReason('create_channel'); }
  get canUpdateChannel(): boolean { return this.policy.can('update_channel').allowed; }
  get updateChannelReason(): string | null { return this.policy.disabledReason('update_channel'); }
  get canDeleteChannel(): boolean { return this.policy.can('delete_channel').allowed; }
  get deleteChannelReason(): string | null { return this.policy.disabledReason('delete_channel'); }

  ngOnInit(): void {
    this.resources.channelTypes().subscribe({ next: (types) => this.channelTypes = types });
  }

  createChannel(): void {
    if (!this.canCreateChannel) return;
    if (!this.newChannel.channel_type || !this.newChannel.target) {
      this.toast.show('Тип і ціль є обов\'язковими', 'error');
      return;
    }
    this.resources.createChannel(this.brandId, {
      brand_id: this.brandId,
      channel_type: this.newChannel.channel_type,
      target: this.newChannel.target,
      enabled: true,
      metadata: {},
    }).subscribe({
      next: (ch) => {
        this.channelAdded.emit(ch);
        this.showCreate = false;
        this.newChannel = { channel_type: '', target: '' };
        this.toast.show('Канал створено', 'success');
      },
      error: (err) => this.toast.show(err.message || 'Помилка створення каналу', 'error'),
    });
  }

  toggleChannel(ch: Channel): void {
    if (!this.canUpdateChannel) return;
    this.resources.updateChannel(ch.channel_id, { enabled: !ch.enabled }).subscribe({
      next: () => { this.channelChanged.emit(); this.toast.show('Канал оновлено', 'success'); },
      error: (err) => this.toast.show(err.message || 'Помилка оновлення', 'error'),
    });
  }

  deleteChannel(ch: Channel): void {
    if (!this.canDeleteChannel) return;
    this.confirm.confirm({ title: 'Видалити канал', message: `Видалити ${ch.channel_type}:${ch.target}?` }).subscribe((ok) => {
      if (!ok) return;
      this.resources.deleteChannel(ch.channel_id).subscribe({
        next: () => { this.channelChanged.emit(); this.toast.show('Канал видалено', 'success'); },
        error: (err) => this.toast.show(err.message || 'Помилка видалення', 'error'),
      });
    });
  }
}
