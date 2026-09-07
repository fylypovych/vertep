import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ConfirmService, ConfirmOptions } from '../core/services/confirm.service';

@Component({
  selector: 'app-confirm-dialog',
  standalone: true,
  imports: [CommonModule],
  template: `
    @if (visible) {
      <div class="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
        <div class="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-6 max-w-sm w-full mx-4 shadow-xl">
          <h3 class="text-lg font-semibold text-slate-900 dark:text-slate-100 mb-2">{{ options.title }}</h3>
          <p class="text-sm text-slate-600 dark:text-slate-400 mb-6">{{ options.message }}</p>
          <div class="flex gap-3 justify-end">
            <button (click)="onCancel()"
                    class="px-4 py-2 text-sm font-medium text-slate-700 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-700 rounded-lg transition-colors">
              {{ options.cancelText || 'Скасувати' }}
            </button>
            <button (click)="onConfirm()"
                    class="px-4 py-2 text-sm font-medium text-white bg-red-600 hover:bg-red-700 rounded-lg transition-colors">
              {{ options.confirmText || 'Підтвердити' }}
            </button>
          </div>
        </div>
      </div>
    }
  `,
})
export class ConfirmDialogComponent implements OnInit {
  visible = false;
  options: ConfirmOptions = { title: '', message: '' };

  constructor(private confirmService: ConfirmService) {}

  ngOnInit(): void {
    this.confirmService.open$.subscribe((opts) => {
      this.options = opts;
      this.visible = true;
    });
  }

  onConfirm(): void {
    this.visible = false;
    this.confirmService.resolve(true);
  }

  onCancel(): void {
    this.visible = false;
    this.confirmService.resolve(false);
  }
}
