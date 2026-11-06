import { TPipe } from '../core/i18n/t.pipe';
import { Component, Input, Output, EventEmitter } from '@angular/core';
import { CommonModule } from '@angular/common';

@Component({
  selector: 'app-error-state',
  standalone: true,
  imports: [TPipe, CommonModule],
  template: `
    <div class="bg-red-50 border border-red-200 rounded-xl p-5" data-testid="error-state">
      <p class="text-red-700">{{ (message || 'Не вдалося завантажити дані') | t }}</p>
      @if (showRetry) {
        <button (click)="retry.emit()" class="mt-2 text-sm text-red-600 hover:text-red-700 font-medium">{{ 'Повторити' | t }}</button>
      }
    </div>
  `,
})
export class ErrorStateComponent {
  @Input() message: string | null = null;
  @Input() showRetry = true;
  @Output() retry = new EventEmitter<void>();
}
