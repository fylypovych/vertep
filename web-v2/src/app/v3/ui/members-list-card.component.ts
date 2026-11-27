import { ChangeDetectionStrategy, Component, EventEmitter, Input, Output } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterModule } from '@angular/router';
import { TPipe } from '../../core/i18n/t.pipe';

export interface MemberListItem {
  id: string;
  name: string;
  subtitle?: string;
  avatarUrl?: string | null;
  initials?: string;
  statusLabel?: string;
  statusTone?: 'ok' | 'warn' | 'muted' | 'danger';
  badge?: string;
  route?: string | string[];
  queryParams?: Record<string, string>;
  actionLabel?: string;
}

/**
 * Paper Dashboard "Team Members" pattern adapted across Vertep:
 * brand characters, brand moderators, channels, and active cluster workers.
 */
@Component({
  selector: 'app-v3-members-list',
  standalone: true,
  imports: [CommonModule, RouterModule, TPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div [class]="embedded ? '' : 'v3-paper-card p-5'" [attr.data-testid]="testId || 'v3-members-list'">
      @if (title) {
        <div class="flex items-center justify-between gap-2 mb-3">
          <div>
            <h4 class="text-sm font-semibold" style="color: var(--v3-text)">{{ title | t }}</h4>
            @if (subtitle) {
              <p class="text-xs mt-0.5" style="color: var(--v3-text-muted)">{{ subtitle | t }}</p>
            }
          </div>
          <span class="text-xs font-medium px-2 py-0.5 rounded-full" style="background-color: #efede7; color: var(--v3-text-muted)">
            {{ items.length }}
          </span>
        </div>
      }

      @if (items.length === 0) {
        <p class="text-xs py-3" style="color: var(--v3-text-subtle)" data-testid="v3-members-empty">{{ emptyText | t }}</p>
      } @else {
        <ul class="divide-y" style="--tw-divide-opacity: 1; border-color: var(--v3-border)">
          @for (item of items; track item.id) {
            <li class="py-2.5 first:pt-1 last:pb-1 flex items-center gap-3" [style.border-color]="'var(--v3-border)'"
                [attr.data-testid]="'v3-member-' + item.id">
              <div class="w-9 h-9 rounded-full overflow-hidden flex items-center justify-center text-xs font-semibold flex-shrink-0 border"
                   style="background-color: var(--v3-surface-subtle); border-color: var(--v3-border); color: var(--v3-accent)">
                @if (item.avatarUrl && !failedAvatars.has(item.id)) {
                  <img [src]="item.avatarUrl" [alt]="item.name" (error)="markAvatarFailed(item.id)"
                       class="w-full h-full object-cover">
                } @else {
                  <span>{{ initialsFor(item) }}</span>
                }
              </div>

              <div class="min-w-0 flex-1">
                <div class="flex items-center gap-2">
                  @if (item.route) {
                    <a [routerLink]="item.route" [queryParams]="item.queryParams" class="text-sm font-medium truncate hover:underline" style="color: var(--v3-text)">
                      {{ item.name }}
                    </a>
                  } @else {
                    <span class="text-sm font-medium truncate" style="color: var(--v3-text)">{{ item.name }}</span>
                  }
                  @if (item.badge) {
                    <span class="px-1.5 py-0.5 rounded text-[11px] font-medium flex-shrink-0"
                          style="background-color: var(--v3-sky-soft); color: #236e73">
                      {{ item.badge | t }}
                    </span>
                  }
                </div>
                <div class="flex items-center gap-2 text-xs mt-0.5">
                  @if (item.statusLabel) {
                    <span class="inline-flex items-center gap-1" [style.color]="statusColor(item.statusTone)">
                      <span class="w-1.5 h-1.5 rounded-full" [style.background-color]="statusColor(item.statusTone)"></span>
                      {{ item.statusLabel | t }}
                    </span>
                  }
                  @if (item.subtitle) {
                    <span class="truncate font-mono text-[11px]" style="color: var(--v3-text-subtle)">{{ item.subtitle }}</span>
                  }
                </div>
              </div>

              <div class="flex-shrink-0">
                @if (item.route && item.actionLabel) {
                  <a [routerLink]="item.route"
                     [queryParams]="item.queryParams"
                     class="inline-flex items-center px-2.5 py-1 rounded-full text-xs font-medium border transition-colors hover:bg-[#faf9f6]"
                     style="border-color: var(--v3-border-strong); color: var(--v3-accent)">
                    {{ item.actionLabel | t }}
                  </a>
                } @else if (item.actionLabel) {
                  <button type="button" (click)="actionClick.emit(item)"
                          class="inline-flex items-center px-2.5 py-1 rounded-full text-xs font-medium border transition-colors hover:bg-[#faf9f6]"
                          style="border-color: var(--v3-border-strong); color: var(--v3-accent)">
                    {{ item.actionLabel | t }}
                  </button>
                }
              </div>
            </li>
          }
        </ul>
      }
    </div>
  `,
})
export class MembersListCardComponent {
  @Input() title = '';
  @Input() subtitle = '';
  @Input() emptyText = 'Немає записів';
  @Input() items: MemberListItem[] = [];
  @Input() embedded = false;
  @Input() testId = '';
  @Output() actionClick = new EventEmitter<MemberListItem>();

  failedAvatars = new Set<string>();

  markAvatarFailed(id: string): void {
    this.failedAvatars.add(id);
  }

  initialsFor(item: MemberListItem): string {
    if (item.initials) return item.initials.slice(0, 2).toUpperCase();
    const parts = (item.name || item.id || '?').trim().split(/\s+/);
    if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
    return (parts[0]?.slice(0, 2) || '?').toUpperCase();
  }

  statusColor(tone?: MemberListItem['statusTone']): string {
    switch (tone) {
      case 'ok':     return 'var(--v3-accent)';
      case 'warn':   return 'var(--v3-amber)';
      case 'danger': return 'var(--v3-danger)';
      default:       return 'var(--v3-text-subtle)';
    }
  }
}
