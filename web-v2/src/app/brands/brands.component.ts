import { Component, OnInit, signal, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterModule } from '@angular/router';
import { VertepApiService } from '../core/api.service';
import { ToastService } from '../core/services/toast.service';
import { ConfirmService } from '../core/services/confirm.service';
import { Brand, Channel } from '../core/models';
import { BrandChannelsComponent } from './brand-channels.component';

@Component({
  selector: 'app-brands',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterModule, BrandChannelsComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="space-y-5" data-testid="brands-page">
      <div class="flex items-center justify-between">
        <h2 class="text-xl font-semibold text-slate-900">Бренди</h2>
        <button (click)="openEditor()" data-testid="create-brand-button"
                class="px-4 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 text-sm font-medium">
          Новий бренд
        </button>
      </div>

      @if (loading()) {
        <div class="animate-pulse space-y-3">
          <div class="h-5 bg-slate-200 rounded w-full"></div>
          <div class="h-5 bg-slate-200 rounded w-3/4"></div>
        </div>
      } @else if (error()) {
        <div class="bg-red-50 border border-red-200 rounded-xl p-4">
          <p class="text-red-700">{{ error() }}</p>
          <button (click)="loadBrands()" class="mt-2 text-sm text-red-600 hover:text-red-700 font-medium">Повторити</button>
        </div>
      } @else if (brands().length === 0) {
        <div class="text-center text-slate-500 py-10" data-testid="brands-empty">Брендів не знайдено</div>
      } @else {
        <div class="space-y-4">
          @for (brand of brands(); track brand.id) {
            <div class="bg-white rounded-xl border border-slate-200 p-5">
              <div class="flex items-center justify-between mb-3">
                <div>
                  <h3 class="text-lg font-semibold text-slate-900">{{ brand.name }}</h3>
                  <p class="text-xs text-slate-500 font-mono">{{ brand.id }}</p>
                </div>
                <div class="flex gap-2">
                  <button (click)="editBrand(brand)" class="text-sm text-blue-600 hover:text-blue-700 font-medium">Редагувати</button>
                  <button (click)="deleteBrand(brand)" class="text-sm text-red-600 hover:text-red-700 font-medium">Видалити</button>
                </div>
              </div>
              <div class="text-sm text-slate-600 mb-3">{{ brand.enabled ? 'Активний' : 'Неактивний' }}</div>
              <app-brand-channels [brandId]="brand.id" [channels]="brandChannels[brand.id] || []" (channelAdded)="onChannelAdded($event)" (channelChanged)="loadChannels(brand.id)" />
            </div>
          }
        </div>
      }

      <!-- Brand Editor Modal -->
      <div *ngIf="showEditor()" data-testid="brand-editor-modal" class="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
        <div class="bg-white rounded-xl p-6 w-full max-w-xl mx-4 max-h-[90vh] overflow-y-auto">
          <h3 class="text-lg font-semibold text-slate-900 mb-4">{{ editingBrand() ? 'Редагувати бренд' : 'Новий бренд' }}</h3>
          <div class="space-y-4">
            <div>
              <label class="block text-sm font-medium text-slate-700 mb-1">ID</label>
              <input [(ngModel)]="editorForm.id" [disabled]="!!editingBrand()" placeholder="наприклад, my_brand" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-sm disabled:bg-slate-100">
            </div>
            <div>
              <label class="block text-sm font-medium text-slate-700 mb-1">Назва</label>
              <input [(ngModel)]="editorForm.name" placeholder="Назва бренду" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-sm">
            </div>
            <div class="flex items-center gap-2">
              <input type="checkbox" [(ngModel)]="editorForm.enabled" id="brand-enabled">
              <label for="brand-enabled" class="text-sm text-slate-700">Бренд активний</label>
            </div>
            <div>
              <label class="block text-sm font-medium text-slate-700 mb-1">Метадані (JSON)</label>
              <textarea [(ngModel)]="metadataJson" rows="4" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-xs font-mono" spellcheck="false"></textarea>
            </div>
            <div>
              <label class="block text-sm font-medium text-slate-700 mb-1">Налаштування публікації (JSON)</label>
              <textarea [(ngModel)]="publishingJson" rows="4" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-xs font-mono" spellcheck="false"></textarea>
            </div>
          </div>
          <div class="flex justify-end gap-2 mt-6">
            <button (click)="closeEditor()" class="px-4 py-2 text-slate-600 hover:text-slate-800 text-sm font-medium">Скасувати</button>
            <button (click)="saveBrand()" [disabled]="saving()" class="px-4 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 text-sm font-medium disabled:opacity-50">{{ saving() ? 'Збереження...' : 'Зберегти' }}</button>
          </div>
        </div>
      </div>
    </div>
  `,
})
export class BrandsComponent implements OnInit {
  brands = signal<Brand[]>([]);
  loading = signal(false);
  error = signal<string | null>(null);
  saving = signal(false);
  showEditor = signal(false);
  editorForm: { id: string; name: string; enabled: boolean } = { id: '', name: '', enabled: true };
  metadataJson = '';
  publishingJson = '';
  brandChannels: Record<string, Channel[]> = {};
  editingBrand = signal<Brand | null>(null);

  constructor(
    private api: VertepApiService,
    private toast: ToastService,
    private confirm: ConfirmService,
  ) {}

  ngOnInit(): void {
    this.loadBrands();
  }

  loadBrands(): void {
    this.loading.set(true);
    this.error.set(null);
    this.api.getBrands().subscribe({
      next: (brands) => {
        this.brands.set(brands);
        brands.forEach(b => this.loadChannels(b.id));
        this.loading.set(false);
      },
      error: (err) => { this.error.set(err.message); this.loading.set(false); },
    });
  }

  loadChannels(brandId: string): void {
    this.api.getBrandChannels(brandId).subscribe({
      next: (channels) => { this.brandChannels[brandId] = channels; },
      error: () => { this.brandChannels[brandId] = []; },
    });
  }

  openEditor(): void {
    this.editingBrand.set(null);
    this.editorForm = { id: '', name: '', enabled: true };
    this.metadataJson = '{}';
    this.publishingJson = '{}';
    this.showEditor.set(true);
  }

  editBrand(brand: Brand): void {
    this.editingBrand.set(brand);
    this.editorForm = { id: brand.id, name: brand.name, enabled: brand.enabled };
    this.metadataJson = JSON.stringify(brand.metadata || {}, null, 2);
    this.publishingJson = JSON.stringify(brand.publishing || {}, null, 2);
    this.showEditor.set(true);
  }

  closeEditor(): void {
    this.showEditor.set(false);
    this.editingBrand.set(null);
    this.editorForm = { id: '', name: '', enabled: true };
    this.saving.set(false);
  }

  saveBrand(): void {
    if (!this.editorForm.id || !this.editorForm.name) {
      this.toast.show('ID та назва є обов\'язковими', 'error');
      return;
    }
    this.saving.set(true);
    const payload = {
      id: this.editorForm.id,
      name: this.editorForm.name,
      enabled: this.editorForm.enabled,
      metadata: this.parseJson(this.metadataJson),
      publishing: this.parseJson(this.publishingJson),
    };

    if (this.editingBrand()) {
      this.api.updateBrand(this.editorForm.id, payload).subscribe({
        next: () => {
          this.closeEditor();
          this.loadBrands();
          this.saving.set(false);
          this.toast.show('Бренд збережено', 'success');
        },
        error: (err) => { this.saving.set(false); this.toast.show(err.message || 'Помилка збереження', 'error'); },
      });
    } else {
      this.api.createBrand(payload).subscribe({
        next: () => {
          this.closeEditor();
          this.loadBrands();
          this.saving.set(false);
          this.toast.show('Бренд створено', 'success');
        },
        error: (err) => { this.saving.set(false); this.toast.show(err.message || 'Помилка створення', 'error'); },
      });
    }
  }

  deleteBrand(brand: Brand): void {
    this.confirm.confirm({ title: 'Видалити бренд', message: `Ви впевнені, що хочете видалити бренд ${brand.name} (${brand.id})?` }).subscribe((ok) => {
      if (!ok) return;
      this.api.deleteBrand(brand.id).subscribe({
        next: () => { this.loadBrands(); this.toast.show('Бренд видалено', 'success'); },
        error: (err) => this.toast.show(err.message || 'Помилка видалення', 'error'),
      });
    });
  }

  onChannelAdded(channel: Channel): void {
    const brandId = channel.brand_id;
    this.brandChannels[brandId] = [...(this.brandChannels[brandId] || []), channel];
  }

  parseJson(text: string): Record<string, unknown> {
    try {
      const parsed = JSON.parse(text);
      return typeof parsed === 'object' && parsed !== null ? parsed : {};
    } catch {
      return {};
    }
  }
}
