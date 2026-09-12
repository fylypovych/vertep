import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { VertepApiService } from '../../core/api.service';
import { ToastService } from '../../core/services/toast.service';
import { ConfirmService } from '../../core/services/confirm.service';
import { BackupInfo } from '../../core/models';
import { VertepDatePipe } from '../../shared/vertep-date.pipe';

@Component({
  selector: 'app-settings-backup',
  standalone: true,
  imports: [CommonModule, VertepDatePipe],
  template: `
    <div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-backup">
      <div class="flex items-center justify-between mb-4">
        <h3 class="text-lg font-semibold text-slate-900">Бекапи</h3>
        <button (click)="createBackup()" data-testid="backup-create-button" class="px-3 py-1.5 text-sm bg-emerald-600 text-white rounded-lg hover:bg-emerald-700">Створити</button>
      </div>
      @if (loading()) {
        <div class="animate-pulse space-y-2"><div class="h-5 bg-slate-200 rounded w-full"></div></div>
      } @else if (error()) {
        <p class="text-red-600">{{ error() }}</p>
      } @else if (backups().length === 0) {
        <p class="text-sm text-slate-500">Бекапів не знайдено</p>
      } @else {
        @if (restoreProgress()) {
          <div class="mb-4 p-3 bg-blue-50 rounded-lg">
            <p class="text-sm text-blue-700">{{ restoreProgress()!.message }}</p>
          </div>
        }
        <div class="space-y-2">
          @for (backup of backups(); track backup.snapshot_id) {
            <div class="flex items-center justify-between py-2 border-b border-slate-100">
              <div>
                <span class="text-sm font-medium text-slate-900">{{ backup.snapshot_id }}</span>
                <span class="text-xs text-slate-500 ml-2">{{ backup.created_at | vertepDate }}</span>
              </div>
              <button (click)="confirmRestore(backup.snapshot_id)" [disabled]="restoring() === backup.snapshot_id" data-testid="backup-restore-button" class="text-xs text-blue-600 disabled:opacity-50">
                {{ restoring() === backup.snapshot_id ? 'Відновлення...' : 'Відновити' }}
              </button>
            </div>
          }
        </div>
      }
    </div>
  `,
})
export class BackupSectionComponent implements OnInit {
  backups = signal<BackupInfo[]>([]);
  loading = signal(false);
  error = signal<string | null>(null);
  restoring = signal<string | null>(null);
  restoreProgress = signal<{ progress: number; message: string } | null>(null);

  constructor(private api: VertepApiService, private toast: ToastService, private confirm: ConfirmService) {}
  ngOnInit(): void { this.loadBackups(); }

  loadBackups(): void {
    this.loading.set(true);
    this.api.getBackups().subscribe({
      next: (data) => { this.backups.set((data['snapshots'] as BackupInfo[]) || []); this.loading.set(false); },
      error: (err) => { this.error.set(err.message); this.loading.set(false); },
    });
  }

  createBackup(): void {
    this.api.createBackup().subscribe({ next: () => { this.toast.show('Бекап створено', 'success'); this.loadBackups(); }, error: (err) => this.error.set(err.message) });
  }

  confirmRestore(snapshotId: string): void {
    this.confirm.confirm({ title: 'Відновити бекап', message: `Відновити ${snapshotId}?` }).subscribe((ok) => {
      if (!ok) return;
      this.restoring.set(snapshotId);
      this.restoreProgress.set({ progress: 0, message: 'Запуск...' });
      this.api.restoreBackup(snapshotId).subscribe({
        next: () => { this.toast.show('Відновлення завершено', 'success'); this.restoring.set(null); this.restoreProgress.set(null); this.loadBackups(); },
        error: (err) => { this.error.set(err.message); this.restoring.set(null); this.restoreProgress.set(null); },
      });
    });
  }
}
