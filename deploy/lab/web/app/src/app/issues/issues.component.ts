import { Component, inject, signal } from '@angular/core';
import { LabApiService } from '../lab-api.service';

@Component({
  selector: 'app-issues',
  standalone: true,
  template: `
    <h1>Issues lab / i / ir / var та докази</h1>
    <section class="panel">
      <button (click)="load()">Оновити</button>
      @if (error()) {
        <p class="error">{{ error() }}</p>
      }
      <h2>Створені</h2>
      <table data-testid="issues-created">
        <thead>
          <tr><th>Ключ</th><th>Ідентифікатор</th><th>Тип</th><th>Назва</th><th>Створено</th></tr>
        </thead>
        <tbody>
          @for (item of created(); track item['key']) {
            <tr [attr.data-testid]="'issue-' + (item['identifier'] || item['key'])">
              <td><code>{{ item['key'] }}</code></td>
              <td><code>{{ item['identifier'] || item['finding']?.['identifier'] }}</code></td>
              <td>{{ item['kind'] || 'lab' }}</td>
              <td>{{ item['title'] || item['finding']?.['title'] }}</td>
              <td>{{ item['created_at'] }}</td>
            </tr>
          } @empty {
            <tr><td colspan="5" class="muted">немає записів</td></tr>
          }
        </tbody>
      </table>
      <h2>Закриті (делеговані рішення)</h2>
      <table>
        <thead>
          <tr><th>Issue</th><th>Reviewer</th><th>Закрито</th></tr>
        </thead>
        <tbody>
          @for (item of closedKeys(); track item) {
            <tr>
              <td><code>{{ item }}</code></td>
              <td>{{ closed()[item]?.['reviewer'] }}</td>
              <td>{{ closed()[item]?.['closed_at'] }}</td>
            </tr>
          } @empty {
            <tr><td colspan="3" class="muted">немає закритих</td></tr>
          }
        </tbody>
      </table>
      <h2>Докази ir</h2>
      <table data-testid="issues-evidence">
        <thead>
          <tr><th>Issue</th><th>Версія</th><th>Commit</th><th>Reviewer</th><th>Тести</th></tr>
        </thead>
        <tbody>
          @for (item of evidence(); track $index) {
            <tr>
              <td><code>{{ item['issue'] }}</code></td>
              <td>{{ item['evidence']?.['version'] }}</td>
              <td><code>{{ item['evidence']?.['commit'] }}</code></td>
              <td>{{ item['evidence']?.['reviewer'] }}</td>
              <td>{{ json(item['evidence']?.['test_results']) }}</td>
            </tr>
          } @empty {
            <tr><td colspan="5" class="muted">доказів немає</td></tr>
          }
        </tbody>
      </table>
      <h2>Чому поверхню відмовлено (probe)</h2>
      @for (kind of kinds; track kind) {
        <p [attr.data-testid]="'probe-' + kind">
          <code>{{ kind }}</code>:
          @if (probes()[kind]; as probe) {
            <strong [class.ok]="probe['allowed']" [class.error]="!probe['allowed']">
              {{ probe['allowed'] ? 'дозволено' : 'відмовлено' }}
            </strong>
            — {{ probe['rule'] }}: {{ probe['reason'] }}
          }
        </p>
      }
    </section>
  `,
})
export class IssuesComponent {
  private readonly api = inject(LabApiService);
  readonly created = signal<Record<string, any>[]>([]);
  readonly closed = signal<Record<string, any>>({});
  readonly evidence = signal<Record<string, any>[]>([]);
  readonly probes = signal<Record<string, Record<string, any>>>({});
  readonly error = signal('');
  readonly kinds = ['lab', 'i', 'ir', 'var', 'edit', 'close', 'push'];

  constructor() {
    this.load();
  }

  closedKeys(): string[] {
    return Object.keys(this.closed());
  }

  load(): void {
    this.error.set('');
    this.api.issues().subscribe({
      next: (data) => {
        this.created.set(data.created || []);
        this.closed.set(data.closed || {});
      },
      error: (err) => this.error.set(String(err?.message || err)),
    });
    this.api.evidence().subscribe({
      next: (data) => this.evidence.set(data.evidence || []),
      error: () => this.evidence.set([]),
    });
    for (const kind of this.kinds) {
      this.api.probe(kind).subscribe({
        next: (probe) => this.probes.update((all) => ({ ...all, [kind]: probe })),
        error: () => undefined,
      });
    }
  }

  json(value: unknown): string {
    return JSON.stringify(value ?? null);
  }
}
