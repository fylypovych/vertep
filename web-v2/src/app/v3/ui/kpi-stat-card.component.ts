import { ChangeDetectionStrategy, Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterModule } from '@angular/router';
import { TPipe } from '../../core/i18n/t.pipe';

export type KpiAccent = 'emerald' | 'coral' | 'amber' | 'sky' | 'danger' | 'slate';

const ACCENT_STYLES: Record<KpiAccent, { bg: string; fg: string }> = {
  emerald: { bg: 'var(--v3-accent-soft)', fg: 'var(--v3-accent)' },
  coral:   { bg: 'var(--v3-coral-soft)',  fg: 'var(--v3-coral)' },
  amber:   { bg: 'var(--v3-amber-soft)',  fg: 'var(--v3-amber)' },
  sky:     { bg: 'var(--v3-sky-soft)',    fg: 'var(--v3-sky)' },
  danger:  { bg: 'var(--v3-danger-soft)', fg: 'var(--v3-danger)' },
  slate:   { bg: '#efede7',               fg: '#66615b' },
};

@Component({
  selector: 'app-v3-kpi-card',
  standalone: true,
  imports: [CommonModule, RouterModule, TPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (link) {
      <a [routerLink]="link" [queryParams]="queryParams"
         class="v3-paper-card block p-5 transition-transform duration-150 hover:-translate-y-0.5"
         [attr.data-testid]="testId || null">
        <ng-container *ngTemplateOutlet="cardBody" />
      </a>
    } @else {
      <div class="v3-paper-card p-5" [attr.data-testid]="testId || null">
        <ng-container *ngTemplateOutlet="cardBody" />
      </div>
    }

    <ng-template #cardBody>
      <div class="flex items-center justify-between gap-4">
        <div class="w-12 h-12 rounded-xl flex items-center justify-center flex-shrink-0"
             [style.background-color]="accentStyle.bg"
             [style.color]="accentStyle.fg">
          @switch (icon) {
            @case ('workers') {
              <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M5 12h14M5 12a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v4a2 2 0 01-2 2M5 12a2 2 0 00-2 2v4a2 2 0 002 2h14a2 2 0 002-2v-4a2 2 0 00-2-2"/></svg>
            }
            @case ('active') {
              <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>
            }
            @case ('queue') {
              <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
            }
            @case ('health') {
              <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
            }
            @default {
              <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
            }
          }
        </div>
        <div class="text-right min-w-0">
          <p class="text-xs font-medium truncate" style="color: var(--v3-text-muted)">{{ label | t }}</p>
          <p class="text-2xl font-semibold tracking-tight mt-0.5 truncate" [style.color]="valueColor || 'var(--v3-text)'">{{ value | t }}</p>
        </div>
      </div>
      @if (footer) {
        <div class="mt-4 pt-3 border-t text-xs flex items-center justify-between gap-2" style="border-color: var(--v3-border); color: var(--v3-text-subtle)">
          <span class="truncate">{{ footer | t }}</span>
          @if (link) {
            <span class="text-[11px] font-medium flex-shrink-0" [style.color]="accentStyle.fg">→</span>
          }
        </div>
      }
    </ng-template>
  `,
})
export class KpiStatCardComponent {
  @Input({ required: true }) label!: string;
  @Input({ required: true }) value!: string;
  @Input() footer = '';
  @Input() accent: KpiAccent = 'emerald';
  @Input() icon: 'workers' | 'active' | 'queue' | 'health' | 'info' = 'info';
  @Input() link?: string | string[];
  @Input() queryParams?: Record<string, string>;
  @Input() valueColor?: string;
  @Input() testId = '';

  get accentStyle(): { bg: string; fg: string } {
    return ACCENT_STYLES[this.accent] ?? ACCENT_STYLES.emerald;
  }
}
