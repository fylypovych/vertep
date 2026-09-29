import { Component, OnInit, signal, OnDestroy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { SettingsApiService, OperationState } from '../../core/api/settings.api';
import { ToastService } from '../../core/services/toast.service';
import { ConfirmService } from '../../core/services/confirm.service';
import { ModelInfo } from '../../core/models';
import { LoadingStateComponent } from '../../shared/loading-state.component';
import { ErrorStateComponent } from '../../shared/error-state.component';
import { interval, Subscription } from 'rxjs';

@Component({
  selector: 'app-settings-models',
  standalone: true,
  imports: [CommonModule, FormsModule, LoadingStateComponent, ErrorStateComponent],
  template: `<div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-models">
  <h3 class="text-lg font-semibold mb-4">Моделі (Ollama)</h3>
  <app-loading-state *ngIf="loading()" />
  <app-error-state *ngIf="error()" [message]="error()!" />
  <div *ngIf="!loading() && !error()">
    <div class="flex gap-2 mb-4">
      <input [(ngModel)]="modelName" class="flex-1 border rounded px-3 py-2" placeholder="Назва моделі" [disabled]="pulling()">
      <button (click)="pullModel()" [disabled]="!modelName || pulling()" class="px-4 py-2 bg-emerald-600 text-white rounded">
        {{ pulling() ? 'Завантаження...' : 'Завантажити' }}
      </button>
    </div>

    <div *ngIf="activePull()" class="mb-4 p-3 bg-slate-50 rounded-lg border">
      <div class="flex justify-between text-sm mb-1">
        <span class="font-medium">{{ activePull()?.current_phase || 'pull' }}</span>
        <span>{{ activePull()?.progress || 0 }}%</span>
      </div>
      <div class="w-full bg-slate-200 rounded-full h-2">
        <div class="bg-emerald-600 h-2 rounded-full transition-all"
             [style.width.%]="activePull()?.progress || 0"></div>
      </div>
      <div *ngIf="activePull()?.error" class="text-red-600 text-sm mt-1">{{ activePull()?.error }}</div>
      <div class="flex gap-2 mt-2">
        <button *ngIf="activePull()?.status === 'QUEUED' || activePull()?.status === 'RUNNING'"
                (click)="cancelPull()" class="text-sm text-red-600 hover:underline">Скасувати</button>
      </div>
    </div>

    <div *ngFor="let model of models()" class="flex justify-between py-2 border-b">
      <span>{{ model.name }}</span>
      <button (click)="deleteModel(model.name)" class="text-red-600">Видалити</button>
    </div>
  </div>
</div>`,
})
export class ModelsSectionComponent implements OnInit, OnDestroy {
  models = signal<ModelInfo[]>([]);
  loading = signal(false);
  error = signal<string | null>(null);
  modelName = '';
  pulling = signal(false);
  activePull = signal<OperationState | null>(null);
  private pollSub: Subscription | null = null;

  constructor(
    private settings: SettingsApiService,
    private toast: ToastService,
    private confirm: ConfirmService,
  ) {}

  ngOnInit(): void {
    this.loadModels();
  }

  loadModels(): void {
    this.loading.set(true);
    this.error.set(null);
    this.settings.models().subscribe({
      next: (data) => { this.models.set((data['models'] as ModelInfo[]) || []); this.loading.set(false); },
      error: (err) => { this.error.set(err.message); this.loading.set(false); },
    });
  }

  pullModel(): void {
    if (!this.modelName || this.pulling()) return;
    this.pulling.set(true);
    this.error.set(null);
    this.settings.pullModel(this.modelName).subscribe({
      next: (op) => {
        this.activePull.set(op as OperationState);
        this.startPolling(op.operation_id);
      },
      error: (err) => { this.pulling.set(false); this.error.set(err.message); },
    });
  }

  cancelPull(): void {
    const op = this.activePull();
    if (!op) return;
    this.settings.cancelPull(op.operation_id).subscribe({
      next: (updated) => {
        this.activePull.set(updated as OperationState);
        this.stopPolling();
        this.pulling.set(false);
        this.modelName = '';
      },
      error: (err) => { this.error.set(err.message); },
    });
  }

  deleteModel(name: string): void {
    this.confirm.confirm({
      title: 'Видалити модель',
      message: `Видалити модель ${name}?`,
    }).subscribe((confirmed) => {
      if (!confirmed) return;
      this.settings.deleteModel(name).subscribe({
        next: () => this.loadModels(),
        error: (err) => this.error.set(err.message),
      });
    });
  }

  private startPolling(operationId: string): void {
    this.stopPolling();
    this.pollSub = interval(2000).subscribe(() => {
      this.settings.operation(operationId).subscribe({
        next: (op) => {
          this.activePull.set(op as OperationState);
          const status = op.status;
          if (status === 'COMPLETED' || status === 'FAILED' || status === 'CANCELLED') {
            this.stopPolling();
            this.pulling.set(false);
            this.modelName = '';
            if (status === 'COMPLETED') {
              this.loadModels();
              this.toast.show('Модель завантажено', 'success');
            } else if (status === 'CANCELLED') {
              this.toast.show('Завантаження скасовано', 'info');
            } else {
              this.error.set(op.error || 'Помилка завантаження');
            }
          }
        },
        error: () => { /* transient; keep polling */ },
      });
    });
  }

  private stopPolling(): void {
    if (this.pollSub) { this.pollSub.unsubscribe(); this.pollSub = null; }
  }

  ngOnDestroy(): void { this.stopPolling(); }
}
