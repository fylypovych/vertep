import { Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';

@Component({
  selector: 'app-empty-state',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="text-center text-slate-500 py-6" data-testid="empty-state">
      <p>{{ message || 'Немає даних для відображення' }}</p>
    </div>
  `,
})
export class EmptyStateComponent {
  @Input() message: string | null = null;
}
