import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { LabApiService } from '../lab-api.service';

/** Batch review of ``ir`` candidates: evidence first, delegated close after. */
@Component({
  selector: 'app-batch-ir',
  standalone: true,
  imports: [FormsModule],
  template: `
    <h1>Пакетна перевірка ir</h1>
    <section class="panel">
      <p class="muted">
        Список кандидатів і докази; закриття — лише делеговане рішення власника
        з повним незалежним доказом (сервер перевіряє все повторно).
      </p>
      <button (click)="load()">Оновити</button>
      @if (error()) {
        <p class="error">{{ error() }}</p>
      }
      <table>
        <thead>
          <tr><th></th><th>Issue</th><th>Назва</th><th>Закритий?</th><th>Доказ прийнято?</th></tr>
        </thead>
        <tbody>
          @for (item of candidates(); track item['identifier']) {
            <tr>
              <td>
                <input type="checkbox"
                       [attr.data-testid]="'batch-select-' + item['identifier']"
                       [checked]="selected()[item['identifier']] === true"
                       (change)="toggle(item['identifier'])">
              </td>
              <td><code>{{ item['identifier'] }}</code></td>
              <td>{{ item['title'] }}</td>
              <td>{{ closed()[item['identifier']] ? 'так' : 'ні' }}</td>
              <td>{{ accepted()[item['identifier']] ?? '—' }}</td>
            </tr>
          } @empty {
            <tr><td colspan="5" class="muted">ir-кандидатів немає</td></tr>
          }
        </tbody>
      </table>
      <h2>Доказ (один на пакет)</h2>
      <textarea rows="10" style="width: 100%" data-testid="batch-ir-evidence"
                [(ngModel)]="evidenceJson"></textarea>
      <p>
        <label>
          <input type="checkbox" data-testid="batch-ir-confirm"
                 [checked]="confirmed()" (change)="setConfirmed($event)">
          Підтверджую небезпечну операцію делегованого закриття
        </label>
      </p>
      <button class="danger" data-testid="batch-ir-submit" (click)="submitEvidence()"
              [disabled]="!candidates().length">Прийняти доказ для вибраних</button>
      <button class="danger" data-testid="batch-ir-close" (click)="closeSelected()"
              [disabled]="!confirmed()">Закрити вибрані (власник)</button>
      @if (results().length) {
        <h2>Результати</h2>
        <pre>{{ json(results()) }}</pre>
      }
    </section>
  `,
})
export class BatchIrComponent {
  private readonly api = inject(LabApiService);
  readonly candidates = signal<Record<string, any>[]>([]);
  readonly closed = signal<Record<string, boolean>>({});
  readonly accepted = signal<Partial<Record<string, boolean>>>({});
  readonly selected = signal<Record<string, boolean>>({});
  readonly results = signal<string[]>([]);
  readonly error = signal('');
  readonly confirmed = signal(false);
  evidenceJson = JSON.stringify({
    version: '0.0.0.0',
    commit: '0000000',
    environment: 'lab-vm',
    expected: 'усі критерії виконані',
    actual: 'усі критерії виконані',
    reviewer: 'independent-reviewer',
    test_results: { passed: 1, failed: 0, skipped: 0 },
  }, null, 2);

  constructor() {
    this.load();
  }

  load(): void {
    this.error.set('');
    this.api.issues().subscribe({
      next: (data) => {
        const irItems = (data.created || []).filter(
          (item) => (item['kind'] || '') === 'ir');
        this.candidates.set(irItems);
        this.closed.set(Object.fromEntries(
          Object.keys(data.closed || {}).map((key) => [key, true])));
      },
      error: (err) => this.error.set(String(err?.message || err)),
    });
    this.api.evidence().subscribe({
      next: (data) => {
        const verdicts: Partial<Record<string, boolean>> = {};
        for (const record of data.evidence || []) {
          verdicts[String(record['issue'])] = record['evidence'] != null;
        }
        this.accepted.set(verdicts);
      },
      error: () => undefined,
    });
  }

  toggle(identifier: string): void {
    this.selected.update((all) => ({ ...all, [identifier]: !all[identifier] }));
  }

  setConfirmed(event: Event): void {
    this.confirmed.set((event.target as HTMLInputElement).checked);
  }

  private parseEvidence(): Record<string, any> | null {
    try {
      return JSON.parse(this.evidenceJson) as Record<string, any>;
    } catch {
      this.error.set('доказ має бути валідним JSON');
      return null;
    }
  }

  submitEvidence(): void {
    const evidence = this.parseEvidence();
    if (!evidence) {
      return;
    }
    const targets = this.candidates().filter(
      (item) => this.selected()[item['identifier']]);
    const outcomes: string[] = [];
    for (const item of targets) {
      this.api.submitEvidence(item['identifier'], evidence).subscribe({
        next: (verdict) => outcomes.push(
          `${item['identifier']}: accepted=${verdict['accepted']}` +
          (verdict['problems'] ? ` (${verdict['problems'].join('; ')})` : '')),
        error: (err) => outcomes.push(`${item['identifier']}: ${err?.message || err}`),
      });
    }
    this.results.set(outcomes);
  }

  json(value: unknown): string {
    return Array.isArray(value) ? value.join('\n') : JSON.stringify(value ?? null);
  }

  closeSelected(): void {
    const evidence = this.parseEvidence();
    if (!evidence) {
      return;
    }
    const targets = this.candidates().filter(
      (item) => this.selected()[item['identifier']] && !this.closed()[item['identifier']]);
    const outcomes: string[] = [];
    for (const item of targets) {
      this.api.closeIr(item['identifier'], evidence).subscribe({
        next: (outcome) => outcomes.push(
          `${item['identifier']}: closed=${outcome['closed']} ` +
          `${outcome['reason'] || outcome['rule'] || ''}`),
        error: (err) => outcomes.push(`${item['identifier']}: ${err?.message || err}`),
      });
    }
    this.results.set(outcomes);
  }
}
