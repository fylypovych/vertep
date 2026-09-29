import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { KeyValuePipe, DatePipe } from '@angular/common';
import { SecurityApiService } from '../../core/api/security.api';
import { ToastService } from '../../core/services/toast.service';
import { SecurityCheck, CertificatesResponse } from '../../core/models';
import { LoadingStateComponent } from '../../shared/loading-state.component';
import { ErrorStateComponent } from '../../shared/error-state.component';

@Component({
  selector: 'app-settings-security',
  standalone: true,
  imports: [CommonModule, KeyValuePipe, DatePipe, LoadingStateComponent, ErrorStateComponent],
  template: `<div class="space-y-6" data-testid="settings-security">
    <div class="bg-white rounded-xl border p-5">
      <h3 class="text-lg font-semibold mb-4">Безпека</h3>
      <app-loading-state *ngIf="loading()" />
      <div *ngIf="secCheck() as check">
        <p class="font-medium" [class.text-emerald-700]="check.ok" [class.text-amber-700]="!check.ok"
           data-testid="security-check-state">{{ check.ok ? 'OK' : 'Потребує уваги' }}</p>
        <p class="text-sm" data-testid="security-check-recommendation">{{ check.recommendation }}</p>
        <p class="text-sm text-amber-700 mt-2" *ngIf="check.weak_or_missing.length"
           data-testid="security-check-weak">
          Слабкі або відсутні значення: {{ check.weak_or_missing.join(', ') }}
        </p>
      </div>
      <div *ngIf="secCheck()?.checks as detail" class="mt-4 space-y-3">
        <div data-testid="security-check-secret-store">
          <h4 class="text-sm font-semibold">Сховище секретів</h4>
          <p class="text-sm">{{ label(detail.secrets_store.status) }} — {{ detail.secrets_store.detail }}</p>
        </div>
        <div>
          <h4 class="text-sm font-semibold">Сертифікати та ключі</h4>
          <ul class="text-sm list-disc pl-5">
            <li *ngFor="let item of detail.certificates | keyvalue" [attr.data-testid]="'security-cert-' + item.key">
              {{ certName(item.key) }}: {{ label(item.value.status) }}<span *ngIf="item.value.expires_at">
                (до {{ item.value.expires_at | date: 'yyyy-MM-dd' }})</span>
              <span class="block font-mono text-xs text-slate-500" *ngIf="item.value.sha256">
                SHA-256: {{ item.value.sha256 }}</span>
            </li>
          </ul>
        </div>
        <div>
          <h4 class="text-sm font-semibold">Інтеграції</h4>
          <p class="text-sm" *ngIf="!detail.integrations.length">Немає активних інтеграцій</p>
          <ul class="text-sm list-disc pl-5">
            <li *ngFor="let item of detail.integrations">{{ item.name }}: {{ label(item.status) }}</li>
          </ul>
        </div>
      </div>
    </div>
    <div class="bg-white rounded-xl border p-5">
      <h3 class="text-lg font-semibold mb-4">Сертифікати</h3>
      <app-loading-state *ngIf="certLoading()" />
      <app-error-state *ngIf="certError()" [message]="certError()!" />
      <pre *ngIf="certificates()" class="text-xs bg-slate-50 p-3">{{ certificates() | json }}</pre>
      <button (click)="renewCert()" class="mt-2 px-3 py-2 bg-emerald-600 text-white rounded">Оновити сертифікат</button>
    </div>
  </div>`,
})
export class SecuritySectionComponent implements OnInit {
  secCheck = signal<SecurityCheck | null>(null);
  loading = signal(false);
  certificates = signal<CertificatesResponse | null>(null);
  certLoading = signal(false);
  certError = signal<string | null>(null);

  constructor(private security: SecurityApiService, private toast: ToastService) {}

  ngOnInit(): void {
    this.loading.set(true);
    this.security.check().subscribe({
      next: (c) => { this.secCheck.set(c); this.loading.set(false); },
      error: () => this.loading.set(false),
    });
    this.certLoading.set(true);
    this.security.certificates().subscribe({
      next: (c) => { this.certificates.set(c); this.certLoading.set(false); },
      error: (err) => { this.certError.set(err.message); this.certLoading.set(false); },
    });
  }

  renewCert(): void {
    this.security.renewCertificate().subscribe({
      next: () => { this.toast.show('Сертифікат оновлено', 'success'); this.ngOnInit(); },
      error: (err) => this.toast.show(err.message || 'Помилка оновлення сертифіката', 'error'),
    });
  }

  label(status: string): string {
    return STATUS_LABELS[status] ?? status;
  }

  certName(key: string): string {
    return CERT_LABELS[key] ?? key;
  }
}

const STATUS_LABELS: Record<string, string> = {
  ok: 'у нормі',
  warning: 'потребує уваги',
  unusable: 'не працює',
  missing: 'відсутній',
  unreadable: 'не читається',
  expiring: 'спливає',
  expired: 'прострочений',
  ready: 'готово',
  configured: 'налаштовано',
  not_configured: 'не налаштовано',
  mock: 'тестовий режим',
  external: 'зовнішній',
};

const CERT_LABELS: Record<string, string> = {
  server_certificate: 'Сертифікат сервера',
  server_key: 'Ключ сервера',
  node_ca: 'CA вузлів',
};
