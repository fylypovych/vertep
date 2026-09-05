import { Component, Inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ConfirmService, ConfirmOptions } from '../core/services/confirm.service';

@Component({
  selector: 'app-confirm-dialog',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="fixed inset-0 bg-black/50 flex items-center justify-center z-50" *ngIf="visible">
      <div class="bg-white rounded-xl border border-slate-200 p-6 max-w-sm w-full mx-4">
        <h3 class="text-lg font-semibold text-slate-900 mb-2">{{ options.title }}</h3>
        <p class="text-sm text-slate-600 mb-6">{{ options.message }}</p>
        <div class="flex gap-3 justify-end">
          <button (click)="cancel()" class="px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-100 rounded-lg">Скасувати</button>
          <button (click)="confirm()" class="px-4 py-2 text-sm font-medium text-white bg-red-600 hover:bg-red-700 rounded-lg">Підтвердити</button>
        </div>
      </div>
    </div>
  `,
})
export class ConfirmDialogComponent {
  visible = false;
  options: ConfirmOptions = { title: '', message: '' };
  private resolve?: (value: boolean) => void;

  constructor(private confirmService: ConfirmService) {
    this.confirmService['subject'].subscribe({
      next: () => {},
    });
  }

  async open(options: ConfirmOptions): Promise<boolean> {
    this.options = options;
    this.visible = true;
    return new Promise((resolve) => {
      this.resolve = resolve;
    });
  }

  confirm(): void {
    this.visible = false;
    this.resolve?.(true);
  }

  cancel(): void {
    this.visible = false;
    this.resolve?.(false);
  }
}
