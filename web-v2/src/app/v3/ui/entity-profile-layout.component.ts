import { ChangeDetectionStrategy, Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';
import { TPipe } from '../../core/i18n/t.pipe';

export interface ProfileStat {
  label: string;
  value: string;
}

export interface ProfileBadge {
  label: string;
  tone?: 'emerald' | 'coral' | 'sky' | 'amber' | 'slate';
}

/**
 * Paper Dashboard two-column profile pattern adapted across Vertep entities
 * (Users and Characters): identity card on the left, structured editing forms on the right.
 */
@Component({
  selector: 'app-v3-entity-profile',
  standalone: true,
  imports: [CommonModule, TPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="grid grid-cols-1 lg:grid-cols-12 gap-6 items-start" [attr.data-testid]="testId || 'v3-entity-profile'">
      <!-- Left column: Identity card + optional extra block -->
      <div class="lg:col-span-4 space-y-5">
        <article class="v3-paper-card overflow-hidden" data-testid="v3-profile-identity-card">
          <div class="h-28 w-full relative"
               [style.background]="coverGradient">
            @if (statusLabel) {
              <span class="absolute top-3 right-3 inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium bg-white/95 shadow-xs"
                    [style.color]="statusTone === 'ok' ? 'var(--v3-accent)' : statusTone === 'warn' ? 'var(--v3-amber)' : 'var(--v3-text-muted)'"
                    data-testid="v3-profile-status">
                <span class="w-1.5 h-1.5 rounded-full"
                      [style.background-color]="statusTone === 'ok' ? 'var(--v3-accent)' : statusTone === 'warn' ? 'var(--v3-amber)' : 'var(--v3-text-subtle)'"></span>
                {{ statusLabel | t }}
              </span>
            }
          </div>

          <div class="px-5 pb-5 text-center -mt-12 relative">
            <div class="w-24 h-24 mx-auto rounded-full border-4 border-white shadow-sm overflow-hidden flex items-center justify-center text-2xl font-bold select-none"
                 style="background-color: var(--v3-accent-soft); color: var(--v3-accent)">
              @if (avatarUrl && !avatarFailed) {
                <img [src]="avatarUrl" [alt]="displayName" (error)="avatarFailed = true"
                     class="w-full h-full object-cover" data-testid="v3-profile-avatar-img">
              } @else {
                <span data-testid="v3-profile-avatar-initials">{{ computedInitials }}</span>
              }
            </div>

            <h2 class="text-lg font-semibold mt-3 leading-snug break-words" style="color: var(--v3-text)" data-testid="v3-profile-title">
              {{ displayName | t }}
            </h2>
            @if (subtitle) {
              <p class="text-xs font-mono mt-0.5 break-all" style="color: var(--v3-text-muted)" data-testid="v3-profile-subtitle">{{ subtitle }}</p>
            }

            @if (badges.length) {
              <div class="flex flex-wrap justify-center gap-1.5 mt-3">
                @for (badge of badges; track badge.label) {
                  <span class="px-2.5 py-0.5 rounded-full text-xs font-medium" [style]="badgeStyle(badge.tone)">
                    {{ badge.label | t }}
                  </span>
                }
              </div>
            }

            @if (description) {
              <p class="text-xs leading-relaxed mt-3 px-2" style="color: var(--v3-text-muted)">{{ description | t }}</p>
            }
          </div>

          @if (stats.length) {
            <div class="border-t px-4 py-3.5 grid gap-2 text-center"
                 [style.border-color]="'var(--v3-border)'"
                 [style.grid-template-columns]="'repeat(' + stats.length + ', minmax(0, 1fr))'"
                 data-testid="v3-profile-stats">
              @for (stat of stats; track stat.label) {
                <div class="min-w-0">
                  <div class="text-sm font-semibold truncate" style="color: var(--v3-text)">{{ stat.value | t }}</div>
                  <div class="text-[11px] truncate mt-0.5" style="color: var(--v3-text-subtle)">{{ stat.label | t }}</div>
                </div>
              }
            </div>
          }
        </article>

        <ng-content select="[profile-left-extra]"></ng-content>
      </div>

      <!-- Right column: Forms and editable sections -->
      <div class="lg:col-span-8 space-y-5 min-w-0">
        <ng-content></ng-content>
      </div>
    </div>
  `,
})
export class EntityProfileLayoutComponent {
  @Input({ required: true }) displayName!: string;
  @Input() subtitle = '';
  @Input() description = '';
  @Input() avatarUrl: string | null = null;
  @Input() initials = '';
  @Input() statusLabel = '';
  @Input() statusTone: 'ok' | 'warn' | 'muted' = 'ok';
  @Input() accent: 'emerald' | 'coral' | 'sky' = 'emerald';
  @Input() badges: ProfileBadge[] = [];
  @Input() stats: ProfileStat[] = [];
  @Input() testId = '';

  avatarFailed = false;

  get computedInitials(): string {
    if (this.initials) return this.initials.slice(0, 2).toUpperCase();
    const parts = (this.displayName || 'V').trim().split(/\s+/);
    if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
    return (parts[0]?.slice(0, 2) || 'V').toUpperCase();
  }

  get coverGradient(): string {
    if (this.accent === 'coral') {
      return 'linear-gradient(135deg, #f3d6c8 0%, #e7b8a3 55%, #d6967a 100%)';
    }
    if (this.accent === 'sky') {
      return 'linear-gradient(135deg, #d7ecef 0%, #a9d5db 55%, #79b8c2 100%)';
    }
    return 'linear-gradient(135deg, #d9eee6 0%, #aed8c8 55%, #7cc0a8 100%)';
  }

  badgeStyle(tone?: ProfileBadge['tone']): Record<string, string> {
    switch (tone) {
      case 'coral':   return { backgroundColor: 'var(--v3-coral-soft)',  color: '#b8512b' };
      case 'sky':     return { backgroundColor: 'var(--v3-sky-soft)',    color: '#236e73' };
      case 'amber':   return { backgroundColor: 'var(--v3-amber-soft)',  color: '#a35d15' };
      case 'slate':   return { backgroundColor: '#efede7',               color: '#66615b' };
      default:        return { backgroundColor: 'var(--v3-accent-soft)', color: '#0d6e50' };
    }
  }
}
