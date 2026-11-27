import { TPipe } from '../../core/i18n/t.pipe';
import { inject, Component, OnInit, computed, signal } from '@angular/core';
import { PolicyService } from '../../core/services/policy.service';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { SystemApiService } from '../../core/api/system.api';
import { SettingsApiService, ProviderMatrixEntry } from '../../core/api/settings.api';
import { ToastService } from '../../core/services/toast.service';
import { SystemStatus } from '../../core/models';
import { LoadingStateComponent } from '../../shared/loading-state.component';
import { ErrorStateComponent } from '../../shared/error-state.component';

@Component({
  selector: 'app-settings-system-info',
  standalone: true,
  imports: [TPipe, CommonModule, FormsModule, LoadingStateComponent, ErrorStateComponent],
  template: `<div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-system-info"><h3 class="text-lg font-semibold mb-4">{{ 'Система' | t }}</h3><app-loading-state *ngIf="loading()" /><app-error-state *ngIf="error()" [message]="error()!" (retry)="ngOnInit()" /><div *ngIf="status()" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4" data-testid="system-info"><p class="border border-slate-200 rounded-xl p-4 text-sm">{{ 'Стан:' | t }} {{ (systemStateLabel) | t }}</p><p class="border border-slate-200 rounded-xl p-4 text-sm">{{ 'Версія:' | t }} {{ status()?.version || '-' }}</p><p class="border border-slate-200 rounded-xl p-4 text-sm">{{ 'Ядро:' | t }} {{ (status()?.core || '-') | t }}</p><p class="border border-slate-200 rounded-xl p-4 text-sm">{{ 'База даних:' | t }} {{ (status()?.postgres || '-') | t }}</p><p class="border border-slate-200 rounded-xl p-4 text-sm">Redis: {{ (status()?.redis || '-') | t }}</p><p class="border border-slate-200 rounded-xl p-4 text-sm">{{ 'Поточна:' | t }} <span data-testid="status-update-current-version">{{ status()?.update?.['current_version'] || '-' }}</span></p><p class="border border-slate-200 rounded-xl p-4 text-sm">{{ 'Доступна:' | t }} <span data-testid="status-update-available-version">{{ status()?.update?.['available_version'] || '-' }}</span></p></div>
  <div *ngIf="backendsList().length" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 mt-4" data-testid="backends-table">
    <section *ngFor="let entry of backendsList(); trackBy: trackSlot" class="border border-slate-200 rounded-xl p-4" [attr.data-testid]="'backend-row-' + entry[0]">
      <div class="flex items-center justify-between mb-2">
        <h4 class="font-semibold text-slate-900">{{ entry[0] }}</h4>
        <span class="text-xs" [class.text-emerald-700]="entry[1]?.['configured']" [class.text-slate-500]="!entry[1]?.['configured']">{{ 'Налаштовано' | t }}: {{ (entry[1]?.['configured'] ? 'Так' : 'Ні') | t }}</span>
      </div>
      <p class="text-sm text-slate-500">{{ 'Бекенд' | t }}</p>
      <div class="text-sm font-medium mb-3" [attr.data-testid]="'backend-current-' + entry[0]">{{ entry[1]?.['backend'] || '-' }}<small *ngIf="entry[1]?.['activation_status']" class="block text-amber-700">{{ 'Очікує Worker:' | t }} {{ entry[1]?.['selected_backend'] }}</small></div>
      <div class="flex flex-wrap items-center gap-2">
          <ng-container *ngIf="switchable(entry[0], entry[1])">
            <select class="border rounded px-2 py-1 mr-2" [attr.data-testid]="'backend-select-' + entry[0]"
                    [ngModel]="choice(entry[0], entry[1])"
                    (ngModelChange)="selected[entry[0]] = $any($event)">
              <option *ngFor="let option of entry[1]?.['options'] || []" [value]="option">{{ option }}</option>
            </select>
            <button class="px-2 py-1 bg-emerald-600 text-white rounded text-sm"
                    [disabled]="switching() || (choice(entry[0], entry[1]) === entry[1]?.['backend'] && choice(entry[0], entry[1]) === (entry[1]?.['selected_backend'] || entry[1]?.['backend']))"
                    (click)="switchBackend(entry[0], choice(entry[0], entry[1]))"
                    [attr.data-testid]="'backend-switch-' + entry[0]">{{ 'Застосувати' | t }}</button>
          </ng-container>
          <span *ngIf="!switchable(entry[0], entry[1])" class="text-slate-400 text-sm">—</span>
        </div>
    </section>
  </div>
  <div class="border border-slate-200 rounded-xl p-4 mt-4 max-w-md" data-testid="turn-timeout">
    <label class="block text-sm font-semibold mb-1" for="turn-timeout">{{ 'Очікування черги між однаковими вузлами, с' | t }}</label>
    <p class="text-xs text-slate-500 mb-2">{{ 'Скільки вузол чекає, поки свою задачу візьме сусід. Після цього забирає її сам.' | t }}</p>
    <div class="flex gap-2">
      <input id="turn-timeout" type="number" min="0" max="600" step="1" class="w-28 px-2 py-1 border border-slate-200 rounded text-sm" [(ngModel)]="turnTimeout" [disabled]="!canSaveTurnTimeout()">
      <button type="button" class="px-3 py-1 bg-emerald-600 text-white rounded text-sm disabled:opacity-50" [disabled]="!canSaveTurnTimeout()" (click)="saveTurnTimeout()" data-testid="turn-timeout-save">{{ 'Зберегти' | t }}</button>
    </div>
  </div>
  <div class="border border-slate-200 rounded-xl p-4 mt-4 max-w-xl" data-testid="lab-panel">
    <label class="block text-sm font-semibold mb-1" for="lab-panel-url">{{ 'Адреса панелі LAB' | t }}</label>
    <p class="text-xs text-slate-500 mb-2">{{ 'Необов’язково. Окрема лабораторія на своїй VM, наприклад https://10.10.10.20:8444. Після збереження адміністратор бачить пункт «Лабораторія» й входить у LAB через Vertep. Порожнє поле вимикає підключення.' | t }}</p>
    <div class="flex gap-2">
      <input id="lab-panel-url" type="url" class="grow px-2 py-1 border border-slate-200 rounded text-sm" placeholder="https://" [(ngModel)]="labPanelUrl" [disabled]="!canSaveTurnTimeout()" data-testid="lab-panel-url">
      <button type="button" class="px-3 py-1 bg-emerald-600 text-white rounded text-sm disabled:opacity-50" [disabled]="!canSaveTurnTimeout()" (click)="saveLabPanel()" data-testid="lab-panel-save">{{ 'Зберегти' | t }}</button>
    </div>
    <div class="mt-3 pt-3 border-t border-slate-100 space-y-2 text-sm" data-testid="lab-connection">
      @if (labConnected()) {
        <p class="text-emerald-700" data-testid="lab-connected">✓ {{ 'Лабораторію підключено' | t }} · {{ labPanelUrl }}</p>
        <button type="button" class="px-3 py-1 border border-red-300 text-red-700 rounded text-sm disabled:opacity-50" [disabled]="!canSaveTurnTimeout()" (click)="disconnectLab()" data-testid="lab-disconnect">{{ 'Відключити лабораторію' | t }}</button>
      } @else {
        <p class="text-xs text-slate-500">{{ 'Лабораторія на окремій VM підключається сама: виконайте на ній показану команду. Vertep видасть сертифікат панелі, доступ до вільного GPU й вхід через Vertep — нічого не треба копіювати вручну. Команда діє одну годину й лише один раз.' | t }}</p>
        <button type="button" class="px-3 py-1 bg-emerald-600 text-white rounded text-sm disabled:opacity-50" [disabled]="!canSaveTurnTimeout()" (click)="connectLab()" data-testid="lab-connect">{{ 'Підключити лабораторію' | t }}</button>
        @if (labCommand()) {
          <pre class="whitespace-pre-wrap break-all bg-slate-50 border border-slate-200 rounded p-2 text-xs select-all" data-testid="lab-connect-command">{{ labCommand() }}</pre>
        }
      }
    </div>
  </div>
  <div *ngIf="switchError()" class="text-red-600 text-sm mt-2" data-testid="backend-switch-error">{{ (switchError()) | t }}</div>
</div>`,
})
export class SystemInfoSectionComponent implements OnInit {
  status = signal<SystemStatus | null>(null);
  loading = signal(false);
  error = signal<string | null>(null);
  switching = signal(false);
  switchError = signal<string | null>(null);
  selected: Record<string, string> = {};
  turnTimeout = 45;
  labPanelUrl = '';
  labConnected = signal(false);
  labCommand = signal('');
  private policy = inject(PolicyService);

  canSaveTurnTimeout(): boolean { return this.policy.can('update_settings').allowed; }

  saveTurnTimeout(): void {
    this.settingsApi.saveDispatchSettings(Number(this.turnTimeout)).subscribe({
      next: (saved) => { this.turnTimeout = saved.worker_turn_timeout_seconds; this.toast.show('Збережено', 'success'); },
      error: (err) => this.toast.show(err.message, 'error'),
    });
  }

  saveLabPanel(): void {
    this.settingsApi.saveLabPanel(this.labPanelUrl.trim()).subscribe({
      next: (saved) => { this.labPanelUrl = saved.url || ''; this.toast.show('Збережено', 'success'); },
      error: (err) => this.toast.show(err.message, 'error'),
    });
  }

  /** #491: a one-time command the LAB VM runs to connect itself (token + pinned CORE certificate). */
  connectLab(): void {
    this.settingsApi.labConnectionToken().subscribe({
      next: (issued) => this.labCommand.set(
        'curl -fL https://raw.githubusercontent.com/fylypovych/vertep/main/bootstrap-lab.sh -o vertep-bootstrap-lab.sh && '
        + `sudo VERTEP_CORE_URL=${window.location.origin} VERTEP_LAB_TOKEN=${issued.token} `
        + `VERTEP_CORE_PIN=${issued.core_certificate_sha256} bash ./vertep-bootstrap-lab.sh`),
      error: (err) => this.toast.show(err.message, 'error'),
    });
  }

  disconnectLab(): void {
    this.settingsApi.labDisconnect().subscribe({
      next: () => { this.labConnected.set(false); this.labPanelUrl = ''; this.toast.show('Збережено', 'success'); },
      error: (err) => this.toast.show(err.message, 'error'),
    });
  }

  private readonly switchableSlots = new Set(['llm', 'tts', 'compute', 'image', 'video', 'video_engine']);

  get systemOk(): boolean {
    const s = this.status()?.system?.state?.toUpperCase();
    return s === 'NORMAL' || s === 'OK' || s === 'HEALTHY';
  }

  get systemStateLabel(): string {
    const state = this.status()?.system?.state?.toUpperCase() ?? 'NORMAL';
    const labels: Record<string, string> = {
      NORMAL: 'Нормальний', OK: 'Працює', HEALTHY: 'Працює',
      MAINTENANCE: 'Обслуговування', UPDATING: 'Оновлення',
      EMERGENCY: 'Аварія', FAILED: 'Помилка', ERROR: 'Помилка',
    };
    return labels[state] ?? state;
  }

  readonly backendsList = computed<[string, ProviderMatrixEntry][]>(() => {
    const providers = this.status()?.providers;
    if (!providers) return [];
    return Object.entries(providers as unknown as Record<string, ProviderMatrixEntry>);
  });

  trackSlot(_index: number, entry: [string, ProviderMatrixEntry]): string {
    return entry[0];
  }

  constructor(
    private systemApi: SystemApiService,
    private settingsApi: SettingsApiService,
    private toast: ToastService,
  ) {}

  switchable(slot: string, entry: ProviderMatrixEntry | undefined): boolean {
    return !!entry && this.switchableSlots.has(slot) && Array.isArray(entry.options) && entry.options.length > 1;
  }

  choice(slot: string, entry: ProviderMatrixEntry | undefined): string {
    return this.selected[slot] || String(entry?.backend || '');
  }

  switchBackend(slot: string, backend: string): void {
    if (!backend || this.switching()) return;
    this.switching.set(true);
    this.switchError.set(null);
    this.settingsApi.switchProvider(slot, backend).subscribe({
      next: (result) => {
        this.switching.set(false);
        this.selected[slot] = result.backend;
        const status = { ...(this.status() as unknown as Record<string, unknown>) };
        if (result.matrix) status['providers'] = result.matrix;
        this.status.set(status as unknown as SystemStatus);
        if (result.worker_activation?.status === 'AWAITING_WORKER') {
          this.toast.show('Конфігурацію збережено; очікується підтвердження Worker.', 'info');
        } else {
          this.toast.show(`Слот ${slot}: ${result.backend}`, 'success');
        }
      },
      error: (err) => {
        this.switching.set(false);
        this.switchError.set(err?.error?.detail || err?.message || 'Не вдалося змінити бекенд');
      },
    });
  }

  ngOnInit(): void {
    this.settingsApi.dispatchSettings().subscribe({ next: (value) => { this.turnTimeout = value.worker_turn_timeout_seconds; }, error: () => {} });
    this.settingsApi.labPanel().subscribe({ next: (value) => { this.labPanelUrl = value.url || ''; }, error: () => {} });
    this.settingsApi.labConnection().subscribe({ next: (value) => this.labConnected.set(value.connected), error: () => {} });
    this.error.set(null);
    this.loading.set(true);
    this.systemApi.status().subscribe({
      next: (s) => { this.status.set(s); this.loading.set(false); },
      error: (err) => { this.error.set(err.message || 'Не вдалося завантажити стан'); this.loading.set(false); },
    });
  }
}
