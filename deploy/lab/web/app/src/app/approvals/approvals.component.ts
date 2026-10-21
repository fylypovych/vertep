import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { LabApiService } from '../lab-api.service';

/** Approvals centre: rights requests, grants, delegated consolidation. */
@Component({
  selector: 'app-approvals',
  standalone: true,
  imports: [FormsModule],
  template: `
    <h1>Центр погоджень</h1>
    <section class="panel">
      <button (click)="load()">Оновити</button>
      @if (error()) {
        <p class="error" data-testid="approvals-error">{{ error() }}</p>
      }
      <h2>Запити прав</h2>
      <table>
        <thead>
          <tr><th>ID</th><th>Актор</th><th>Дія</th><th>Суб'єкт</th><th>Стан</th><th>Причина</th></tr>
        </thead>
        <tbody>
          @for (item of requests(); track item['request_id']) {
            <tr [attr.data-testid]="'rights-' + item['request_id']">
              <td><code>{{ item['request_id'] }}</code></td>
              <td>{{ item['actor'] }}</td>
              <td>{{ item['action'] }}</td>
              <td>{{ item['subject'] }}</td>
              <td>{{ item['state'] }}</td>
              <td>{{ item['reason'] }}</td>
            </tr>
          } @empty {
            <tr><td colspan="6" class="muted">запитів немає</td></tr>
          }
        </tbody>
      </table>
      <h2>Grants</h2>
      <table>
        <thead>
          <tr><th>ID</th><th>Актор</th><th>Дія</th><th>Суб'єкт</th><th>Діє до</th><th></th></tr>
        </thead>
        <tbody>
          @for (item of grants(); track item['grant_id']) {
            <tr [attr.data-testid]="'grant-' + item['grant_id']">
              <td><code>{{ item['grant_id'] }}</code></td>
              <td>{{ item['actor'] }}</td>
              <td>{{ item['action'] }}</td>
              <td>{{ item['subject'] }}</td>
              <td>{{ item['expires_at'] }}</td>
              <td>
                @if (pendingRevoke() === item['grant_id']) {
                  <button class="danger" data-testid="revoke-confirm"
                          (click)="confirmRevoke(item['grant_id'])">
                    Підтвердити відкликання
                  </button>
                  <button data-testid="revoke-cancel" (click)="pendingRevoke.set('')">
                    Скасувати
                  </button>
                } @else {
                  <button class="danger" data-testid="revoke-request"
                          (click)="pendingRevoke.set(item['grant_id'])"
                          [disabled]="item['revoked']">
                    Відкликати
                  </button>
                }
              </td>
            </tr>
          } @empty {
            <tr><td colspan="6" class="muted">grants немає</td></tr>
          }
        </tbody>
      </table>
      <h2>Консолідація залишків ir</h2>
      <p>
        <label>Source identifier
          <input type="text" data-testid="consolidate-source" [(ngModel)]="sourceId"
                 placeholder="ir.0.0.0.3">
        </label>
        <label>Залишок критеріїв (через кому)
          <input type="text" data-testid="consolidate-remaining"
                 [(ngModel)]="remainingInput" size="40"
                 placeholder="U2 browser scenario, U3 reports">
        </label>
      </p>
      <p class="muted">Successors: JSON-масив {{ '{{' }}title, identifier,
        transferred_criteria{{ '}}' }}</p>
      <textarea rows="8" style="width: 100%" data-testid="consolidate-successors"
                [(ngModel)]="successorsJson"></textarea>
      <p>
        <label>
          <input type="checkbox" data-testid="consolidate-confirm"
                 [checked]="confirmed()" (change)="setConfirmed($event)">
          Підтверджую небезпечну операцію консолідації
        </label>
        <button class="danger" data-testid="consolidate-run" (click)="consolidate()"
                [disabled]="!confirmed()">
          Консолідувати (власник)
        </button>
      </p>
      @if (outcome()) {
        <h2>Результат</h2>
        <pre data-testid="consolidate-outcome">{{ json(outcome()) }}</pre>
      }
      <h2>Історія консолідацій</h2>
      <pre data-testid="consolidate-transfers">{{ json(transfers()) }}</pre>
    </section>
  `,
})
export class ApprovalsComponent {
  private readonly api = inject(LabApiService);
  readonly requests = signal<Record<string, any>[]>([]);
  readonly grants = signal<Record<string, any>[]>([]);
  readonly transfers = signal<Record<string, any>>({});
  readonly error = signal('');
  readonly confirmed = signal(false);
  readonly pendingRevoke = signal('');
  readonly outcome = signal<Record<string, any> | null>(null);
  sourceId = '';
  remainingInput = '';
  successorsJson = '[]';

  constructor() {
    this.load();
  }

  load(): void {
    this.error.set('');
    this.api.rightsRequests().subscribe({
      next: (data) => this.requests.set(data.requests || []),
      error: (err) => this.error.set(String(err?.message || err)),
    });
    this.api.grants().subscribe({
      next: (data) => this.grants.set(data.grants || []),
      error: () => this.grants.set([]),
    });
    this.api.transfers().subscribe({
      next: (data) => this.transfers.set(data['transfers'] || {}),
      error: () => this.transfers.set({}),
    });
  }

  setConfirmed(event: Event): void {
    this.confirmed.set((event.target as HTMLInputElement).checked);
  }

  confirmRevoke(grantId: string): void {
    this.pendingRevoke.set('');
    this.api.revokeGrant(grantId).subscribe({
      next: () => this.load(),
      error: (err) => this.error.set(String(err?.message || err)),
    });
  }

  consolidate(): void {
    try {
      const successors = JSON.parse(this.successorsJson) as Record<string, any>[];
      const remaining = this.remainingInput.split(',').map((item) => item.trim())
        .filter(Boolean);
      this.api.consolidate(
        { identifier: this.sourceId, remaining_criteria: remaining },
        successors, 'консолідація з Web-панелі')
        .subscribe({
          next: (outcome) => {
            this.error.set('');
            this.outcome.set(outcome);
            this.load();
          },
          error: (err) => this.error.set(String(err?.message || err)),
        });
    } catch {
      this.error.set('successors має бути валідним JSON-масивом');
    }
  }

  json(value: unknown): string {
    return JSON.stringify(value ?? null, null, 2);
  }
}
