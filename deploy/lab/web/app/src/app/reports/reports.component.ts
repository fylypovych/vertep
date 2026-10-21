import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { LabApiService } from '../lab-api.service';

const SECTIONS = ['status', 'queue', 'roles', 'gpu', 'budget', 'policy', 'issues',
                  'evidence', 'rights', 'grants', 'transfers', 'notifications',
                  'audit'];

/** Combined configurable report; the same builder backs ``lab.cli report``. */
@Component({
  selector: 'app-reports',
  standalone: true,
  imports: [FormsModule],
  template: `
    <h1>Комбіновані звіти</h1>
    <section class="panel">
      <p class="muted">
        Секції звіту налаштовуються; той самий стан CLI показує командою
        <code>python -m lab.cli report --sections ...</code>.
      </p>
      @for (section of sections; track section) {
        <label>
          <input type="checkbox" [attr.data-testid]="'report-section-' + section"
                 [checked]="picked()[section]"
                 (change)="toggle(section)">
          {{ section }}
        </label>
      }
      <p>
        <button data-testid="report-fetch" (click)="fetch()"
                [disabled]="!pickedList().length">Отримати</button>
      </p>
      @if (error()) {
        <p class="error">{{ error() }}</p>
      }
      <pre data-testid="report-output">{{ json(report()) }}</pre>
    </section>
  `,
})
export class ReportsComponent {
  private readonly api = inject(LabApiService);
  readonly sections = SECTIONS;
  readonly picked = signal<Record<string, boolean>>(
    Object.fromEntries(SECTIONS.map((section) => [section, true])));
  readonly report = signal<Record<string, any> | null>(null);
  readonly error = signal('');

  pickedList(): string[] {
    return this.sections.filter((section) => this.picked()[section]);
  }

  toggle(section: string): void {
    this.picked.update((all) => ({ ...all, [section]: !all[section] }));
  }

  fetch(): void {
    this.error.set('');
    this.api.reports(this.pickedList()).subscribe({
      next: (report) => this.report.set(report),
      error: (err) => this.error.set(String(err?.message || err)),
    });
  }

  json(value: unknown): string {
    return JSON.stringify(value ?? null, null, 2);
  }
}
