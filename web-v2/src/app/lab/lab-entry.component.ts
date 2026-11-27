import { HttpClient } from '@angular/common/http';
import { Component, OnInit, inject, signal } from '@angular/core';
import { DomSanitizer, SafeResourceUrl } from '@angular/platform-browser';

/**
 * «Лабораторія» (#491 L2/L3): the LAB panel opens inside Vertep, in a frame.
 *
 * The page asks CORE (same origin, session cookie) for the panel address with a
 * short-lived signed assertion of the signed-in administrator and frames it; the LAB
 * lets only the connected Vertep frame it (frame-ancestors). «Відкрити окремо» keeps the
 * former full-page entry through `/api/lab/login`. The LAB panel's «Увійти через Vertep»
 * lands here too.
 */
@Component({
  selector: 'app-lab-entry',
  standalone: true,
  template: `
    <div class="flex flex-col h-full min-h-[calc(100vh-8rem)] text-sm text-slate-700 dark:text-slate-300" data-testid="lab-entry">
      @if (missing()) {
        <p class="p-6" data-testid="lab-entry-missing">Лабораторію не налаштовано: вкажіть адресу панелі LAB у «Налаштування → Загальне».</p>
      } @else if (frame(); as src) {
        <div class="flex flex-wrap items-center gap-3 px-4 py-2 border-b border-slate-200 dark:border-slate-700 text-xs text-slate-500">
          <span>Якщо панель нижче порожня, браузер ще не довіряє сертифікату лабораторії — відкрийте її окремо й прийміть сертифікат один раз.</span>
          <a class="underline text-emerald-700" [href]="separate()" target="_blank" rel="noopener" data-testid="lab-open-separately">Відкрити окремо</a>
        </div>
        <iframe class="flex-1 w-full border-0" [src]="src" title="Лабораторія" data-testid="lab-frame"
                referrerpolicy="no-referrer"></iframe>
      } @else {
        <p class="p-6">Відкриваємо лабораторію…</p>
      }
    </div>
  `,
})
export class LabEntryComponent implements OnInit {
  private readonly http = inject(HttpClient);
  private readonly sanitizer = inject(DomSanitizer);
  readonly missing = signal(false);
  readonly frame = signal<SafeResourceUrl | null>(null);
  readonly separate = signal('');

  ngOnInit(): void {
    this.http.get<{ url: string | null; panel?: string }>('/api/lab/frame').subscribe({
      next: ({ url, panel }) => {
        // Only an https panel address from CORE is framed (the assertion rides in the fragment).
        if (!url || !panel || !/^https:\/\/[^\s"<>]+$/.test(url)) {
          this.missing.set(true);
          return;
        }
        this.separate.set('/api/lab/login?return_to=' + encodeURIComponent(panel + '/'));
        this.frame.set(this.sanitizer.bypassSecurityTrustResourceUrl(url));
      },
      error: () => this.missing.set(true),
    });
  }
}
