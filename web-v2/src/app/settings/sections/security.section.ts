import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { SecurityApiService } from '../../core/api/security.api';
import { ToastService } from '../../core/services/toast.service';
import { SecurityCheck, CertificatesResponse } from '../../core/models';
import { LoadingStateComponent } from '../../shared/loading-state.component';
import { ErrorStateComponent } from '../../shared/error-state.component';

@Component({
  selector: 'app-settings-security',
  standalone: true,
  imports: [CommonModule, LoadingStateComponent, ErrorStateComponent],
  template: `<div class="space-y-6" data-testid="settings-security"><div class="bg-white rounded-xl border p-5"><h3 class="text-lg font-semibold mb-4">Безпека</h3><app-loading-state *ngIf="loading()" /><p *ngIf="secCheck()">{{ secCheck()!.ok ? 'OK' : 'Потребує уваги' }}</p><p class="text-sm">{{ secCheck()?.recommendation }}</p></div><div class="bg-white rounded-xl border p-5"><h3 class="text-lg font-semibold mb-4">Сертифікати</h3><app-loading-state *ngIf="certLoading()" /><app-error-state *ngIf="certError()" [message]="certError()!" /><pre *ngIf="certificates()" class="text-xs bg-slate-50 p-3">{{ certificates() | json }}</pre><button (click)="renewCert()" class="mt-2 px-3 py-2 bg-emerald-600 text-white rounded">Оновити сертифікат</button></div></div>`,
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
}
