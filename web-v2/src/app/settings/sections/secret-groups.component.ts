import { TPipe } from '../../core/i18n/t.pipe';
import { Component, Input, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { SettingsApiService } from '../../core/api/settings.api';
import { ToastService } from '../../core/services/toast.service';
import { ConfirmService } from '../../core/services/confirm.service';
import { LoadingStateComponent } from '../../shared/loading-state.component';
import { ErrorStateComponent } from '../../shared/error-state.component';

/** A credential shared with another tab is shown read-only there, never duplicated. */
export interface SecretGroup {
  id: string;
  label: string;
  names: string[];
  hint?: string;
  shared?: { name: string; note: string; tab: string; tabLabel: string };
}

/**
 * Credential groups backed by the existing encrypted secret store and API.
 * Only the saved/not-saved state is read; values are written, never returned.
 */
@Component({
  selector: 'app-secret-groups',
  standalone: true,
  imports: [TPipe, CommonModule, FormsModule, RouterLink, LoadingStateComponent, ErrorStateComponent],
  template: `
    <app-loading-state *ngIf="loading()" />
    <app-error-state *ngIf="error()" [message]="error()!" />
    <div *ngIf="!loading() && !error()" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
      <section *ngFor="let group of groups" class="border border-slate-200 rounded-xl p-4 min-w-0" [attr.data-testid]="'secret-group-' + group.id">
        <div class="flex items-center justify-between mb-2">
          <h4 class="font-semibold text-slate-900">{{ group.label | t }}</h4>
          <span *ngIf="group.names.length" class="text-xs text-slate-500">{{ savedCount(group.names) }}/{{ group.names.length }}</span>
        </div>
        <p *ngIf="group.hint" class="text-xs text-slate-500 mb-2">{{ group.hint | t }}</p>
        <div *ngIf="group.shared as shared" class="py-2 border-t border-slate-100 text-sm" [attr.data-testid]="'secret-shared-' + group.id">
          <span class="block font-medium">{{ secretLabel(shared.name) | t }}
            <span [class.text-emerald-700]="secrets()?.[shared.name]" [class.text-slate-500]="!secrets()?.[shared.name]">
              — {{ (secrets()?.[shared.name] ? 'Збережено' : 'Не збережено') | t }}</span></span>
          <span class="block text-xs text-slate-500">{{ shared.note | t }}</span>
          <a [routerLink]="['/settings']" [queryParams]="{ tab: shared.tab }" class="text-sm text-blue-700">{{ 'Змінити у вкладці' | t }} «{{ shared.tabLabel | t }}»</a>
        </div>
        <div *ngFor="let name of group.names" class="flex flex-wrap items-center justify-between gap-2 py-2 border-t border-slate-100">
          <span class="min-w-0"><span class="block text-sm font-medium">{{ secretLabel(name) | t }}</span><span class="block font-mono text-xs text-slate-400 break-all">{{ name }}</span></span>
          <div class="flex flex-wrap items-center gap-2">
            <ng-container *ngIf="editingSecret() === name; else secretActions">
              <label class="sr-only" [for]="'secret-' + name">{{ 'Нове значення для' | t }} {{ name }}</label>
              <input [(ngModel)]="editValue" [id]="'secret-' + name" type="password" autocomplete="off"
                     class="px-2 py-1 text-sm border border-slate-200 rounded w-full">
              <button (click)="saveSecret(name)" [disabled]="secretSaving()"
                      class="text-sm text-emerald-700 font-medium disabled:opacity-50">{{ 'Зберегти' | t }}</button>
              <button (click)="editingSecret.set(null)" class="text-sm text-slate-600">{{ 'Скасувати' | t }}</button>
            </ng-container>
            <ng-template #secretActions>
              <span class="text-sm" [class.text-emerald-700]="secrets()?.[name]" [class.text-slate-500]="!secrets()?.[name]">
                {{ (secrets()?.[name] ? 'Збережено' : 'Не збережено') | t }}
              </span>
              <button (click)="startEdit(name)" class="text-sm text-blue-700">{{ 'Редагувати' | t }}</button>
              <button *ngIf="secrets()?.[name]" (click)="confirmDeleteSecret(name)" class="text-sm text-red-700">{{ 'Видалити' | t }}</button>
            </ng-template>
          </div>
        </div>
      </section>
    </div>`,
})
export class SecretGroupsComponent implements OnInit {
  @Input({ required: true }) groups: SecretGroup[] = [];
  secrets = signal<Record<string, boolean> | null>(null);
  loading = signal(false);
  error = signal<string | null>(null);
  editingSecret = signal<string | null>(null);
  secretSaving = signal(false);
  editValue = '';

  constructor(private settings: SettingsApiService, private toast: ToastService, private confirm: ConfirmService) {}

  ngOnInit(): void { this.load(); }

  load(): void {
    this.loading.set(true);
    this.error.set(null);
    this.settings.secrets().subscribe({
      next: (data) => { this.secrets.set(data.secrets); this.loading.set(false); },
      error: (err) => { this.error.set(err.message); this.loading.set(false); },
    });
  }

  secretLabel(name: string): string {
    const labels: [string, string][] = [['client_id', 'Client ID'], ['client_secret', 'Client Secret'], ['access_token', 'Токен доступу'],
      ['refresh_token', 'Токен оновлення'], ['bot_token', 'Токен бота'], ['api_key', 'API key'], ['password', 'Пароль'], ['token', 'Токен']];
    return labels.find(([suffix]) => name.endsWith(suffix))?.[1] ?? name;
  }

  savedCount(names: string[]): number { return names.filter(name => this.secrets()?.[name]).length; }

  startEdit(name: string): void { this.editingSecret.set(name); this.editValue = ''; }

  saveSecret(name: string): void {
    if (!this.editValue) return;
    this.secretSaving.set(true);
    this.settings.updateSecret(name, this.editValue).subscribe({
      next: () => { this.secretSaving.set(false); this.editingSecret.set(null); this.editValue = ''; this.toast.show('Секрет збережено', 'success'); this.load(); },
      error: (err) => { this.secretSaving.set(false); this.error.set(err.message); },
    });
  }

  confirmDeleteSecret(name: string): void {
    this.confirm.confirm({ title: 'Видалити секрет', message: `Видалити ${name}?` }).subscribe((ok) => {
      if (!ok) return;
      this.settings.deleteSecret(name).subscribe({ next: () => { this.toast.show('Секрет видалено', 'success'); this.load(); }, error: (err) => this.error.set(err.message) });
    });
  }
}
