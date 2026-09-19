import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { SystemApiService } from '../../core/api/system.api';
import { SystemStatus } from '../../core/models';
import { LoadingStateComponent } from '../../shared/loading-state.component';
import { ErrorStateComponent } from '../../shared/error-state.component';

@Component({
  selector: 'app-settings-system-info',
  standalone: true,
  imports: [CommonModule, LoadingStateComponent, ErrorStateComponent],
  template: `
    <div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-system-info">
      <h3 class="text-lg font-semibold text-slate-900 mb-4">���⥬�</h3>
      @if (loading()) {
        <app-loading-state />
      } @else if (error()) {
        <app-error-state [message]="error()!" />
      } @else {
        <div class="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-2" data-testid="system-info">
          <div class="flex justify-between py-2 border-b border-slate-100">
            <span class="text-sm text-slate-500">�⠭ ��⥬�</span>
            <span class="text-sm font-medium" [class.text-emerald-600]="systemOk" [class.text-red-600]="!systemOk">{{ systemStateLabel }}</span>
          </div>
          <div class="flex justify-between py-2 border-b border-slate-100">
            <span class="text-sm text-slate-500">����?�</span>
            <span class="text-sm font-medium text-slate-900">{{ status()?.version || '-' }}</span>
          </div>
          <div class="flex justify-between py-2 border-b border-slate-100">
            <span class="text-sm text-slate-500">���</span>
            <span class="text-sm font-medium" [class.text-emerald-600]="status()?.core === 'OK'" [class.text-red-600]="status()?.core !== 'OK'">{{ status()?.core || '-' }}</span>
          </div>
          <div class="flex justify-between py-2 border-b border-slate-100">
            <span class="text-sm text-slate-500">���� �����</span>
            <span class="text-sm font-medium" [class.text-emerald-600]="status()?.postgres === 'OK'" [class.text-red-600]="status()?.postgres !== 'OK'">{{ status()?.postgres || '-' }}</span>
          </div>
          <div class="flex justify-between py-2 border-b border-slate-100">
            <span class="text-sm text-slate-500">Redis</span>
            <span class="text-sm font-medium" [class.text-emerald-600]="status()?.redis === 'OK'" [class.text-red-600]="status()?.redis !== 'OK'">{{ status()?.redis || '-' }}</span>
          </div>
          <div class="flex justify-between py-2 border-b border-slate-100">
            <span class="text-sm text-slate-500">�客��</span>
            <span class="text-sm font-medium" [class.text-emerald-600]="status()?.storage === 'OK'" [class.text-red-600]="status()?.storage !== 'OK'">{{ status()?.storage || '-' }}</span>
          </div>
          <div class="flex justify-between py-2 border-b border-slate-100">
            <span class="text-sm text-slate-500">���筠 ����?�</span>
            <p class="text-sm font-medium text-slate-900" data-testid="status-update-current-version">{{ status()?.update?.['current_version'] || '-' }}</p>
          </div>
          <div>
            <span class="text-xs text-slate-500">����㯭� ����?�</span>
            <p class="text-sm font-medium text-slate-900" data-testid="status-update-available-version">{{ status()?.update?.['available_version'] || '-' }}</p>
          </div>
          <div>
            <span class="text-xs text-slate-500">�⠭</span>
            <p class="text-sm font-medium text-slate-900">{{ status()?.update?.['state'] || '-' }}</p>
          </div>
          <div>
            <span class="text-xs text-slate-500">���������</span>
            <p class="text-sm font-medium text-slate-900>{{ status()?.update?.['update_available'] ? '����㯭�' : '�����' }}</p>
          </div>
        </div>
        @if (backendsList.length) {
          <div class="mt-4 overflow-x-auto">
            <table class="w-full text-sm text-left" data-testid="backends-table">
              <thead class="text-xs text-slate-500 uppercase bg-slate-50">
                <tr>
                  <th class="px-4 py-2">������</th>
                  <th class="px-4 py-2">�����⮢���</th>
                  <th class="px-4 py-2">������</th>
                </tr>
              </thead>
              <tbody>
                @for (entry of backendsList; track entry[0]) {
                  <tr class="border-t border-slate-100">
                    <td class="px-4 py-2">{{ entry[0] }}</td>
                    <td class="px-4 py-2">{{ entry[1]?.['configured'] ? '���' : '�?' }}</td>
                    <td class="px-4 py-2">{{ entry[1]?.['backend'] || '-' }}</td>
                  </tr>
                }
              }
            </table>
          </div>
        }
      }
    </div>
  `,
})
export class SystemInfoSectionComponent implements OnInit {
  status = signal<SystemStatus | null>(null);
  loading = signal(false);
  error = signal<string | null>(null);

  get systemOk(): boolean {
    const s = this.status()?.system?.state?.toUpperCase();
    return s === 'NORMAL' || s === 'OK' || s === 'HEALTHY';
  }

  get systemStateLabel(): string {
    const state = this.status()?.system?.state?.toUpperCase() ?? 'NORMAL';
    const labels: Record<string, string> = {
      NORMAL: '��ଠ�쭨�', OK: '�����', HEALTHY: '�����',
      MAINTENANCE: '���㣮�㢠���', UPDATING: '���������',
      EMERGENCY: '����?�', FAILED: '�������', ERROR: '�������',
    };
    return labels[state] ?? state;
  }

  get backendsList(): [string, Record<string, unknown>][] {
    const providers = this.status()?.providers;
    if (!providers) return [];
    return Object.entries(providers as unknown as Record<string, Record<string, unknown>>);
  }

  constructor(private systemApi: SystemApiService) {}

  ngOnInit(): void {
    this.loading.set(true);
    this.systemApi.status().subscribe({
      next: (s) => { this.status.set(s); this.loading.set(false); },
      error: (err) => { this.error.set(err.message || '�� ������� �����⠦�� �����'); this.loading.set(false); },
    });
  }
}
