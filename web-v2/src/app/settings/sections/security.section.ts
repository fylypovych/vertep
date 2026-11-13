import { TPipe } from '../../core/i18n/t.pipe';
import { Component, inject, OnInit, signal } from '@angular/core';
import { I18nService } from '../../core/i18n/i18n.service';
import { CommonModule } from '@angular/common';
import { KeyValuePipe } from '@angular/common';
import { SecurityApiService } from '../../core/api/security.api';
import { ToastService } from '../../core/services/toast.service';
import { SecurityCheck, CertificatesResponse, SecurityEffectiveConfig } from '../../core/models';
import { LoadingStateComponent } from '../../shared/loading-state.component';
import { ErrorStateComponent } from '../../shared/error-state.component';

@Component({
  selector: 'app-settings-security',
  standalone: true,
  imports: [TPipe, CommonModule, KeyValuePipe, LoadingStateComponent, ErrorStateComponent],
  template: `<div class="space-y-6" data-testid="settings-security">
    <div class="bg-white rounded-xl border p-5">
      <h3 class="text-lg font-semibold mb-4">{{ 'Безпека' | t }}</h3>
      <app-loading-state *ngIf="loading()" />
      <div *ngIf="secCheck() as check">
        <p class="font-medium" [class.text-emerald-700]="check.ok" [class.text-amber-700]="!check.ok"
           data-testid="security-check-state">{{ (check.ok ? 'У нормі' : 'Потребує уваги') | t }}</p>
        <p class="text-sm" data-testid="security-check-recommendation">{{ (check.recommendation) | t }}</p>
        <p class="text-sm text-amber-700 mt-2" *ngIf="check.weak_or_missing?.length"
           data-testid="security-check-weak">
          {{ 'Слабкі або відсутні значення:' | t }} {{ check.weak_or_missing.join(', ') }}
        </p>
      </div>
      <div *ngIf="secCheck()?.checks as detail" class="mt-4 grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 min-w-0">
        <div class="border border-slate-200 rounded-xl p-4 min-w-0" data-testid="security-check-secret-store">
          <h4 class="text-sm font-semibold">{{ 'Сховище секретів' | t }}</h4>
          <p class="text-sm">{{ (label(detail.secrets_store.status)) | t }} — {{ (detail.secrets_store.detail) | t }}</p>
        </div>
        <div class="border border-slate-200 rounded-xl p-4 min-w-0">
          <h4 class="text-sm font-semibold">{{ 'Сертифікати та ключі' | t }}</h4>
          <ul class="text-sm list-disc pl-5">
            <li *ngFor="let item of detail.certificates | keyvalue" [attr.data-testid]="'security-cert-' + item.key">
              {{ (certName(item.key)) | t }}: {{ (label(item.value.status)) | t }}<span *ngIf="item.value.expires_at">
                {{ '(до' | t }} {{ formatDate(item.value.expires_at) }})</span>
              <span class="block font-mono text-xs text-slate-500 break-all" *ngIf="item.value.sha256">
                SHA-256: {{ item.value.sha256 }}</span>
            </li>
          </ul>
        </div>
        <div class="border border-slate-200 rounded-xl p-4 min-w-0">
          <h4 class="text-sm font-semibold">{{ 'Інтеграції' | t }}</h4>
          <p class="text-sm" *ngIf="!detail.integrations?.length">{{ 'Немає активних інтеграцій' | t }}</p>
          <ul class="text-sm list-disc pl-5">
            <li *ngFor="let item of detail.integrations">{{ item.name }}: {{ (label(item.status)) | t }}</li>
          </ul>
        </div>
        <div *ngIf="detail.effective_config as effective" class="border border-slate-200 rounded-xl p-4" data-testid="security-effective-config">
          <h4 class="text-sm font-semibold">{{ 'Ефективна конфігурація' | t }}</h4>
          <ul class="text-sm list-disc pl-5">
            <li>LLM: {{ (effective.llm_provider || 'не налаштовано') | t }}</li>
            <li>TTS: {{ (effective.tts_provider || 'не налаштовано') | t }}</li>
            <li>{{ 'Публікація:' | t }} {{ (effective.publisher_mock ? 'тестовий режим' : 'робочий режим') | t }}</li>
            <li data-testid="security-admin-credentials">{{ 'Обліковий запис адміністратора:' | t }} {{ adminState(effective) | t }}<span *ngIf="adminSources(effective) as sources"> ({{ sources | t }})</span>
              <span *ngIf="adminHint(effective) as hint" class="block text-xs text-amber-700">{{ hint | t }}</span></li>
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
      <dl *ngIf="certificates()" class="text-sm grid grid-cols-1 sm:grid-cols-[12rem_1fr] gap-x-4 gap-y-1 mb-3" data-testid="certificate-info">
        <ng-container *ngFor="let row of certificateRows()">
          <dt class="text-slate-500">{{ row.label | t }}</dt>
          <dd class="break-all">{{ row.value }}</dd>
        </ng-container>
      </dl>
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

  certificateRows(): { label: string; value: string }[] {
    const { pem: _pem, ...info } = (this.certificates() ?? {}) as unknown as Record<string, unknown>;
    return Object.entries(info).filter(([, value]) => value !== null && value !== undefined && value !== '')
      .map(([key, value]) => ({
        label: CERT_FIELDS[key] ?? key,
        value: Array.isArray(value) ? value.join(', ')
          : key.endsWith('_at') || key === 'not_after' || key === 'not_before' ? this.formatDate(String(value))
          : typeof value === 'object' ? JSON.stringify(value) : this.i18n.t(STATUS_LABELS[String(value)] ?? String(value)),
      }));
  }

  formatDate(value: string): string {
    const date = new Date(value);
    return isNaN(date.getTime()) ? value : date.toLocaleString(this.i18n.locale);
  }

  downloadCert(): void {
    const url = URL.createObjectURL(new Blob([this.certificatePem()], { type: 'application/x-pem-file' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = 'vertep.crt';
    link.click();
    URL.revokeObjectURL(url);
  }

  private i18n = inject(I18nService);
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

  /** The administrator account by the sources authentication uses (#457). */
  adminState(effective: SecurityEffectiveConfig): string {
    const state = effective.admin_credentials?.state ?? (effective.admin_password_set ? 'configured' : 'missing');
    return ({ configured: 'налаштовано', invalid: 'пароль не може бути перевірений', missing: 'не налаштовано' } as Record<string, string>)[state];
  }

  adminSources(effective: SecurityEffectiveConfig): string | null {
    const names: Record<string, string> = { first_run: 'створено в майстрі першого запуску', user_store: 'обліковий запис користувачів', environment: 'змінна ADMIN_PASSWORD' };
    const sources = effective.admin_credentials?.sources ?? [];
    return sources.length ? sources.map(source => names[source] ?? source).join(', ') : null;
  }

  adminHint(effective: SecurityEffectiveConfig): string | null {
    const credentials = effective.admin_credentials;
    if (!credentials) return null;
    if (credentials.state === 'invalid') return 'Збережений пароль адміністратора пошкоджено або має непідтримуваний формат: вхід цим обліковим записом неможливий.';
    if (credentials.state === 'missing') return 'Жодного облікового запису адміністратора не знайдено: ні з майстра першого запуску, ні у сховищі користувачів, ні в ADMIN_PASSWORD.';
    if (credentials.problems.length) return 'Частина записів адміністратора пошкоджена; вхід працює через інше джерело.';
    return null;
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
  HEALTHY: 'у нормі',
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

const CERT_FIELDS: Record<string, string> = {
  status: 'Стан', subject: 'Кому видано', issuer: 'Ким видано', expires_at: 'Діє до', not_after: 'Діє до', not_before: 'Діє від',
  renewed_at: 'Оновлено', serial: 'Серійний номер', serial_number: 'Серійний номер', sha256: 'SHA-256', fingerprint: 'Відбиток',
  san: 'Адреси сертифіката', subject_alt_names: 'Адреси сертифіката', days_left: 'Залишилось днів', domain: 'Домен', self_signed: 'Самопідписаний',
};

const CERT_LABELS: Record<string, string> = {
  server_certificate: 'Сертифікат сервера',
  server_key: 'Ключ сервера',
  node_ca: 'CA вузлів',
};
