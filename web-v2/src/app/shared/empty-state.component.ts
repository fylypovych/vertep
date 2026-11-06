import { TPipe } from '../core/i18n/t.pipe';
import { Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';

@Component({
  selector: 'app-empty-state',
  standalone: true,
  imports: [TPipe, CommonModule],
  template: `
    <div class="text-center text-slate-500 py-6" data-testid="empty-state">
      <p>{{ (message || 'Немає даних для відображення') | t }}</p>
    </div>
  `,
})
export class EmptyStateComponent {
  @Input() message: string | null = null;
}
