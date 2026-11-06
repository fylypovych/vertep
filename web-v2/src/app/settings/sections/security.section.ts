import { TPipe } from '../../core/i18n/t.pipe';
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
  imports: [TPipe, CommonModule, KeyValuePipe, DatePipe, LoadingStateComponent, ErrorStateComponent],
  template: `<div class="space-y-6" data-testid="settings-security">
    <div class="bg-white rounded-xl border p-5">
      <h3 class="text-lg font-semibold mb-4">{{ 'Безпека' | t }}</h3>
      <app-loading-state *ngIf="loading()" />
      <div *ngIf="secCheck() as check">
        <p class="font-medium" [class.text-emerald-700]="check.ok" [class.text-amber-700]="!check.ok"
           data-testid="security-check-state">{{ (check.ok ? 'У нормі' : 'Потребує уваги') | t }}</p>
        <p class="text-sm" data-testid="security-check-recommendation">{{ check.recommendation }}</p>
        <p class="text-sm text-amber-700 mt-2" *ngIf="check.weak_or_missing.length"
           data-testid="security-check-weak">
          {{ 'Слабкі або відсутні значення:' | t }} {{ check.weak_or_missing.join(', ') }}
        </p>
      </div>
      <div *ngIf="secCheck()?.checks as detail" class="mt-4 space-y-3">
        <div data-testid="security-check-secret-store">
          <h4 class="text-sm font-semibold">{{ 'Сховище секретів' | t }}</h4>
          <p class="text-sm">{{ (label(detail.secrets_store.status)) | t }} — {{ detail.secrets_store.detail }}</p>
        </div>
        <div>
          <h4 class="text-sm font-semibold">{{ 'Сертифікати та ключі' | t }}</h4>
          <ul class="text-sm list-disc pl-5">
            <li *ngFor="let item of detail.certificates | keyvalue" [attr.data-testid]="'security-cert-' + item.key">
              {{ certName(item.key) }}: {{ (label(item.value.status)) | t }}<span *ngIf="item.value.expires_at">
                {{ '(до' | t }} {{ item.value.expires_at | date: 'yyyy-MM-dd' }})</span>
              <span class="block font-mono text-xs text-slate-500" *ngIf="item.value.sha256">
                SHA-256: {{ item.value.sha256 }}</span>
            </li>
          </ul>
        </div>
        <div>
          <h4 class="text-sm font-semibold">{{ 'Інтеграції' | t }}</h4>
          <p class="text-sm" *ngIf="!detail.integrations.length">{{ 'Немає активних інтеграцій' | t }}</p>
          <ul class="text-sm list-disc pl-5">
            <li *ngFor="let item of detail.integrations">{{ item.name }}: {{ (label(item.status)) | t }}</li>
          </ul>
        </div>
        <div *ngIf="detail.effective_config as effective" data-testid="security-effective-config">
          <h4 class="text-sm font-semibold">{{ 'Ефективна конфігурація' | t }}</h4>
          <ul class="text-sm list-disc pl-5">
            <li>LLM: {{ (effective.llm_provider || 'не налаштовано') | t }}</li>
            <li>TTS: {{ (effective.tts_provider || 'не налаштовано') | t }}</li>
            <li>{{ 'Публікація:' | t }} {{ (effective.publisher_mock ? 'тестовий режим' : 'робочий режим') | t }}</li>
            <li>{{ 'Пароль адміністратора:' | t }} {{ (effective.admin_password_set ? 'встановлено' : 'відсутній') | t }}</li>
            <li>{{ 'Токен вузла:' | t }} {{ (effective.node_api_token_set ? 'встановлено' : 'відсутній') | t }}</li>
            <li>{{ 'Пароль PostgreSQL:' | t }} {{ (effective.postgres_password_set ? 'встановлено' : 'відсутній') | t }}</li>
            <li>{{ 'Ключ сховища секретів:' | t }} {{ (effective.secret_store_passphrase_set ? 'встановлено' : 'відсутній') | t }}</li>
            <li>{{ 'Секрет сесії:' | t }} {{ (effective.session_secret_set ? 'встановлено' : 'відсутній') | t }}</li>
          </ul>
        </div>
      </div>
    </div>
    <div class="bg-white rounded-xl border p-5">
      <h3 class="text-lg font-semibold mb-4">{{ 'Сертифікати' | t }}</h3>
      <app-loading-state *ngIf="certLoading()" />
      <app-error-state *ngIf="certError()" [message]="certError()!" />
      <pre *ngIf="certificates()" class="text-xs bg-slate-50 p-3 overflow-x-auto">{{ certificateInfo() | json }}</pre>
      <p class="text-sm text-slate-600 my-2">{{ 'Сертифікат самопідписаний: дані шифруються, але браузер показуватиме попередження, доки ви не додасте завантажений файл до довірених сертифікатів на своєму комп\\'ютері. Після створення нового сертифіката файл треба додати знову.' | t }}</p>
      <div class="flex flex-wrap gap-2">
        <button (click)="renewCert()" data-testid="certificate-renew" class="px-3 py-2 bg-emerald-600 text-white rounded">{{ 'Створити або оновити сертифікат' | t }}</button>
        <button (click)="downloadCert()" [disabled]="!certificatePem()" data-testid="certificate-download" class="px-3 py-2 border rounded disabled:opacity-50">{{ 'Завантажити сертифікат' | t }}</button>
      </div>
    </div>
  </div>`,
})
export class SecuritySectionComponent implements OnInit {
  secCheck = signal<SecurityCheck | null>(null);
  loading = signal(false);
  certificates = signal<CertificatesResponse | null>(null);

  certificatePem(): string { return (this.certificates() as unknown as { pem?: string } | null)?.pem ?? ''; }

  certificateInfo(): unknown {
    const { pem: _pem, ...info } = (this.certificates() ?? {}) as unknown as Record<string, unknown>;
    return info;
  }

  downloadCert(): void {
    const url = URL.createObjectURL(new Blob([this.certificatePem()], { type: 'application/x-pem-file' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = 'vertep.crt';
    link.click();
    URL.revokeObjectURL(url);
  }

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
