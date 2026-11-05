import { Component, effect, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { PostSetupState, SystemApiService } from '../core/api/system.api';
import { PolicyService } from '../core/services/policy.service';

/** Shown after login while an update or install still needs an administrator's hand. */
@Component({
  selector: 'app-post-setup-dialog',
  standalone: true,
  imports: [CommonModule, FormsModule],
  template: `
    <div *ngIf="state() as current" class="fixed inset-0 z-40 flex items-center justify-center bg-black/50 p-4"
         role="dialog" aria-modal="true" aria-labelledby="post-setup-title" data-testid="post-setup-dialog">
      <div class="bg-white dark:bg-slate-800 rounded-xl shadow-xl w-full max-w-lg p-5 space-y-5 overflow-y-auto max-h-full">
        <h2 id="post-setup-title" class="text-lg font-semibold">Завершіть налаштування</h2>
        <section *ngIf="current.telegram" class="space-y-2" data-testid="post-setup-telegram">
          <h3 class="font-medium">Доступ до Telegram-бота</h3>
          <p class="text-sm text-slate-600 dark:text-slate-300">Бот нікому не відповідає, доки не вказано, кому це дозволено. Кілька ідентифікаторів чатів розділяйте комами.</p>
          <label class="block text-sm">Чати адміністраторів
            <input class="mt-1 w-full border rounded px-2 py-1 dark:bg-slate-700" data-testid="post-setup-admins" [(ngModel)]="admins" />
          </label>
          <label class="block text-sm">Інші дозволені чати (необов'язково)
            <input class="mt-1 w-full border rounded px-2 py-1 dark:bg-slate-700" data-testid="post-setup-allowed" [(ngModel)]="allowed" />
          </label>
          <button class="px-3 py-1 bg-emerald-600 text-white rounded text-sm disabled:opacity-50" data-testid="post-setup-telegram-save"
                  [disabled]="busy() || !admins.trim()" (click)="saveTelegram()">Зберегти</button>
        </section>
        <section *ngIf="current.backup_key" class="space-y-2" data-testid="post-setup-key">
          <h3 class="font-medium">Ключ резервних копій</h3>
          <p class="text-sm text-slate-600 dark:text-slate-300">Ключ не входить у резервні копії. Якщо сервер буде втрачено, без цього файлу їх не відновити. Збережіть його поза сервером і нікому не передавайте.</p>
          <button class="px-3 py-1 border rounded text-sm disabled:opacity-50" data-testid="post-setup-key-download"
                  [disabled]="busy()" (click)="downloadKey()">Завантажити ключ</button>
          <label class="flex items-center gap-2 text-sm">
            <input type="checkbox" data-testid="post-setup-key-saved" [disabled]="!downloaded()" [(ngModel)]="saved" />
            Я зберіг ключ у безпечному місці поза сервером
          </label>
          <button class="px-3 py-1 bg-emerald-600 text-white rounded text-sm disabled:opacity-50" data-testid="post-setup-key-confirm"
                  [disabled]="busy() || !saved" (click)="confirmKey()">Підтвердити</button>
        </section>
        <p *ngIf="error()" class="text-sm text-red-600" data-testid="post-setup-error">{{ error() }}</p>
        <div class="text-right">
          <button class="text-sm text-slate-500 hover:text-slate-700" data-testid="post-setup-later" (click)="state.set(null)">Нагадати пізніше</button>
        </div>
      </div>
    </div>
  `,
})
export class PostSetupDialogComponent {
  state = signal<PostSetupState | null>(null);
  busy = signal(false);
  downloaded = signal(false);
  error = signal<string | null>(null);
  admins = '';
  allowed = '';
  saved = false;

  private checked = false;

  constructor(private system: SystemApiService, private policy: PolicyService) {
    // Until the policy has loaded everyone looks like a viewer, so wait for it.
    effect(() => {
      if (this.checked || !this.policy.stateLoaded() || !this.policy.can('update_settings').allowed) return;
      this.checked = true;
      this.system.postSetup().subscribe({ next: (state) => this.show(state), error: () => {} });
    });
  }

  private show(state: PostSetupState): void { this.state.set(state.pending ? state : null); }

  private fail(err: { error?: { detail?: string }; message?: string }): void {
    this.busy.set(false);
    this.error.set(err?.error?.detail || err?.message || 'Не вдалося виконати дію');
  }

  private reload(): void {
    this.system.postSetup().subscribe({
      next: (state) => { this.busy.set(false); this.error.set(null); this.show(state); },
      error: (err) => this.fail(err),
    });
  }

  saveTelegram(): void {
    this.busy.set(true);
    this.system.saveTelegramAccess(this.allowed.trim(), this.admins.trim()).subscribe({
      next: () => this.reload(),
      error: (err) => this.fail(err),
    });
  }

  downloadKey(): void {
    this.busy.set(true);
    this.system.backupKey().subscribe({
      next: (blob) => {
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = 'vertep-backup.key';
        link.click();
        URL.revokeObjectURL(url);
        this.busy.set(false);
        this.downloaded.set(true);
      },
      error: (err) => this.fail(err),
    });
  }

  confirmKey(): void {
    this.busy.set(true);
    this.system.acknowledgeBackupKey().subscribe({
      next: (state) => { this.busy.set(false); this.show(state); },
      error: (err) => this.fail(err),
    });
  }
}
