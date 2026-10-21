import { Component, inject, signal } from '@angular/core';
import { LabApiService } from '../lab-api.service';

@Component({
  selector: 'app-dashboard',
  standalone: true,
  template: `
    <h1>Dashboard стану</h1>
    <section class="panel">
      <button (click)="load()">Оновити</button>
      @if (error()) {
        <p class="error">{{ error() }}</p>
      }
      @if (status(); as s) {
        <h2>Policy gate</h2>
        <p>
          activated:
          <strong data-testid="policy-activated"
                  [class.ok]="s['policy']?.['activated']"
                  [class.error]="!s['policy']?.['activated']">
            {{ s['policy']?.['activated'] }}
          </strong>
          &nbsp;|&nbsp; active grants:
          {{ (s['policy']?.['active_grants'] || []).length }}
        </p>
        <h2>Черга та ролі</h2>
        <p>
          roles: <code data-testid="dashboard-roles">{{ (s['roles'] || [])?.join(', ') }}</code>
          &nbsp;|&nbsp; executor configured: {{ s['executor']?.['configured'] }}
          &nbsp;|&nbsp; queue: <span data-testid="dashboard-queue">{{ (s['queue'] || []).length }}</span>
        </p>
        <h2>Budget</h2>
        <p>
          runs/day {{ s['budget']?.['runs_last_day'] }} /
          {{ s['budget']?.['max_runs_per_day'] }},
          tokens/day {{ s['budget']?.['tokens_last_day'] }} /
          {{ s['budget']?.['max_tokens_per_day'] }}
        </p>
        <h2>GPU</h2>
        <pre data-testid="dashboard-gpu">{{ gpuText() }}</pre>
        <h2>Handoff</h2>
        <pre>{{ json(s['handoff']) }}</pre>
      }
    </section>
  `,
})
export class DashboardComponent {
  private readonly api = inject(LabApiService);
  readonly status = signal<Record<string, any> | null>(null);
  readonly error = signal('');
  readonly gpuText = signal('—');

  constructor() {
    this.load();
  }

  load(): void {
    this.error.set('');
    this.api.status().subscribe({
      next: (status) => this.status.set(status),
      error: (err) => this.error.set(String(err?.message || err)),
    });
    this.api.gpu().subscribe({
      next: (gpu) => this.gpuText.set(this.json(gpu)),
      error: () => this.gpuText.set('GPU недоступний'),
    });
  }

  json(value: unknown): string {
    return JSON.stringify(value ?? null, null, 2);
  }
}
