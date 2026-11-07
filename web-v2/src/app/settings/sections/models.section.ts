import { TPipe } from '../../core/i18n/t.pipe';
import { Component, OnInit, signal, OnDestroy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { SettingsApiService, OperationState, ModelNodeInfo } from '../../core/api/settings.api';
import { ToastService } from '../../core/services/toast.service';
import { ConfirmService } from '../../core/services/confirm.service';
import { ModelInfo } from '../../core/models';
import { PolicyService } from '../../core/services/policy.service';
import { LoadingStateComponent } from '../../shared/loading-state.component';
import { ErrorStateComponent } from '../../shared/error-state.component';
import { interval, Subscription } from 'rxjs';

@Component({
  selector: 'app-settings-models',
  standalone: true,
  imports: [TPipe, CommonModule, FormsModule, LoadingStateComponent, ErrorStateComponent],
  template: `<div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-models">
  <h3 class="text-lg font-semibold mb-4">{{ 'Моделі (Ollama)' | t }}</h3>
  <app-loading-state *ngIf="loading()" />
  <app-error-state *ngIf="error()" [message]="error()!" />
  <div *ngIf="!loading() && !error()">
    <div class="flex gap-2 mb-4">
      <select [(ngModel)]="targetNode" class="border rounded px-3 py-2" data-testid="pull-node-select">
        <option value="">{{ 'Локальний (CORE)' | t }}</option>
        <option *ngFor="let node of nodes()" [value]="node.node_name">
          {{ node.node_name }}<ng-container *ngIf="!node.ready"> {{ '(неготовий)' | t }}</ng-container>
        </option>
      </select>
      <input [(ngModel)]="modelName" class="flex-1 border rounded px-3 py-2" placeholder="{{ 'Назва моделі' | t }}" [disabled]="pulling()" data-testid="pull-model-input">
      <button (click)="pullModel()" [disabled]="!modelName || pulling() || !canPullModel" [title]="(pullModelReason || 'Завантажити модель') | t" class="px-4 py-2 bg-emerald-600 text-white rounded" data-testid="pull-model-button">
        {{ (pulling() ? 'Завантаження...' : 'Завантажити') | t }}
      </button>
    </div>

    <div *ngIf="activePull()" class="mb-4 p-3 bg-slate-50 rounded-lg border" data-testid="pull-progress">
      <div class="flex justify-between text-sm mb-1">
        <span class="font-medium">{{ activePull()?.current_phase || 'pull' }}</span>
        <span>{{ activePull()?.progress || 0 }}%</span>
      </div>
      <div class="w-full bg-slate-200 rounded-full h-2">
        <div class="bg-emerald-600 h-2 rounded-full transition-all"
             [style.width.%]="activePull()?.progress || 0"></div>
      </div>
      <div *ngIf="activePull()?.message" class="text-slate-600 text-sm mt-1">{{ (activePull()?.message) | t }}</div>
      <div *ngIf="activePull()?.error" class="text-red-600 text-sm mt-1">{{ activePull()?.error }}</div>
      <div class="flex gap-2 mt-2">
        <button *ngIf="activePull()?.status === 'QUEUED' || activePull()?.status === 'RUNNING'"
                (click)="cancelPull()" [disabled]="!canCancelPull" [title]="(cancelPullReason || 'Скасувати') | t" class="text-sm text-red-600 hover:underline disabled:opacity-50" data-testid="pull-cancel-button">{{ 'Скасувати' | t }}</button>
      </div>
    </div>

    <div *ngIf="nodes().length" class="mb-4" data-testid="model-nodes-table">
      <h4 class="text-sm font-semibold mb-2">{{ 'Розподіл по вузлах' | t }}</h4>
      <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        <div *ngFor="let node of nodes()" class="border border-slate-200 rounded-xl p-4 text-sm" [attr.data-testid]="'model-node-' + node.node_name">
          <div class="font-semibold mb-1">{{ node.node_name }}</div>
          <div>{{ 'Стан' | t }}: {{ (node.ready ? 'Готовий' : 'Неготовий') | t }}</div>
          <div>{{ 'Каталог' | t }}: {{ (node.stale ? 'застарілий' : 'активний') | t }}</div>
          <div>{{ 'Моделей' | t }}: {{ node.model_count ?? 0 }}</div>
          <div>{{ 'Команда' | t }}: {{ (node.pending_command ? 'в черзі' : '-') | t }}</div>
        </div>
      </div>
    </div>

    <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
    <div *ngFor="let model of models()" class="flex justify-between gap-2 border border-slate-200 rounded-xl p-4" [attr.data-testid]="'model-row-' + model.name">
      <span>{{ model.name }}</span>
      <button (click)="deleteModel(model.name)" [disabled]="!canDeleteModel" [title]="(deleteModelReason || 'Видалити модель') | t" class="text-red-600 disabled:opacity-40" [attr.data-testid]="'model-delete-' + model.name">{{ 'Видалити' | t }}</button>
    </div>
    </div>

    <div class="mt-6 pt-4 border-t" data-testid="voice-preview">
      <h4 class="text-sm font-semibold mb-2">{{ 'Перевірка голосу (TTS)' | t }}</h4>
      <div class="flex gap-2 mb-2">
        <input [(ngModel)]="previewText" class="flex-1 border rounded px-3 py-2"
               placeholder="{{ 'Текст для перевірки' | t }}" maxlength="500" data-testid="voice-preview-text">
        <select [(ngModel)]="previewVoice" class="border rounded px-3 py-2" data-testid="voice-preview-voice">
          <option value="default">default</option>
          <option *ngFor="let voice of voices()" [value]="voice">{{ voice }}</option>
        </select>
        <input [(ngModel)]="previewSpeed" type="number" min="1" max="500" class="w-20 border rounded px-2 py-2"
               data-testid="voice-preview-speed">
        <button (click)="runPreview()" [disabled]="previewRunning() || !previewText.trim()"
                class="px-4 py-2 bg-sky-600 text-white rounded" data-testid="voice-preview-button">
          {{ (previewRunning() ? 'Синтез...' : 'Прослухати') | t }}
        </button>
      </div>
      <div *ngIf="previewError()" class="text-red-600 text-sm" data-testid="voice-preview-error">{{ (previewError()) | t }}</div>
      <audio *ngIf="previewUrl()" [src]="previewUrl()" controls autoplay data-testid="voice-preview-audio"></audio>
    </div>
  </div>
</div>`,
})
export class ModelsSectionComponent implements OnInit, OnDestroy {
  models = signal<ModelInfo[]>([]);
  nodes = signal<ModelNodeInfo[]>([]);
  voices = signal<string[]>([]);
  loading = signal(false);
  error = signal<string | null>(null);
  modelName = '';
  targetNode = '';
  pulling = signal(false);
  activePull = signal<OperationState | null>(null);
  previewText = 'Перевірка голосу Vertep.';
  previewVoice = 'default';
  previewSpeed = 150;
  previewRunning = signal(false);
  previewError = signal<string | null>(null);
  previewUrl = signal<string | null>(null);
  private pollSub: Subscription | null = null;
  private audioUrl: string | null = null;

  constructor(
    private settings: SettingsApiService,
    private toast: ToastService,
    private confirm: ConfirmService,
    public policy: PolicyService,
  ) {}

  get canPullModel(): boolean { return this.policy.can('pull_model').allowed; }
  get pullModelReason(): string | null { return this.policy.disabledReason('pull_model'); }
  get canCancelPull(): boolean { return this.policy.can('cancel_pull').allowed; }
  get cancelPullReason(): string | null { return this.policy.disabledReason('cancel_pull'); }
  get canDeleteModel(): boolean { return this.policy.can('delete_model').allowed; }
  get deleteModelReason(): string | null { return this.policy.disabledReason('delete_model'); }

  ngOnInit(): void {
    this.loadModels();
    this.loadNodes();
    this.loadVoices();
  }

  loadModels(): void {
    this.loading.set(true);
    this.error.set(null);
    this.settings.models().subscribe({
      next: (data) => {
        this.models.set((data['models'] as ModelInfo[]) || []);
        if (data.nodes) this.nodes.set(data.nodes);
        this.loading.set(false);
      },
      error: (err) => { this.error.set(err.message); this.loading.set(false); },
    });
  }

  loadNodes(): void {
    this.settings.modelNodes().subscribe({
      next: (data) => this.nodes.set(data.nodes || []),
      error: () => { /* placement is an optional view */ },
    });
  }

  loadVoices(): void {
    this.settings.voices().subscribe({
      next: (data) => this.voices.set(data.voices || []),
      error: () => { /* catalog stays at the default entry */ },
    });
  }

  pullModel(): void {
    if (!this.modelName || this.pulling() || !this.canPullModel) return;
    this.pulling.set(true);
    this.error.set(null);
    this.settings.pullModel(this.modelName, this.targetNode || undefined).subscribe({
      next: (op) => {
        this.activePull.set(op as OperationState);
        this.startPolling(op.operation_id);
      },
      error: (err) => { this.pulling.set(false); this.error.set(err.message); },
    });
  }

  cancelPull(): void {
    const op = this.activePull();
    if (!op || !this.canCancelPull) return;
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
    if (!this.canDeleteModel) return;
    this.confirm.confirm({
      title: 'Видалити модель',
      message: `Видалити модель ${name}${this.targetNode ? ` з вузла ${this.targetNode}` : ''}?`,
    }).subscribe((confirmed) => {
      if (!confirmed) return;
      this.settings.deleteModel(name, this.targetNode || undefined).subscribe({
        next: () => { this.loadModels(); this.loadNodes(); },
        error: (err) => this.error.set(err.message),
      });
    });
  }

  runPreview(): void {
    const text = this.previewText.trim();
    if (!text || this.previewRunning()) return;
    this.previewRunning.set(true);
    this.previewError.set(null);
    this.settings.previewVoice({ text, voice: this.previewVoice, speed: Number(this.previewSpeed) }).subscribe({
      next: (blob) => {
        this.previewRunning.set(false);
        if (this.audioUrl) URL.revokeObjectURL(this.audioUrl);
        this.audioUrl = URL.createObjectURL(blob);
        this.previewUrl.set(this.audioUrl);
      },
      error: (err) => {
        this.previewRunning.set(false);
        this.previewError.set(err?.error?.detail || err?.message || 'Не вдалося синтезувати голос');
      },
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
              this.loadNodes();
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

  ngOnDestroy(): void {
    this.stopPolling();
    if (this.audioUrl) URL.revokeObjectURL(this.audioUrl);
  }
}
