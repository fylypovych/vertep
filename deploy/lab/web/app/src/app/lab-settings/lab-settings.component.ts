import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { LabApiService } from '../lab-api.service';

const FIELDS = ['github_repository', 'resource_url', 'vertep_url', 'resource_ca_pem'] as const;

/** LAB settings through the one LAB API (#491 L4): the same store as `lab settings`. */
@Component({
  selector: 'app-lab-settings',
  standalone: true,
  imports: [FormsModule],
  template: `
    <h1>Налаштування LAB</h1>
    <section class="panel">
      <p class="muted">
        Зберігаються в LAB (0600) і мають пріоритет над lab.env; токени лише
        записуються — назад повертається тільки ознака «задано». Зміна адреси
        ресурсу GPU набуває чинності після перезапуску lab-control.
      </p>
      @if (error()) {
        <p class="error" data-testid="lab-settings-error">{{ error() }}</p>
      }
      @if (saved()) {
        <p class="ok" data-testid="lab-settings-saved">Збережено.</p>
      }
      <p><label>GitHub repository (owner/name)
        <input data-testid="lab-settings-github_repository" [(ngModel)]="model.github_repository"></label></p>
      <p><label>GitHub token ({{ state()?.['github_token_set'] ? 'задано' : 'не задано' }})
        <input type="password" data-testid="lab-settings-github_token" [(ngModel)]="secrets.github_token"
               placeholder="порожньо — без змін"></label></p>
      <p><label>Адреса ресурсу GPU (LAB_RESOURCE_URL)
        <input data-testid="lab-settings-resource_url" [(ngModel)]="model.resource_url" size="48"></label></p>
      <p><label>Токен ресурсу ({{ state()?.['resource_token_set'] ? 'задано' : 'не задано' }})
        <input type="password" data-testid="lab-settings-resource_token" [(ngModel)]="secrets.resource_token"
               placeholder="порожньо — без змін"></label></p>
      <p><label>CA сертифікат Vertep (PEM)<br>
        <textarea data-testid="lab-settings-resource_ca_pem" rows="4" cols="64"
                  [(ngModel)]="model.resource_ca_pem"></textarea></label></p>
      <p><label>Адреса Vertep (для «Увійти через Vertep»)
        <input data-testid="lab-settings-vertep_url" [(ngModel)]="model.vertep_url" size="48"></label></p>
      <p><label><input type="checkbox" data-testid="lab-settings-idle_sharing_enabled"
                       [(ngModel)]="idleSharing"> Доступ до GPU Vertep у простої (idle sharing)</label></p>
      <button data-testid="lab-settings-save" (click)="save()">Зберегти</button>
    </section>
  `,
})
export class LabSettingsComponent {
  private readonly api = inject(LabApiService);
  readonly state = signal<Record<string, any> | null>(null);
  readonly error = signal('');
  readonly saved = signal(false);
  model = { github_repository: '', resource_url: '', vertep_url: '', resource_ca_pem: '' };
  secrets = { github_token: '', resource_token: '' };
  idleSharing = false;

  constructor() {
    this.api.labSettings().subscribe({
      next: (state) => this.load(state),
      error: (err) => this.error.set(String(err?.message || err)),
    });
  }

  private load(state: Record<string, any>): void {
    this.state.set(state);
    this.model = { github_repository: '', resource_url: '', vertep_url: '', resource_ca_pem: '' };
    for (const field of FIELDS) {
      this.model[field] = String(state[field] ?? '');
    }
    this.idleSharing = !!state['idle_sharing_enabled'];
    this.secrets = { github_token: '', resource_token: '' };
  }

  save(): void {
    const settings: Record<string, any> = { ...this.model, idle_sharing_enabled: this.idleSharing };
    for (const [name, value] of Object.entries(this.secrets)) {
      if (value.trim()) {
        settings[name] = value.trim();
      }
    }
    this.saved.set(false);
    this.error.set('');
    this.api.saveLabSettings(settings).subscribe({
      next: (state) => { this.load(state); this.saved.set(true); },
      error: (err) => this.error.set(String(err?.error?.detail || err?.message || err)),
    });
  }
}
