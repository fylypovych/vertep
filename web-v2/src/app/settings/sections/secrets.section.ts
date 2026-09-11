import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { VertepApiService } from '../../core/api.service';
import { ToastService } from '../../core/services/toast.service';
import { ConfirmService } from '../../core/services/confirm.service';

@Component({
  selector: 'app-settings-secrets',
  standalone: true,
  imports: [CommonModule, FormsModule],
  template: `
    <div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-secrets">
      <h3 class="text-lg font-semibold text-slate-900 mb-4">Сховище секретів</h3>
      @if (loading()) {
        <div class="animate-pulse space-y-2"><div class="h-5 bg-slate-200 rounded w-full"></div></div>
      } @else if (error()) {
        <p class="text-red-600">{{ error() }}</p>
      } @else {
        @for (name of secretNames; track name) {
          <div class="flex items-center justify-between py-2 border-b border-slate-100">
            <span class="text-sm text-slate-600 font-mono">{{ name }}</span>
            <div class="flex items-center gap-2">
              @if (editingSecret() === name) {
                <input [(ngModel)]="editValue" type="password" class="px-2 py-1 text-sm border border-slate-200 rounded w-48">
                <button (click)="saveSecret(name)" [disabled]="secretSaving()" class="text-xs text-emerald-600 font-medium">Зберегти</button>
                <button (click)="editingSecret.set(null)" class="text-xs text-slate-500">Скасувати</button>
              } @else {
                <span class="text-xs" [class.text-emerald-600]="secrets()?.[name]" [class.text-slate-400]="!secrets()?.[name]">
                  {{ secrets()?.[name] ? 'Встановлено' : 'Не встановлено' }}
                </span>
                <button (click)="startEdit(name)" class="text-xs text-blue-600">Редагувати</button>
                @if (secrets()?.[name]) {
                  <button (click)="confirmDeleteSecret(name)" class="text-xs text-red-600">Видалити</button>
                }
              }
            </div>
          </div>
        }
        <div class="mt-4 pt-4 border-t border-slate-200">
          <h4 class="text-sm font-medium text-slate-700 mb-2">Додати секрет</h4>
          <div class="flex gap-2">
            <input [(ngModel)]="newSecretName" placeholder="Назва" class="px-3 py-2 border border-slate-200 rounded-lg text-sm w-48">
            <input [(ngModel)]="newSecretValue" type="password" placeholder="Значення" class="px-3 py-2 border border-slate-200 rounded-lg text-sm flex-1">
            <button (click)="createSecret()" [disabled]="!newSecretName || !newSecretValue" class="px-3 py-2 bg-emerald-600 text-white rounded-lg text-sm hover:bg-emerald-700 disabled:opacity-50">Додати</button>
          </div>
        </div>
      }
    </div>
  `,
})
export class SecretsSectionComponent implements OnInit {
  secrets = signal<Record<string, boolean> | null>(null);
  loading = signal(false);
  error = signal<string | null>(null);
  editingSecret = signal<string | null>(null);
  secretSaving = signal(false);
  editValue = '';
  newSecretName = '';
  newSecretValue = '';

  readonly secretNames = [
    'telegram_bot_token', 'youtube_client_secret', 'facebook_access_token',
    'tiktok_client_secret', 'smtp_password', 'external_ai_api_key',
  ];

  constructor(
    private api: VertepApiService,
    private toast: ToastService,
    private confirm: ConfirmService,
  ) {}

  ngOnInit(): void { this.loadSecrets(); }

  loadSecrets(): void {
    this.loading.set(true);
    this.error.set(null);
    this.api.getSecrets().subscribe({
      next: (data) => { this.secrets.set(data.secrets); this.loading.set(false); },
      error: (err) => { this.error.set(err.message); this.loading.set(false); },
    });
  }

  startEdit(name: string): void { this.editingSecret.set(name); this.editValue = ''; }

  saveSecret(name: string): void {
    if (!this.editValue) return;
    this.secretSaving.set(true);
    this.api.updateSecret(name, this.editValue).subscribe({
      next: () => { this.secretSaving.set(false); this.editingSecret.set(null); this.toast.show('Секрет збережено', 'success'); this.loadSecrets(); },
      error: (err) => { this.secretSaving.set(false); this.error.set(err.message); },
    });
  }

  confirmDeleteSecret(name: string): void {
    this.confirm.confirm({ title: 'Видалити секрет', message: `Видалити ${name}?` }).subscribe((ok) => {
      if (!ok) return;
      this.api.deleteSecret(name).subscribe({ next: () => { this.toast.show('Секрет видалено', 'success'); this.loadSecrets(); }, error: (err) => this.error.set(err.message) });
    });
  }

  createSecret(): void {
    if (!this.newSecretName || !this.newSecretValue) return;
    this.secretSaving.set(true);
    this.api.updateSecret(this.newSecretName, this.newSecretValue).subscribe({
      next: () => { this.toast.show('Секрет створено', 'success'); this.newSecretName = ''; this.newSecretValue = ''; this.secretSaving.set(false); this.loadSecrets(); },
      error: (err) => { this.secretSaving.set(false); this.error.set(err.message); },
    });
  }
}
