import { Component, OnInit, Input, Output, EventEmitter, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { VertepApiService } from '../core/api.service';
import { ToastService } from '../core/services/toast.service';
import { Channel } from '../core/models';

@Component({
  selector: 'app-brand-channels',
  standalone: true,
  imports: [CommonModule, FormsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="mt-3 pt-3 border-t border-slate-100">
      <div class="flex items-center justify-between mb-2">
        <h4 class="text-xs font-medium text-slate-500">Канали бренду</h4>
        <button (click)="showCreate = true" class="text-xs text-emerald-600 hover:text-emerald-700 font-medium">Додати канал</button>
      </div>
      @if (channels.length === 0) {
        <p class="text-xs text-slate-400">Каналів не налаштовано</p>
      } @else {
        <div class="space-y-1">
          @for (ch of channels; track ch.channel_id) {
            <div class="flex items-center justify-between text-sm">
              <span>{{ ch.channel_type }}: {{ ch.target }}</span>
              <span class="text-xs" [class.text-emerald-600]="ch.enabled" [class.text-slate-400]="!ch.enabled">
                {{ ch.enabled ? 'Активний' : 'Неактивний' }}
              </span>
            </div>
          }
        </div>
      }

      <div *ngIf="showCreate" class="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
        <div class="bg-white rounded-xl p-6 w-full max-w-md mx-4">
          <h4 class="text-lg font-semibold text-slate-900 mb-4">Новий канал</h4>
          <div class="space-y-4">
            <div>
              <label class="block text-sm font-medium text-slate-700 mb-1">Тип каналу</label>
              <select [(ngModel)]="newChannel.channel_type" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-sm">
                @for (type of channelTypes; track type) {
                  <option [value]="type">{{ type }}</option>
                }
              </select>
            </div>
            <div>
              <label class="block text-sm font-medium text-slate-700 mb-1">Ціль</label>
              <input [(ngModel)]="newChannel.target" placeholder="наприклад, @mychannel або channel_id" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-sm">
            </div>
          </div>
          <div class="flex justify-end gap-2 mt-6">
            <button (click)="showCreate = false" class="px-4 py-2 text-slate-600 hover:text-slate-800 text-sm font-medium">Скасувати</button>
            <button (click)="createChannel()" class="px-4 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 text-sm font-medium">Створити</button>
          </div>
        </div>
      </div>
    </div>
  `,
})
export class BrandChannelsComponent implements OnInit {
  @Input() brandId = '';
  @Input() channels: Channel[] = [];
  @Output() channelAdded = new EventEmitter<Channel>();
  showCreate = false;
  newChannel: { channel_type: string; target: string } = { channel_type: '', target: '' };
  channelTypes: string[] = [];

  constructor(private api: VertepApiService, private toast: ToastService) {}

  ngOnInit(): void {
    this.api.getChannelTypes().subscribe({ next: (types) => this.channelTypes = types });
  }

  createChannel(): void {
    if (!this.newChannel.channel_type || !this.newChannel.target) {
      this.toast.show('Тип і ціль є обов\'язковими', 'error');
      return;
    }
    this.api.createChannel(this.brandId, {
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
}
