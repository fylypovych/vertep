import { ChangeDetectionStrategy, Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';
import { TPipe } from '../../core/i18n/t.pipe';

@Component({
  selector: 'app-v3-paper-card',
  standalone: true,
  imports: [CommonModule, TPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section class="v3-paper-card flex flex-col" [attr.data-testid]="testId || null">
      @if (title || subtitle || eyebrow || hasHeader) {
        <header class="flex items-start justify-between gap-4 px-5 pt-5" [class.pb-3]="!headerDivider" [class.pb-4]="headerDivider" [class.border-b]="headerDivider" [style.border-color]="'var(--v3-border)'">
          <div class="min-w-0">
            @if (eyebrow) {
              <p class="text-[11px] font-semibold uppercase tracking-wider mb-0.5" style="color: var(--v3-text-subtle)">{{ eyebrow | t }}</p>
            }
            @if (title) {
              <h3 class="text-base font-semibold tracking-tight leading-snug" style="color: var(--v3-text)">{{ title | t }}</h3>
            }
            @if (subtitle) {
              <p class="text-xs mt-1 leading-relaxed" style="color: var(--v3-text-muted)">{{ subtitle | t }}</p>
            }
          </div>
          <div class="flex items-center gap-2 flex-shrink-0">
            <ng-content select="[card-actions]"></ng-content>
          </div>
        </header>
      }
      <div [class]="flush ? '' : 'px-5 pb-5 pt-2'">
        <ng-content></ng-content>
      </div>
      <ng-content select="[card-footer]"></ng-content>
    </section>
  `,
})
export class PaperCardComponent {
  @Input() title = '';
  @Input() subtitle = '';
  @Input() eyebrow = '';
  @Input() testId = '';
  @Input() flush = false;
  @Input() headerDivider = false;
  @Input() hasHeader = false;
}
