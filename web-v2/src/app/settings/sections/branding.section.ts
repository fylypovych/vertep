import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { VertepApiService } from '../../core/api.service';
import { ToastService } from '../../core/services/toast.service';
import { ConfirmService } from '../../core/services/confirm.service';

@Component({
  selector: 'app-settings-branding',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-branding">
      <h3 class="text-lg font-semibold text-slate-900 mb-4">Логотип</h3>
      @if (logoError()) { <p role="alert" class="text-sm text-red-600 mb-3">{{ logoError() }}</p> }
      <div class="flex items-center gap-4">
        <div class="w-16 h-16 rounded-lg border border-slate-200 flex items-center justify-center overflow-hidden bg-slate-50">
          @if (logoUrl()) {
            <img [src]="logoUrl()" alt="Logo" class="max-w-full max-h-full object-contain">
          } @else {
            <span class="text-2xl text-slate-300">V</span>
          }
        </div>
        <div class="flex gap-2">
          <label class="px-3 py-1.5 text-sm bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 cursor-pointer">
            {{ uploading() ? 'Завантаження...' : 'Завантажити' }}
            <input [disabled]="uploading()" type="file" accept="image/*" (change)="uploadFile($event)" class="hidden" data-testid="logo-upload">
          </label>
          @if (logoUrl()) {
            <button (click)="deleteLogo()" class="px-3 py-1.5 text-sm text-red-600 hover:text-red-700 border border-red-200 rounded-lg" data-testid="logo-delete">Видалити</button>
          }
        </div>
      </div>
    </div>
  `,
})
export class BrandingSectionComponent implements OnInit {
  logoUrl = signal<string | null>(null);
  uploading = signal(false);
  logoError = signal<string | null>(null);

  constructor(private api: VertepApiService, private toast: ToastService, private confirm: ConfirmService) {}

  ngOnInit(): void { this.loadLogo(); }

  loadLogo(): void {
    this.api.getLogo().subscribe({
      next: (blob) => { this.logoUrl.set(URL.createObjectURL(blob)); },
      error: () => this.logoUrl.set(null),
    });
  }

  uploadFile(event: Event): void {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file || this.uploading()) return;
    input.value = '';
    this.logoError.set(null);
    if (!file.type.startsWith('image/') || file.size > 2 * 1024 * 1024) {
      this.logoError.set('Виберіть зображення до 2 МБ.'); return;
    }
    this.uploading.set(true);
    this.api.uploadLogo(file).subscribe({
      next: () => { this.uploading.set(false); this.toast.show('Логотип завантажено', 'success'); this.loadLogo(); },
      error: (err) => { this.uploading.set(false); this.logoError.set(err.message || 'Помилка'); },
    });
  }

  deleteLogo(): void {
    this.confirm.confirm({ title: 'Видалити логотип', message: 'Видалити логотип?' }).subscribe((ok) => {
      if (!ok) return;
      this.api.deleteLogo().subscribe({ next: () => { this.toast.show('Видалено', 'success'); this.logoUrl.set(null); }, error: (err) => this.toast.show(err.message, 'error') });
    });
  }
}
