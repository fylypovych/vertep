import { Component, inject, signal } from '@angular/core';
import { LabApiService } from '../lab-api.service';

@Component({
  selector: 'app-gpu',
  standalone: true,
  template: `
    <h1>Статус GPU та моделей</h1>
    <section class="panel">
      <button (click)="load()">Оновити</button>
      @if (error()) {
        <p class="error">{{ error() }}</p>
      }
      <pre data-testid="gpu-status">{{ json(status()) }}</pre>
    </section>
  `,
})
export class GpuComponent {
  private readonly api = inject(LabApiService);
  readonly status = signal<Record<string, any> | null>(null);
  readonly error = signal('');

  constructor() {
    this.load();
  }

  load(): void {
    this.error.set('');
    this.api.gpu().subscribe({
      next: (status) => this.status.set(status),
      error: (err) => this.error.set(String(err?.message || err)),
    });
  }

  json(value: unknown): string {
    return JSON.stringify(value ?? null, null, 2);
  }
}
