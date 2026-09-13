import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { map, Observable } from 'rxjs';
import { OperationsApiService } from '../../core/api/operations.api';
import { ToastService } from '../../core/services/toast.service';
import { ConfirmService } from '../../core/services/confirm.service';
import { BackupInfo, BackupListResponse } from '../../core/models';
import { RemoteState, RemoteMutation } from '../../core/state/remote-state';
import { VertepDatePipe } from '../../shared/vertep-date.pipe';

function mapToSnapshots(): (source: Observable<BackupListResponse>) => Observable<BackupInfo[]> {
  return (source) => source.pipe(map((data) => data?.snapshots || []));
}

@Component({
  selector: 'app-settings-backup',
  standalone: true,
  imports: [CommonModule, VertepDatePipe],
  template: `
    <div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-backup">
      <div class="flex items-center justify-between mb-4">
        <h3 class="text-lg font-semibold text-slate-900">Бекапи</h3>
        <button (click)="createBackup()" [disabled]="creating.pending()" data-testid="backup-create-button" class="px-3 py-1.5 text-sm bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 disabled:opacity-50">{{ creating.pending() ? 'Створення...' : 'Створити' }}</button>
      </div>
      @if (creating.error()) {
        <p class="text-sm text-red-600 mb-2">{{ creating.error() }}</p>
      }
      @if (restore.progress() !== null) {
        <div class="mb-4 p-3 bg-blue-50 rounded-lg">
          <p class="text-sm text-blue-700">{{ restore.message() }}</p>
        </div>
      }
      @if (list.loading()) {
        <div class="animate-pulse space-y-2"><div class="h-5 bg-slate-200 rounded w-full"></div></div>
      } @else if (list.failed()) {
        <div>
          <p class="text-red-600">{{ list.error() }}</p>
          <button (click)="loadBackups()" class="text-sm text-red-600 hover:text-red-700 font-medium mt-1">Повторити</button>
        </div>
      } @else if (list.data().length === 0) {
        <p class="text-sm text-slate-500">Бекапів не знайдено</p>
      } @else {
        <div class="space-y-2">
          @for (backup of list.data(); track backup.snapshot_id) {
            <div class="flex items-center justify-between py-2 border-b border-slate-100">
              <div>
                <span class="text-sm font-medium text-slate-900">{{ backup.snapshot_id }}</span>
                <span class="text-xs text-slate-500 ml-2">{{ backup.created_at | vertepDate }}</span>
              </div>
              <button (click)="confirmRestore(backup.snapshot_id)" [disabled]="restore.pending()" data-testid="backup-restore-button" class="text-xs text-blue-600 disabled:opacity-50">
                {{ restore.pending() ? 'Відновлення...' : 'Відновити' }}
              </button>
            </div>
          }
        </div>
      }
    </div>
  `,
})
export class BackupSectionComponent implements OnInit {
  readonly list = new RemoteState<BackupInfo[]>([]);
  readonly creating = new RemoteMutation();
  readonly restore = new RemoteMutation();

  constructor(private ops: OperationsApiService, private toast: ToastService, private confirm: ConfirmService) {}
  ngOnInit(): void { this.loadBackups(); }

  loadBackups(): void {
    this.list.run(() => this.ops.backups().pipe(mapToSnapshots()), 'Не вдалося завантажити бекапи');
  }

  createBackup(): void {
    this.creating
      .then(() => { this.toast.show('Бекап створено', 'success'); this.loadBackups(); })
      .run(() => this.ops.createBackup(), { fallbackMessage: 'Не вдалося створити бекап' });
  }

  confirmRestore(snapshotId: string): void {
    this.confirm.confirm({ title: 'Відновити бекап', message: `Відновити ${snapshotId}?` }).subscribe((ok) => {
      if (!ok) return;
      this.restore
        .then(() => { this.toast.show('Відновлення завершено', 'success'); this.loadBackups(); })
        .run(() => this.ops.restoreBackup(snapshotId), {
          fallbackMessage: 'Не вдалося відновити бекап',
          onProgress: (p) => this.restore.progress.set(p),
        });
    });
  }
}
