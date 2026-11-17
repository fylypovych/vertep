import { Component, OnDestroy, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ActivatedRoute } from '@angular/router';
import { DomSanitizer, SafeResourceUrl } from '@angular/platform-browser';
import { Subscription, of } from 'rxjs';
import { map, switchMap } from 'rxjs/operators';
import { TPipe } from '../core/i18n/t.pipe';
import { ModulesApiService } from '../core/api/modules.api';

type ExtensionState = 'loading' | 'ready' | 'unavailable';

/**
 * Hosts one module UI extension in a sandboxed iframe (scripts only, opaque
 * origin): it cannot read the panel DOM, cookies or session. A missing,
 * disabled or failing extension shows a notice here; the rest of the panel
 * keeps working.
 */
@Component({
  selector: 'app-module-extension',
  standalone: true,
  imports: [CommonModule, TPipe],
  template: `
    <div class="p-6 space-y-4" data-testid="module-extension">
      @if (state() === 'loading') {
        <p class="text-sm text-slate-500">{{ 'Завантаження розширення модуля…' | t }}</p>
      } @else if (state() === 'unavailable') {
        <div class="rounded-lg border border-amber-300 bg-amber-50 dark:bg-amber-900/20 p-4 text-sm" data-testid="module-extension-unavailable" role="alert">
          {{ 'Розширення модуля недоступне. Інші розділи панелі працюють.' | t }}
        </div>
      } @else {
        <iframe data-testid="module-extension-frame" class="w-full h-[70vh] rounded-lg border border-slate-200 dark:border-slate-700"
                sandbox="allow-scripts" referrerpolicy="no-referrer" [src]="frameUrl()" [title]="moduleId()"></iframe>
      }
    </div>
  `,
})
export class ModuleExtensionComponent implements OnInit, OnDestroy {
  state = signal<ExtensionState>('loading');
  frameUrl = signal<SafeResourceUrl | null>(null);
  moduleId = signal('');
  private subscription?: Subscription;

  constructor(private route: ActivatedRoute, private modules: ModulesApiService, private sanitizer: DomSanitizer) {}

  ngOnInit(): void {
    this.subscription = this.route.paramMap.pipe(
      map(params => params.get('moduleId') || ''),
      switchMap(moduleId => {
        this.moduleId.set(moduleId);
        this.state.set('loading');
        return this.modules.uiExtensions().pipe(
          map(list => list.find(item => item.module_id === moduleId && item.url.startsWith('/api/modules/ui/'))),
          switchMap(extension => extension
            ? this.modules.entryAvailable(extension.url).pipe(map(ok => ok ? extension.url : null))
            : of(null)));
      }),
    ).subscribe(url => {
      if (url) {
        // Only same-origin, CORE-verified module UI URLs are ever framed.
        this.frameUrl.set(this.sanitizer.bypassSecurityTrustResourceUrl(url));
        this.state.set('ready');
      } else {
        this.state.set('unavailable');
      }
    });
  }

  ngOnDestroy(): void {
    this.subscription?.unsubscribe();
  }
}
