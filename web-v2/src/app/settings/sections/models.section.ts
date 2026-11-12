import { TPipe } from '../../core/i18n/t.pipe';
import { Component, OnInit, signal, OnDestroy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { SettingsApiService, OperationState, ModelNodeInfo, ModelCatalogEntry, VoiceOption } from '../../core/api/settings.api';
import { ToastService } from '../../core/services/toast.service';
import { ConfirmService } from '../../core/services/confirm.service';
import { ModelInfo } from '../../core/models';
import { PolicyService } from '../../core/services/policy.service';
import { LoadingStateComponent } from '../../shared/loading-state.component';
import { ErrorStateComponent } from '../../shared/error-state.component';
import { interval, Subscription } from 'rxjs';

/** Same rule as the pull API; the name reaches Ollama unchanged. */
const MODEL_NAME = /^[0-9A-Za-z][0-9A-Za-z._:/+-]{0,127}$/;

interface PullFailure { text: string; detail?: string; retry: boolean }

/** What went wrong with a pull, from the HTTP status or the recorded Ollama/node error. */
export function pullFailure(detail: string, status?: number): PullFailure {
  if (status === 422) return { text: 'Неправильна назва моделі. Формат: назва:тег без пробілів, наприклад llama3.2:3b.', detail, retry: false };
  if (/file does not exist|manifest unknown|not found/i.test(detail)) {
    return { text: 'Модель або тег не знайдено в бібліотеці Ollama. Перевірте назву або виберіть модель з каталогу.', detail, retry: false };
  }
  if (status === 404 || status === 409 || /^Node |Unknown node/.test(detail)) {
    return { text: 'Вибраний вузол зараз не може завантажувати моделі.', detail, retry: true };
  }
  if (/OLLAMA_URL|connect|refused|unreachable|timed? ?out|pull failed|Ollama returned 5/i.test(detail)) {
    return { text: 'Ollama недоступна на вибраному вузлі.', detail, retry: true };
  }
  return { text: 'Не вдалося завантажити модель.', detail, retry: true };
}

@Component({
  selector: 'app-settings-models',
  standalone: true,
  imports: [TPipe, CommonModule, FormsModule, LoadingStateComponent, ErrorStateComponent],
  template: `<div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-models">
  <h3 class="text-lg font-semibold mb-4">{{ 'Моделі (Ollama)' | t }}</h3>
  <div>
    <section class="mb-4" data-testid="model-pull">
      <h4 class="text-sm font-semibold mb-2">{{ 'Завантажити модель' | t }}</h4>
      <div class="flex flex-wrap gap-2 mb-2">
        <label class="sr-only" for="pull-node-select">{{ 'Вузол' | t }}</label>
        <select id="pull-node-select" [(ngModel)]="targetNode" class="border rounded px-3 py-2" data-testid="pull-node-select" [disabled]="pulling()">
          <option value="">{{ 'Локальний (CORE)' | t }}</option>
          <option *ngFor="let node of nodes()" [value]="node.node_name">
            {{ node.node_name }}<ng-container *ngIf="!node.ready"> {{ '(неготовий)' | t }}</ng-container>
          </option>
        </select>
        <label class="sr-only" for="pull-catalog-select">{{ 'Модель з каталогу' | t }}</label>
        <select id="pull-catalog-select" class="border rounded px-3 py-2 flex-1 min-w-48" data-testid="pull-catalog-select"
                [ngModel]="catalogChoice()" (ngModelChange)="chooseFromCatalog($event)" [disabled]="pulling() || !catalog().length">
          <option value="">{{ (catalog().length ? 'Виберіть модель з каталогу' : 'Каталог недоступний') | t }}</option>
          <option *ngFor="let item of catalog()" [value]="item.name">
            {{ item.name }} — {{ item.family }} {{ item.parameters }}, {{ gigabytes(item.download_bytes) }} {{ 'ГБ' | t }}<ng-container *ngIf="installedOnTarget(item.name)"> · {{ 'встановлено' | t }}</ng-container>
          </option>
        </select>
      </div>
      <div class="flex flex-wrap gap-2">
        <label class="sr-only" for="pull-model-input">{{ 'Назва моделі' | t }}</label>
        <input id="pull-model-input" [(ngModel)]="modelName" (ngModelChange)="pullError.set(null)" class="flex-1 min-w-48 border rounded px-3 py-2"
               placeholder="{{ 'Назва моделі, наприклад llama3.2:3b' | t }}" [disabled]="pulling()" data-testid="pull-model-input">
        <button (click)="pullModel()" [disabled]="!modelName.trim() || pulling() || !canPullModel" [title]="(pullModelReason || 'Завантажити модель') | t" class="px-4 py-2 bg-emerald-600 text-white rounded disabled:opacity-50" data-testid="pull-model-button">
          {{ (pulling() ? 'Завантаження...' : 'Завантажити') | t }}
        </button>
      </div>
      <p class="text-xs text-slate-500 mt-1">{{ 'Каталог — перевірені теги бібліотеки Ollama з розміром завантаження; придатність для ресурсів вузла він не гарантує. Назву іншого тегу можна ввести вручну.' | t }}</p>
      <p *ngIf="catalogError()" class="text-sm text-amber-700 mt-1" data-testid="catalog-error">{{ catalogError()! | t }}
        <button type="button" class="underline ml-1" (click)="loadCatalog()" data-testid="catalog-retry">{{ 'Повторити' | t }}</button></p>
      <div *ngIf="pullError() as failure" class="mt-2 p-3 bg-red-50 border border-red-200 rounded-lg text-sm" data-testid="pull-error" role="alert">
        <p class="text-red-700">{{ failure.text | t }}</p>
        <p *ngIf="failure.detail" class="text-xs text-slate-500 mt-1 break-all">{{ failure.detail }}</p>
        <button *ngIf="failure.retry" type="button" class="mt-1 text-sm text-red-700 font-medium underline" (click)="retryPull()" data-testid="pull-retry">{{ 'Повторити завантаження' | t }}</button>
      </div>
    </section>

    <div *ngIf="activePull()" class="mb-4 p-3 bg-slate-50 rounded-lg border" data-testid="pull-progress">
      <div class="flex justify-between text-sm mb-1">
        <span class="font-medium">{{ activePull()?.model }} · {{ activePull()?.current_phase || 'pull' }}</span>
        <span>{{ activePull()?.progress || 0 }}%</span>
      </div>
      <div class="w-full bg-slate-200 rounded-full h-2">
        <div class="bg-emerald-600 h-2 rounded-full transition-all"
             [style.width.%]="activePull()?.progress || 0"></div>
      </div>
      <div *ngIf="activePull()?.message" class="text-slate-600 text-sm mt-1">{{ (activePull()?.message) | t }}</div>
      <div class="text-sm mt-1" data-testid="pull-status">{{ pullStatusText(activePull()?.status) | t }}</div>
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

    <section data-testid="installed-models">
      <h4 class="text-sm font-semibold mb-2">{{ 'Встановлені моделі (CORE)' | t }}</h4>
      <app-loading-state *ngIf="loading()" />
      <app-error-state *ngIf="error()" [message]="error()!" (retry)="loadModels()" />
      <p *ngIf="!loading() && !error() && !models().length" class="text-sm text-slate-500" data-testid="installed-empty">{{ 'Моделей ще не встановлено' | t }}</p>
      <div *ngIf="!loading() && !error()" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
      <div *ngFor="let model of models()" class="flex justify-between gap-2 border border-slate-200 rounded-xl p-4" [attr.data-testid]="'model-row-' + model.name">
        <span>{{ model.name }}</span>
        <button (click)="deleteModel(model.name)" [disabled]="!canDeleteModel" [title]="(deleteModelReason || 'Видалити модель') | t" class="text-red-600 disabled:opacity-40" [attr.data-testid]="'model-delete-' + model.name">{{ 'Видалити' | t }}</button>
      </div>
      </div>
    </section>

    <div class="mt-6 pt-4 border-t" data-testid="voice-preview">
      <h4 class="text-sm font-semibold mb-2">{{ 'Перевірка голосу (TTS)' | t }}</h4>
      <p *ngIf="voicesError()" class="text-sm text-amber-700 mb-2" data-testid="voice-catalog-error">{{ voicesError()! | t }}
        <button type="button" class="underline ml-1" (click)="loadVoices()" data-testid="voice-catalog-retry">{{ 'Повторити' | t }}</button></p>
      <div class="flex flex-wrap items-end gap-2 mb-2">
        <label class="flex-1 min-w-48 text-xs text-slate-600">{{ 'Текст для перевірки' | t }}
          <input [(ngModel)]="previewText" class="mt-1 w-full border rounded px-3 py-2 text-sm"
                 placeholder="{{ 'Текст для перевірки' | t }}" maxlength="500" data-testid="voice-preview-text"></label>
        <label class="text-xs text-slate-600">{{ 'Голос' | t }}
          <select [(ngModel)]="previewVoice" class="mt-1 block border rounded px-3 py-2 text-sm" data-testid="voice-preview-voice">
            <option value="default">{{ 'За замовчуванням (голос рушія)' | t }}</option>
            <option *ngFor="let voice of voices()" [value]="voice.id">{{ voice.name }} ({{ voice.id }})</option>
          </select></label>
        <label class="text-xs text-slate-600">{{ 'Швидкість, слів за хвилину' | t }}
          <input [(ngModel)]="previewSpeed" type="number" [min]="speedRange().min" [max]="speedRange().max" step="10"
                 class="mt-1 block w-28 border rounded px-2 py-2 text-sm" data-testid="voice-preview-speed"></label>
        <button (click)="runPreview()" [disabled]="previewRunning() || !previewText.trim()"
                class="px-4 py-2 bg-sky-600 text-white rounded disabled:opacity-50" data-testid="voice-preview-button">
          {{ (previewRunning() ? 'Синтез...' : 'Прослухати') | t }}
        </button>
      </div>
      <p class="text-xs text-slate-500 mb-2" data-testid="voice-speed-hint">{{ 'Темп мовлення рушія espeak-ng: більше значення — швидше. Допустимо' | t }} {{ speedRange().min }}–{{ speedRange().max }}.</p>
      <div *ngIf="previewError()" class="text-red-600 text-sm" data-testid="voice-preview-error" role="alert">{{ (previewError()) | t }}</div>
      <audio *ngIf="previewUrl()" [src]="previewUrl()" controls autoplay data-testid="voice-preview-audio"></audio>
    </div>
  </div>
</div>`,
})
export class ModelsSectionComponent implements OnInit, OnDestroy {
  models = signal<ModelInfo[]>([]);
  catalog = signal<ModelCatalogEntry[]>([]);
  catalogError = signal<string | null>(null);
  catalogChoice = signal('');
  pullError = signal<PullFailure | null>(null);
  nodes = signal<ModelNodeInfo[]>([]);
  voices = signal<VoiceOption[]>([]);
  voicesError = signal<string | null>(null);
  speedRange = signal<{ min: number; max: number }>({ min: 80, max: 450 });
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
    this.loadCatalog();
    this.loadVoices();
  }

  loadCatalog(): void {
    this.catalogError.set(null);
    this.settings.modelCatalog().subscribe({
      next: (data) => this.catalog.set(data.models || []),
      error: (err) => { this.catalog.set([]); this.catalogError.set(err.message || 'Каталог моделей недоступний'); },
    });
  }

  chooseFromCatalog(name: string): void {
    this.catalogChoice.set(name);
    if (name) { this.modelName = name; this.pullError.set(null); }
  }

  /** Installed on the selected target: CORE's own list or the node's reported catalog. */
  installedOnTarget(name: string): boolean {
    const installed = this.targetNode
      ? this.nodes().find(node => node.node_name === this.targetNode)?.models ?? []
      : this.models().map(model => model.name);
    return installed.includes(name);
  }

  gigabytes(bytes: number): string { return (bytes / 1e9).toFixed(1); }

  pullStatusText(status?: string): string {
    return ({ QUEUED: 'У черзі', RUNNING: 'Завантажується', COMPLETED: 'Модель завантажено', FAILED: 'Завантаження не вдалося', CANCELLED: 'Завантаження скасовано' } as Record<string, string>)[status || ''] || '';
  }

  retryPull(): void { this.pullModel(); }

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
    this.voicesError.set(null);
    this.settings.voices().subscribe({
      next: (data) => {
        this.voices.set(data.voices || []);
        if (data.speed_range) this.speedRange.set(data.speed_range);
        if (!data.voices?.length) this.voicesError.set('Голосовий сервіс не повідомив жодного голосу');
      },
      // An unavailable catalog is shown, not replaced by a list that only looks valid.
      error: (err) => { this.voices.set([]); this.voicesError.set(err.message || 'Список голосів недоступний'); },
    });
  }

  pullModel(): void {
    const name = this.modelName.trim();
    if (!name || this.pulling() || !this.canPullModel) return;
    this.modelName = name;
    // Invalid input stays in the form for correction instead of replacing the page.
    if (!MODEL_NAME.test(name)) { this.pullError.set(pullFailure(name, 422)); return; }
    this.pulling.set(true);
    this.pullError.set(null);
    this.settings.pullModel(name, this.targetNode || undefined).subscribe({
      next: (op) => {
        this.activePull.set(op as OperationState);
        this.startPolling(op.operation_id);
      },
      error: (err) => { this.pulling.set(false); this.pullError.set(pullFailure(err.message || '', err.status)); },
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
      error: (err) => { this.pullError.set(pullFailure(err.message || '', err.status)); },
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
        error: (err) => this.toast.show(err.message || 'Не вдалося видалити модель', 'error'),
      });
    });
  }

  runPreview(): void {
    const text = this.previewText.trim();
    if (!text || this.previewRunning()) return;
    const speed = Number(this.previewSpeed);
    const { min, max } = this.speedRange();
    this.previewError.set(null);
    this.clearAudio();  // a previous recording never stands for the new request
    if (!Number.isInteger(speed) || speed < min || speed > max) {
      this.previewError.set('Швидкість має бути цілим числом у допустимих межах');
      return;
    }
    this.previewRunning.set(true);
    this.settings.previewVoice({ text, voice: this.previewVoice, speed }).subscribe({
      next: (blob) => {
        this.previewRunning.set(false);
        if (!blob.size) { this.previewError.set('Голосовий сервіс не повернув аудіо'); return; }
        this.audioUrl = URL.createObjectURL(blob);
        this.previewUrl.set(this.audioUrl);
      },
      error: (err) => {
        this.previewRunning.set(false);
        this.previewError.set(err?.error?.detail || err?.message || 'Не вдалося синтезувати голос');
      },
    });
  }

  private clearAudio(): void {
    if (this.audioUrl) URL.revokeObjectURL(this.audioUrl);
    this.audioUrl = null;
    this.previewUrl.set(null);
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
            if (status === 'COMPLETED') {
              this.modelName = '';
              this.catalogChoice.set('');
              this.loadModels();
              this.loadNodes();
              this.toast.show('Модель завантажено', 'success');
            } else if (status === 'CANCELLED') {
              this.toast.show('Завантаження скасовано', 'info');
            } else {
              // The name stays in the form, so the pull can be retried or corrected.
              this.pullError.set(pullFailure(op.error || ''));
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
