import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { SettingsApiService } from '../../core/api/settings.api';
import { ToastService } from '../../core/services/toast.service';
import { ConfirmService } from '../../core/services/confirm.service';
import { LoadingStateComponent } from '../../shared/loading-state.component';
import { ErrorStateComponent } from '../../shared/error-state.component';

@Component({
  selector: 'app-settings-secrets',
  standalone: true,
  imports: [CommonModule, FormsModule, LoadingStateComponent, ErrorStateComponent],
  template: `
    <div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-secrets">
      <h3 class="text-lg font-semibold mb-4">Секрети інтеграцій</h3>
      <app-loading-state *ngIf="loading()" />
      <app-error-state *ngIf="error()" [message]="error()!" />
      <div *ngIf="!loading() && !error()">
        <div *ngFor="let name of secretNames" class="flex flex-wrap items-center justify-between gap-2 py-2 border-b">
          <span class="font-mono text-sm break-all">{{ name }}</span>
          <div class="flex flex-wrap items-center gap-2">
            <ng-container *ngIf="editingSecret() === name; else secretActions">
              <label class="sr-only" [for]="'secret-' + name">Нове значення для {{ name }}</label>
              <input [(ngModel)]="editValue" [id]="'secret-' + name" type="password"
                     class="px-2 py-1 text-sm border border-slate-200 rounded w-48">
              <button (click)="saveSecret(name)" [disabled]="secretSaving()"
                      class="text-sm text-emerald-700 font-medium disabled:opacity-50">Зберегти</button>
              <button (click)="editingSecret.set(null)" class="text-sm text-slate-600">Скасувати</button>
            </ng-container>
            <ng-template #secretActions>
              <span class="text-sm" [class.text-emerald-700]="secrets()?.[name]"
                    [class.text-slate-500]="!secrets()?.[name]">
                {{ secrets()?.[name] ? 'Встановлено' : 'Не встановлено' }}
              </span>
              <button (click)="startEdit(name)" class="text-sm text-blue-700">Редагувати</button>
              <button *ngIf="secrets()?.[name]" (click)="confirmDeleteSecret(name)"
                      class="text-sm text-red-700">Видалити</button>
            </ng-template>
          </div>
        </div>
        <div class="mt-4 pt-4 border-t border-slate-200">
          <h4 class="text-sm font-medium text-slate-700 mb-2">Новий секрет</h4>
          <div class="flex flex-wrap gap-2">
            <label class="sr-only" for="new-secret-name">Назва секрету</label>
            <input id="new-secret-name" [(ngModel)]="newSecretName" placeholder="Назва"
                   class="px-3 py-2 border border-slate-200 rounded-lg text-sm w-48">
            <label class="sr-only" for="new-secret-value">Значення секрету</label>
            <input id="new-secret-value" [(ngModel)]="newSecretValue" type="password" placeholder="Значення"
                   class="px-3 py-2 border border-slate-200 rounded-lg text-sm flex-1 min-w-48">
            <button (click)="createSecret()" [disabled]="!newSecretName || !newSecretValue || secretSaving()"
                    class="px-3 py-2 bg-emerald-600 text-white rounded-lg text-sm disabled:opacity-50">
              Створити
            </button>
          </div>
        </div>
      </div>
    </div>`,
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
    private settings: SettingsApiService,
    private toast: ToastService,
    private confirm: ConfirmService,
  ) {}

  ngOnInit(): void { this.loadSecrets(); }

  loadSecrets(): void {
    this.loading.set(true);
    this.error.set(null);
    this.settings.secrets().subscribe({
      next: (data) => { this.secrets.set(data.secrets); this.loading.set(false); },
      error: (err) => { this.error.set(err.message); this.loading.set(false); },
    });
  }

  startEdit(name: string): void { this.editingSecret.set(name); this.editValue = ''; }

  saveSecret(name: string): void {
    if (!this.editValue) return;
    this.secretSaving.set(true);
    this.settings.updateSecret(name, this.editValue).subscribe({
      next: () => { this.secretSaving.set(false); this.editingSecret.set(null); this.toast.show('Секрет збережено', 'success'); this.loadSecrets(); },
      error: (err) => { this.secretSaving.set(false); this.error.set(err.message); },
    });
  }

  confirmDeleteSecret(name: string): void {
    this.confirm.confirm({ title: 'Видалити секрет', message: `Видалити ${name}?` }).subscribe((ok) => {
      if (!ok) return;
      this.settings.deleteSecret(name).subscribe({ next: () => { this.toast.show('Секрет видалено', 'success'); this.loadSecrets(); }, error: (err) => this.error.set(err.message) });
    });
  }

  createSecret(): void {
    if (!this.newSecretName || !this.newSecretValue) return;
    this.secretSaving.set(true);
    this.settings.updateSecret(this.newSecretName, this.newSecretValue).subscribe({
      next: () => { this.toast.show('Секрет створено', 'success'); this.newSecretName = ''; this.newSecretValue = ''; this.secretSaving.set(false); this.loadSecrets(); },
      error: (err) => { this.secretSaving.set(false); this.error.set(err.message); },
    });
  }
}
