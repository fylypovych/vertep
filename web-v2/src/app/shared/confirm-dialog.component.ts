import { Component, DestroyRef, OnInit, ElementRef, inject, signal, effect, viewChild } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { CommonModule } from '@angular/common';
import { ConfirmService, ConfirmOptions } from '../core/services/confirm.service';

@Component({
  selector: 'app-confirm-dialog',
  standalone: true,
  imports: [CommonModule],
  template: `
    @if (visible()) {
      <div class="fixed inset-0 bg-black/50 flex items-center justify-center z-50" role="dialog" aria-modal="true" [attr.aria-labelledby]="'confirm-title'" (keydown.escape)="onCancel()">
        <div #dialogPanel
             class="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-6 max-w-sm w-full mx-4 shadow-xl"
             (click)="$event.stopPropagation()"
             (keydown)="onKeydown($event)">
          <h3 id="confirm-title" class="text-lg font-semibold text-slate-900 dark:text-slate-100 mb-2">{{ options().title }}</h3>
          <p class="text-sm text-slate-600 dark:text-slate-400 mb-6">{{ options().message }}</p>
          <div class="flex gap-3 justify-end">
            <button #cancelBtn (click)="onCancel()"
                    class="px-4 py-2 text-sm font-medium text-slate-700 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-700 rounded-lg transition-colors">
              {{ options().cancelText || 'Скасувати' }}
            </button>
            <button #confirmBtn (click)="onConfirm()"
                    class="px-4 py-2 text-sm font-medium text-white bg-red-600 hover:bg-red-700 rounded-lg transition-colors">
              {{ options().confirmText || 'Підтвердити' }}
            </button>
          </div>
        </div>
      </div>
    }
  `,
})
export class ConfirmDialogComponent implements OnInit {
  visible = signal(false);
  options = signal<ConfirmOptions>({ title: '', message: '' });
  private destroyRef = inject(DestroyRef);
  private previouslyFocused: HTMLElement | null = null;

  confirmBtn = viewChild<ElementRef<HTMLButtonElement>>('confirmBtn');
  cancelBtn = viewChild<ElementRef<HTMLButtonElement>>('cancelBtn');

  private _focusEffect = effect(() => {
    if (this.visible()) {
      this.confirmBtn()?.nativeElement?.focus();
    }
  });

  constructor(private confirmService: ConfirmService) {}

  ngOnInit(): void {
    this.confirmService.open$.pipe(takeUntilDestroyed(this.destroyRef)).subscribe((opts) => {
      this.previouslyFocused = document.activeElement as HTMLElement | null;
      this.options.set(opts);
      this.visible.set(true);
    });
  }

  onConfirm(): void {
    this.visible.set(false);
    this.confirmService.resolve(true);
    this._restoreFocus();
  }

  onCancel(): void {
    this.visible.set(false);
    this.confirmService.resolve(false);
    this._restoreFocus();
  }

  onKeydown(event: KeyboardEvent): void {
    if (event.key !== 'Tab') return;
    const confirmEl = this.confirmBtn()?.nativeElement;
    const cancelEl = this.cancelBtn()?.nativeElement;
    if (!confirmEl || !cancelEl) return;
    const focusables = [cancelEl, confirmEl];
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    if (event.shiftKey) {
      if (document.activeElement === first) { event.preventDefault(); last.focus(); }
    } else {
      if (document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  }

  private _restoreFocus(): void {
    if (this.previouslyFocused) {
      this.previouslyFocused.focus();
      this.previouslyFocused = null;
    }
  }
}
