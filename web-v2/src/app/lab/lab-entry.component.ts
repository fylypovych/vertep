import { HttpClient } from '@angular/common/http';
import { Component, OnInit, inject, signal } from '@angular/core';

/**
 * «Лабораторія» (#491 L3): the signed-in administrator enters the separate LAB panel.
 *
 * The page asks CORE for the configured panel address and continues to
 * `/api/lab/login`, which redirects there with a short-lived signed assertion. The
 * LAB panel's «Увійти через Vertep» lands here too: the Vertep session cookie is
 * SameSite=Strict, so the request to CORE must start from a Vertep page.
 */
@Component({
  selector: 'app-lab-entry',
  standalone: true,
  template: `
    <div class="p-6 text-sm text-slate-700 dark:text-slate-300" data-testid="lab-entry">
      @if (missing()) {
        <p data-testid="lab-entry-missing">Лабораторію не налаштовано: вкажіть адресу панелі LAB у «Налаштування → Загальне».</p>
      } @else {
        <p>Перехід до лабораторії…</p>
      }
    </div>
  `,
})
export class LabEntryComponent implements OnInit {
  private readonly http = inject(HttpClient);
  readonly missing = signal(false);

  ngOnInit(): void {
    this.http.get<{ url: string | null }>('/api/lab/panel').subscribe({
      next: ({ url }) => url
        ? window.location.assign('/api/lab/login?return_to=' + encodeURIComponent(url + '/'))
        : this.missing.set(true),
      error: () => this.missing.set(true),
    });
  }
}
