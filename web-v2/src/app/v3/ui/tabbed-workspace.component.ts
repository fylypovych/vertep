import { ChangeDetectionStrategy, Component, EventEmitter, Input, Output } from '@angular/core';
import { CommonModule } from '@angular/common';
import { TPipe } from '../../core/i18n/t.pipe';

export interface WorkspaceTab {
  id: string;
  label: string;
  badge?: string | number | null;
}

@Component({
  selector: 'app-v3-tabbed-workspace',
  standalone: true,
  imports: [CommonModule, TPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="space-y-5" [attr.data-testid]="testId || 'v3-tabbed-workspace'">
      <div class="v3-paper-card px-3 py-2 flex items-center gap-1.5 overflow-x-auto" role="tablist">
        @for (tab of tabs; track tab.id) {
          <button type="button"
                  role="tab"
                  [attr.aria-selected]="activeTab === tab.id"
                  [attr.data-testid]="'v3-tab-' + tab.id"
                  (click)="tabChange.emit(tab.id)"
                  class="px-3.5 py-2 rounded-lg text-sm font-medium whitespace-nowrap transition-colors flex items-center gap-2"
                  [style.background-color]="activeTab === tab.id ? 'var(--v3-accent-soft)' : 'transparent'"
                  [style.color]="activeTab === tab.id ? 'var(--v3-accent)' : 'var(--v3-text-muted)'">
            <span>{{ tab.label | t }}</span>
            @if (tab.badge !== undefined && tab.badge !== null && tab.badge !== '') {
              <span class="px-1.5 py-0.5 rounded-full text-[11px] font-semibold"
                    [style.background-color]="activeTab === tab.id ? 'var(--v3-accent)' : '#efede7'"
                    [style.color]="activeTab === tab.id ? '#ffffff' : 'var(--v3-text-muted)'">
                {{ tab.badge }}
              </span>
            }
          </button>
        }
      </div>
      <div class="min-w-0">
        <ng-content></ng-content>
      </div>
    </div>
  `,
})
export class TabbedWorkspaceComponent {
  @Input({ required: true }) tabs: WorkspaceTab[] = [];
  @Input({ required: true }) activeTab = '';
  @Input() testId = '';
  @Output() tabChange = new EventEmitter<string>();
}
