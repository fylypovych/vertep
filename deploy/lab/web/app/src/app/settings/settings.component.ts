import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { LabApiService } from '../lab-api.service';

const CHANNELS = ['web', 'telegram'] as const;
const LEVELS = ['info', 'important', 'critical'] as const;

/** One importance store for Web and Telegram; writes are owner acts (U3). */
@Component({
  selector: 'app-settings',
  standalone: true,
  imports: [FormsModule],
  template: `
    <h1>Налаштування важливості сповіщень</h1>
    <section class="panel">
      <p class="muted">
        Один магазин налаштувань для Web-панелі та Telegram; збереження —
        акт власника (потрібен owner token), сервер перевіряє все повторно.
      </p>
      @if (error()) {
        <p class="error" data-testid="notify-error">{{ error() }}</p>
      }
      @if (saved()) {
        <p class="ok" data-testid="notify-saved">Збережено.</p>
      }
      @if (settings(); as model) {
        @for (channel of channels; track channel) {
          <h2>{{ channel }}</h2>
          <p>
            @for (level of levels; track level) {
              <label>
                <input type="checkbox"
                       [attr.data-testid]="'notify-' + channel + '-' + level"
                       [checked]="model[channel][level]"
                       (change)="toggle(channel, level)">
                {{ level }}
              </label>
            }
          </p>
        }
        <button data-testid="notify-save" (click)="save()">Зберегти (власник)</button>
      }
      <h2>Поточний стан (GET)</h2>
      <pre data-testid="notify-state">{{ json(settings()) }}</pre>
    </section>
  `,
})
export class SettingsComponent {
  private readonly api = inject(LabApiService);
  readonly settings = signal<Record<string, Record<string, boolean>> | null>(null);
  readonly error = signal('');
  readonly saved = signal(false);
  readonly channels = CHANNELS;
  readonly levels = LEVELS;

  constructor() {
    this.api.notifications().subscribe({
      next: (settings) => this.settings.set(settings),
      error: (err) => this.error.set(String(err?.message || err)),
    });
  }

  toggle(channel: string, level: string): void {
    this.settings.update((model) => {
      if (!model) {
        return model;
      }
      const next: Record<string, Record<string, boolean>> = {
        ...model,
        [channel]: { ...model[channel], [level]: !model[channel][level] },
      };
      return next;
    });
  }

  save(): void {
    const settings = this.settings();
    if (!settings) {
      return;
    }
    this.saved.set(false);
    this.error.set('');
    this.api.saveNotifications(settings).subscribe({
      next: (result) => {
        this.settings.set(result['settings'] as Record<string, Record<string, boolean>>);
        this.saved.set(true);
      },
      error: (err) => this.error.set(String(err?.message || err)),
    });
  }

  json(value: unknown): string {
    return JSON.stringify(value ?? null, null, 2);
  }
}
