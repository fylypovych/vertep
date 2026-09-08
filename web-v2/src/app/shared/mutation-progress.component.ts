import { Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';

@Component({
  selector: 'app-mutation-progress',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="flex items-center gap-2 text-sm text-slate-600" data-testid="mutation-progress">
      @if (loading) {
        <div class="w-4 h-4 border-2 border-emerald-500 border-t-transparent rounded-full animate-spin"></div>
        <span>{{ message || 'Обробка...' }}</span>
      } @else if (success) {
        <span class="text-emerald-600">{{ successMessage || 'Успішно' }}</span>
      } @else if (error) {
        <span class="text-red-600">{{ error }}</span>
      }
    </div>
  `,
})
export class MutationProgressComponent {
  @Input() loading = false;
  @Input() success = false;
  @Input() error: string | null = null;
  @Input() message: string | null = null;
  @Input() successMessage: string | null = null;
}
