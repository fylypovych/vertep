import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { VertepApiService } from '../../core/api.service';
import { ToastService } from '../../core/services/toast.service';
import { SecurityCheck } from '../../core/models';

@Component({
  selector: 'app-settings-security',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="space-y-6" data-testid="settings-security">
      <div class="bg-white rounded-xl border border-slate-200 p-5">
        <h3 class="text-lg font-semibold text-slate-900 mb-4">Безпека</h3>
        @if (loading()) {
          <p class="text-sm text-slate-500">Завантаження...</p>
        } @else if (secCheck()) {
          <div class="space-y-2">
            <div class="flex justify-between py-2 border-b border-slate-100">
              <span class="text-sm">Статус</span>
              <span class="text-sm font-medium" [class.text-emerald-600]="secCheck()!.ok" [class.text-red-600]="!secCheck()!.ok">
                {{ secCheck()!.ok ? 'OK' : 'Увага' }}
              </span>
            </div>
            @if (secCheck()!.weak_or_missing.length) {
              <div class="text-sm text-slate-700">
                <p class="font-medium mb-1">Слабкі/відсутні:</p>
                <ul class="list-disc pl-5 space-y-0.5">
                  @for (item of secCheck()!.weak_or_missing; track item) {
                    <li class="text-xs text-slate-600">{{ item }}</li>
                  }
                </ul>
              </div>
            }
            <p class="text-xs text-slate-500">{{ secCheck()!.recommendation }}</p>
          </div>
        }
      </div>
      <div class="bg-white rounded-xl border border-slate-200 p-5">
        <h3 class="text-lg font-semibold text-slate-900 mb-4">Сертифікати</h3>
        @if (certLoading()) {
          <p class="text-sm text-slate-500">Завантаження...</p>
        } @else if (certError()) {
          <p class="text-red-600">{{ certError() }}</p>
        } @else if (certificates()) {
          <pre class="text-xs text-slate-700 bg-slate-50 p-3 rounded overflow-x-auto max-h-48">{{ certificates() | json }}</pre>
          <button (click)="renewCert()" class="mt-2 px-3 py-1.5 text-sm bg-emerald-600 text-white rounded-lg hover:bg-emerald-700">Оновити сертифікат</button>
        }
      </div>
    </div>
  `,
})
export class SecuritySectionComponent implements OnInit {
  secCheck = signal<SecurityCheck | null>(null);
  loading = signal(false);
  certificates = signal<Record<string, unknown> | null>(null);
  certLoading = signal(false);
  certError = signal<string | null>(null);

  constructor(private api: VertepApiService, private toast: ToastService) {}

  ngOnInit(): void {
    this.loading.set(true);
    this.api.getSecurityCheck().subscribe({
      next: (c) => { this.secCheck.set(c); this.loading.set(false); },
      error: () => this.loading.set(false),
    });
    this.certLoading.set(true);
    this.api.getCertificates().subscribe({
      next: (c) => { this.certificates.set(c); this.certLoading.set(false); },
      error: (err) => { this.certError.set(err.message); this.certLoading.set(false); },
    });
  }

  renewCert(): void {
    this.api.renewCertificate().subscribe({
      next: () => { this.toast.show('Сертифікат оновлено', 'success'); this.ngOnInit(); },
      error: (err) => this.toast.show(err.message || 'Помилка оновлення', 'error'),
    });
  }
}
