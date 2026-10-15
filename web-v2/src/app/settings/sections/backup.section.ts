import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { map, Observable, of, throwError, timer } from 'rxjs';
import { switchMap, takeWhile } from 'rxjs/operators';
import { OperationsApiService } from '../../core/api/operations.api';
import { ToastService } from '../../core/services/toast.service';
import { ConfirmService } from '../../core/services/confirm.service';
import { BackupInfo, BackupListResponse, OperationAck } from '../../core/models';
import { RemoteState, RemoteMutation } from '../../core/state/remote-state';
import { VertepDatePipe } from '../../shared/vertep-date.pipe';
import { LoadingStateComponent } from '../../shared/loading-state.component';
import { ErrorStateComponent } from '../../shared/error-state.component';

function mapToSnapshots(): (source: Observable<BackupListResponse>) => Observable<BackupInfo[]> {
  return (source) => source.pipe(map((data) => data?.snapshots || []));
}

@Component({
  selector: 'app-settings-backup',
  standalone: true,
  imports: [CommonModule, VertepDatePipe, LoadingStateComponent, ErrorStateComponent],
  template: `
    <div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-backup">
      <div class="flex items-center justify-between mb-4">
        <h3 class="text-lg font-semibold text-slate-900">Резервні копії</h3>
        <button (click)="createBackup()" [disabled]="creating.pending()" data-testid="backup-create-button" class="px-3 py-1.5 text-sm bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 disabled:opacity-50">{{ creating.pending() ? 'Створення...' : 'Створити' }}</button>
      </div>
      @if (creating.error()) {
        <app-error-state [message]="creating.error()!" [showRetry]="false" />
      }
      @if (restore.progress() !== null) {
        <div class="mb-4 p-3 bg-blue-50 rounded-lg">
          <p class="text-sm text-blue-700">{{ restore.message() }}</p>
        </div>
      }
      @if (list.loading()) {
        <app-loading-state />
      } @else if (list.failed()) {
        <app-error-state [message]="list.error()!" [showRetry]="true" (retry)="loadBackups()" />
      } @else if (list.data().length === 0) {
        <p class="text-sm text-slate-500">Резервних копій немає</p>
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
    this.list.run(() => this.ops.backups().pipe(mapToSnapshots()), 'Не вдалося завантажити резервні копії');
  }

  createBackup(): void {
    this.creating
      .then(() => { this.toast.show('Резервну копію створено', 'success'); this.loadBackups(); })
      .run(() => this.ops.createBackup(), { fallbackMessage: 'Не вдалося створити резервну копію' });
  }

  confirmRestore(snapshotId: string): void {
    this.confirm.confirm({ title: 'Відновити резервну копію', message: `Відновити ${snapshotId}?` }).subscribe((ok) => {
      if (!ok) return;
      this.restore
        .then(() => { this.toast.show('Відновлення завершено', 'success'); this.loadBackups(); })
        .run(() => this.ops.restoreBackup(snapshotId).pipe(
          switchMap((ack) => this.executeConfirmed(snapshotId, ack)),
        ), {
          fallbackMessage: 'Не вдалося відновити резервну копію',
          onProgress: (p) => { this.restore.progress.set(p); },
        });
    });
  }

  /**
   * Issue #137: after the confirmation challenge, execute the destructive restore
   * with the single-use token and follow its live progress to a terminal state.
   * A `confirmation_required` acknowledgement is never reported as a success.
   */
  private executeConfirmed(snapshotId: string, ack: OperationAck): Observable<OperationAck> {
    if (ack?.status === 'confirmation_required') {
      if (!ack.confirmation_token) {
        return throwError(() => new Error('Backend не надав токен підтвердження відновлення'));
      }
      this.restore.progress.set(0);
      this.restore.message.set('Виконую відновлення...');
      return this.ops.confirmRestore(snapshotId, ack.confirmation_token).pipe(
        switchMap((result) => this.awaitTerminal(snapshotId, result)),
      );
    }
    return this.awaitTerminal(snapshotId, ack);
  }

  private awaitTerminal(snapshotId: string, ack: OperationAck): Observable<OperationAck> {
    if (ack?.status === 'done') return of(ack);
    if (ack?.status === 'error' || ack?.error) {
      return throwError(() => new Error(ack?.message || ack?.error || 'Відновлення завершилось з помилкою'));
    }
    let attempts = 0;
    return timer(1000, 1000).pipe(
      takeWhile((tick) => tick < 600),
      switchMap(() => this.ops.restoreProgress(snapshotId)),
      map((progress) => {
        attempts += 1;
        this.restore.progress.set(progress?.progress ?? null);
        this.restore.message.set(progress?.message ?? null);
        return progress;
      }),
      takeWhile((progress) => progress?.status !== 'done' && progress?.status !== 'error', true),
      switchMap((progress) => {
        if (progress?.status === 'done') return of<OperationAck>({ status: 'done', snapshot_id: snapshotId });
        if (progress?.status === 'error' || attempts >= 600) {
          return throwError(() => new Error(progress?.message || 'Відновлення не завершилось'));
        }
        return throwError(() => new Error('Відновлення перевищило час очікування'));
      }),
    );
  }
}
