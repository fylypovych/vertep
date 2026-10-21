import { Component, inject, signal } from '@angular/core';
import { LabApiService } from '../lab-api.service';

@Component({
  selector: 'app-history',
  standalone: true,
  template: `
    <h1>Історія рішень (audit trail)</h1>
    <section class="panel">
      <button (click)="load()">Оновити</button>
      @if (error()) {
        <p class="error">{{ error() }}</p>
      }
      <table data-testid="history-records">
        <thead>
          <tr>
            <th>Час</th><th>Актор</th><th>Дія</th><th>Суб'єкт</th>
            <th>Рішення</th><th>Правило</th><th>Причина</th>
          </tr>
        </thead>
        <tbody>
          @for (item of records(); track $index) {
            <tr [attr.data-testid]="'history-' + item['action']">
              <td>{{ item['at'] }}</td>
              <td>{{ item['actor'] }}</td>
              <td>{{ item['action'] }}</td>
              <td>{{ item['subject'] }}</td>
              <td [class.ok]="item['decision'] === 'ALLOWED' ||
                             item['decision'] === 'CREATED' ||
                             item['decision'] === 'CLOSED'"
                  [class.error]="item['decision'] === 'DENIED' ||
                                 item['decision'] === 'REJECTED'">
                {{ item['decision'] }}
              </td>
              <td>{{ item['rule'] }}</td>
              <td>{{ item['reason'] }}</td>
            </tr>
          } @empty {
            <tr><td colspan="7" class="muted">записів немає</td></tr>
          }
        </tbody>
      </table>
    </section>
  `,
})
export class HistoryComponent {
  private readonly api = inject(LabApiService);
  readonly records = signal<Record<string, any>[]>([]);
  readonly error = signal('');

  constructor() {
    this.load();
  }

  load(): void {
    this.error.set('');
    this.api.auditRecords().subscribe({
      next: (data) => this.records.set(data.records || []),
      error: (err) => this.error.set(String(err?.message || err)),
    });
  }
}
