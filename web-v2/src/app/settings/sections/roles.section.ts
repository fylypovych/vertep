import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { WorkersApiService } from '../../core/api/workers.api';
import { ToastService } from '../../core/services/toast.service';
import { SystemRole, SystemRolesResponse } from '../../core/models';
import { LoadingStateComponent } from '../../shared/loading-state.component';
import { ErrorStateComponent } from '../../shared/error-state.component';

@Component({
  selector: 'app-settings-roles',
  standalone: true,
  imports: [CommonModule, LoadingStateComponent, ErrorStateComponent],
  /* template: `
    <div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-roles">
      <h3 class="text-lg font-semibold text-slate-900 mb-4">���? � ���������?</h3>
      @if (loading()) {
        <app-loading-state />
      } @else if (error()) {
        <app-error-state [message]="error()!" />
      } @else {
        <div class="text-xs text-slate-500 mb-3">��⨢�? ஫?: {{ selectedRoles.length ? selectedRoles.join(', ') : '������ ����?����?�' }}</div>
        <div class="flex flex-wrap gap-2 mb-3">
          @for (role of allRoles; track role.id) {
            <span class="px-2 py-1 text-xs border border-slate-200 rounded-lg"
                  [class.bg-emerald-50]="selectedRoles.includes(role.id)"
                  [class.border-emerald-300]="selectedRoles.includes(role.id)">
              <input type="checkbox" [checked]="selectedRoles.includes(role.id)"
                     (change)="toggleRole(role.id, $event)" class="mr-1">
              {{ role.label }}
            </span>
          }
        }
        @if (saveMessage()) {
          <div class="mb-3 text-sm" [class.text-amber-600]="saveState() === 'QUEUED' || saveState() === 'APPLYING'"
               [class.text-red-600]="saveState() === 'ERROR'" [class.text-emerald-600]="saveState() === 'COMPLETED'">
            {{ saveMessage() }}
          </div>
        }
        <button (click)="saveRoles()" [disabled]="saving()" class="px-3 py-1.5 text-sm bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 disabled:opacity-50" data-testid="roles-save-button">
          {{ saving() ? '���০����...' : '���ॣ� ஫?' }}
        </button>
      }
    </div>
  `, */
  template: `<div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-roles"><h3 class="text-lg font-semibold mb-4">Ролі вузла</h3><app-loading-state *ngIf="loading()" /><app-error-state *ngIf="error()" [message]="error()!" /><div *ngIf="rolesResponse"><p class="mb-3 text-sm text-slate-600">Активні ролі: {{ selectedRoles.length ? selectedRoles.join(', ') : 'не налаштовано' }}</p><label *ngFor="let role of allRoles" class="mr-3 inline-flex items-center gap-1"><input type="checkbox" [checked]="selectedRoles.includes(role.id)" (change)="toggleRole(role.id, $event)">{{ role.label }}</label><button (click)="saveRoles()" [disabled]="saving()" class="mt-4 block px-3 py-1.5 text-sm bg-emerald-600 text-white rounded-lg disabled:opacity-50" data-testid="roles-save-button">{{ saving() ? 'Збереження...' : 'Зберегти ролі' }}</button></div></div>`,
})
export class RolesSectionComponent implements OnInit {
  allRoles: SystemRole[] = [];
  selectedRoles: string[] = [];
  rolesResponse: SystemRolesResponse | null = null;
  loading = signal(false);
  error = signal<string | null>(null);
  saving = signal(false);
  saveMessage = signal<string | null>(null);
  saveState = signal<string | null>(null);

  constructor(private workersApi: WorkersApiService, private toast: ToastService) {}

  ngOnInit(): void { this.loadRoles(); }

  loadRoles(): void {
    this.loading.set(true);
    this.error.set(null);
    this.workersApi.systemRoles().subscribe({
      next: (resp) => { this.rolesResponse = resp; this.allRoles = resp.available_roles || []; this.selectedRoles = resp.active_roles || []; this.loading.set(false); },
      error: (err) => { this.error.set(err.message); this.loading.set(false); },
    });
  }

  toggleRole(roleId: string, event: Event): void {
    const checked = (event.target as HTMLInputElement).checked;
    this.selectedRoles = checked
      ? [...new Set([...this.selectedRoles, roleId])]
      : this.selectedRoles.filter(r => r !== roleId);
  }

  saveRoles(): void {
    this.saving.set(true);
    this.saveMessage.set(null);
    this.workersApi.updateSystemRoles(this.selectedRoles).subscribe({
      next: () => { this.saving.set(false); this.toast.show('Ролі збережено', 'success'); this.loadRoles(); },
      error: (err) => { this.saving.set(false); this.toast.show(err.message || 'Помилка', 'error'); },
    });
  }
}
