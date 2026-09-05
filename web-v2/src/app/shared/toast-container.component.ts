import { Component, Inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ToastService, Toast } from '../core/services/toast.service';

@Component({
  selector: 'app-toast-container',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="fixed top-4 right-4 z-50 space-y-2">
      @for (toast of toasts; track toast.id) {
        <div class="px-4 py-3 rounded-lg border shadow-lg text-sm font-medium"
             [class.bg-emerald-50]="toast.type === 'success'"
             [class.border-emerald-200]="toast.type === 'success'"
             [class.text-emerald-800]="toast.type === 'success'"
             [class.bg-red-50]="toast.type === 'error'"
             [class.border-red-200]="toast.type === 'error'"
             [class.text-red-800]="toast.type === 'error'"
             [class.bg-blue-50]="toast.type === 'info'"
             [class.border-blue-200]="toast.type === 'info'"
             [class.text-blue-800]="toast.type === 'info'">
          {{ toast.message }}
        </div>
      }
    </div>
  `,
})
export class ToastContainerComponent {
  toasts: Toast[] = [];

  constructor(private toastService: ToastService) {
    this.toastService.toasts$.subscribe((toasts) => {
      this.toasts = toasts;
    });
  }
}
