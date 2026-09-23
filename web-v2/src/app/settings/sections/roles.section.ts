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
