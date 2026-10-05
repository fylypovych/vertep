import { Component, OnInit, computed, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { SettingsApiService, EffectiveEngineConfig, EngineField } from '../../core/api/settings.api';
import { ToastService } from '../../core/services/toast.service';
import { PolicyService } from '../../core/services/policy.service';
import { LoadingStateComponent } from '../../shared/loading-state.component';
import { ErrorStateComponent } from '../../shared/error-state.component';

/** Settings → Движок відео (Issue #122 P8).

    The section renders one engine choice for the operator: the user-facing names, the
    inputs an external engine still needs, its readiness, and which engine is actually
    effective right now. The displayed choice is always read back from the API, so it can
    never drift away from what the running executor uses. */
@Component({
  selector: 'app-settings-video-engine',
  standalone: true,
  imports: [CommonModule, FormsModule, LoadingStateComponent, ErrorStateComponent],
  template: `<div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-video-engine">
    <h3 class="text-lg font-semibold mb-1">Движок відео</h3>
    <p class="text-sm text-slate-500 mb-4">Движок, яким збирається фінальне відео. Зміна застосовується одразу з перевіркою готовності; невдале застосування повертає попередній движок.</p>
    <app-loading-state *ngIf="loading()" />
    <app-error-state *ngIf="error()" [message]="error()!" />
    <div *ngIf="config() as cfg" data-testid="video-engine-panel">
      <div class="grid grid-cols-1 md:grid-cols-3 gap-3 text-sm">
        <p>Обрано: <span data-testid="video-engine-selected">{{ selectedLabel() }}</span></p>
        <p>Діє: <span data-testid="video-engine-effective">{{ effectiveLabel() }}</span></p>
        <p>Готовність: <span data-testid="video-engine-ready">{{ cfg.ready ? 'Готовий' : 'Не готовий' }}</span></p>
        <p *ngIf="cfg.reason" class="text-amber-700 md:col-span-3" data-testid="video-engine-reason">Причина: {{ reasonLabel(cfg.reason) }}</p>
        <p *ngIf="!cfg.agree" class="text-red-600 md:col-span-3" data-testid="video-engine-mismatch">Обраний движок не відповідає застосованому — виконання відхилено без підміни.</p>
        <p>Ревізія конфігурації: <code class="text-xs" data-testid="video-engine-revision">{{ cfg.config_revision }}</code></p>
        <p>Стан системи: <span data-testid="video-engine-system-state">{{ cfg.system_state }}</span></p>
        <p>Upstream: <code class="text-xs" data-testid="video-engine-upstream">{{ cfg.upstream_reference || '—' }}</code></p>
      </div>
      <div class="mt-4 grid grid-cols-1 md:grid-cols-2 gap-3">
        <label class="text-sm" for="video-engine-choice">Движок</label>
        <select id="video-engine-choice" class="border rounded px-2 py-1" data-testid="video-engine-select"
                [ngModel]="choice()" (ngModelChange)="choice.set($any($event))"
                [disabled]="!canEdit() || applying()">
          <option *ngFor="let option of cfg.options" [value]="option.id">{{ option.label }}</option>
        </select>
      </div>
      <div *ngIf="endpointField() as field" class="mt-3 grid grid-cols-1 md:grid-cols-2 gap-3">
        <label class="text-sm" for="video-engine-endpoint">Адреса runtime ({{ field.env }})</label>
        <input id="video-engine-endpoint" class="border rounded px-2 py-1" data-testid="video-engine-endpoint"
               [ngModel]="endpoint()" (ngModelChange)="endpoint.set($any($event))"
               [disabled]="!canEdit() || applying()" placeholder="http://host:port" />
      </div>
      <p *ngIf="tokenField() as field" class="mt-2 text-sm" data-testid="video-engine-token-state">
        Токен ({{ field.env }}): {{ field.configured ? 'налаштовано' : 'не налаштовано' }}
      </p>
      <div class="mt-4 flex flex-wrap gap-2">
        <button class="px-3 py-1 bg-emerald-600 text-white rounded text-sm"
                data-testid="video-engine-apply"
                [disabled]="!canApply()"
                (click)="apply()">Застосувати</button>
        <button class="px-3 py-1 bg-slate-700 text-white rounded text-sm"
                data-testid="video-engine-return-native"
                [disabled]="!canEdit() || applying() || cfg.effective === 'native'"
                (click)="returnNative()">Повернути Vertep Native</button>
      </div>
      <p *ngIf="lockedReason() as reason" class="mt-2 text-amber-700 text-sm" data-testid="video-engine-locked">{{ reason }}</p>
      <div *ngIf="applyError()" class="text-red-600 text-sm mt-2" data-testid="video-engine-error">{{ applyError() }}</div>
      <div *ngIf="rollbackNotice()" class="text-amber-700 text-sm mt-2" data-testid="video-engine-rollback">{{ rollbackNotice() }}</div>
    </div>
  </div>`,
})
export class VideoEngineSectionComponent implements OnInit {
  config = signal<EffectiveEngineConfig | null>(null);
  loading = signal(false);
  error = signal<string | null>(null);
  applying = signal(false);
  applyError = signal<string | null>(null);
  rollbackNotice = signal<string | null>(null);
  choice = signal('native');
  endpoint = signal('');

  constructor(
    private settingsApi: SettingsApiService,
    private toast: ToastService,
    private policy: PolicyService,
  ) {}

  /** The operator may change the engine only with rights and in a state that permits it.

      Both guards already exist elsewhere: ``update_settings`` is refused for viewers and
      for the system states that block configuration, so the section asks the same policy
      the API enforces instead of hiding the control behind a second rule of its own. */
  readonly canEdit = computed(() => {
    const config = this.config();
    return !!config?.change_allowed && this.policy.can('update_settings').allowed;
  });

  readonly lockedReason = computed<string | null>(() => {
    const config = this.config();
    if (!config) return null;
    const policy = this.policy.can('update_settings');
    if (!policy.allowed) return `Зміна движка недоступна: ${policy.reason}.`;
    if (!config.change_allowed) {
      return `Зміна движка недоступна в стані системи «${config.system_state}».`;
    }
    return null;
  });

  readonly selectedLabel = computed(() => this.labelOf(this.config()?.selected));

  private labelOf(id?: string | null): string {
    const options = this.config()?.options ?? [];
    return options.find(option => option.id === id)?.label ?? id ?? '—';
  }

  readonly effectiveLabel = computed(() => this.labelOf(this.config()?.effective));

  readonly endpointField = computed<EngineField | null>(
    () => this.activeFields().find(field => field.name === 'endpoint') ?? null,
  );

  readonly tokenField = computed<EngineField | null>(
    () => this.activeFields().find(field => field.name === 'token') ?? null,
  );

  /** The runtime address the operator typed is not the one this engine is running with.

      An external engine is pointed at its runtime through an endpoint, so repointing it
      is a real change of the effective configuration even when the engine itself stays
      the same. Binding Apply to the engine name alone left the current engine's runtime
      address unchangeable from Settings (Issue #122 P8). */
  readonly endpointChanged = computed(() => {
    const field = this.endpointField();
    if (!field) return false;
    return this.endpoint().trim() !== String(field.value ?? '').trim();
  });

  /** Nothing to apply while the form describes exactly what is already effective.

      Both halves of the decision are compared: the engine and, for an external engine,
      the runtime address it uses. */
  readonly canApply = computed(() => {
    const config = this.config();
    if (!config || !this.canEdit() || this.applying()) return false;
    return this.choice() !== config.effective || this.endpointChanged();
  });

  /** The inputs of the engine the operator is choosing, falling back to the effective one. */
  private activeFields(): EngineField[] {
    const config = this.config();
    if (!config) return [];
    return config.required_fields?.[this.choice()] ?? config.fields ?? [];
  }

  reasonLabel(reason: string): string {
    const labels: Record<string, string> = {
      upstream_unreachable: 'runtime недоступний',
      upstream_schema_unsupported: 'непідтримувана схема відповіді runtime',
      upstream_not_ready: 'runtime ще не готовий',
      endpoint_missing: 'не вказано адресу runtime',
      endpoint_mismatch: 'адреса змінилася після застосування',
      secret_missing: 'не налаштовано токен',
      config_revision_mismatch: 'не збігається ревізія конфігурації',
      native_engine: 'Vertep Native не потребує зовнішнього runtime',
      runtime_not_ready: 'runtime не готовий',
      unknown: 'невідома причина',
    };
    return labels[reason] ?? reason;
  }

  private load(probe: boolean): void {
    this.settingsApi.videoEngine(probe).subscribe({
      next: (config) => {
        this.config.set(config);
        this.error.set(null);
        this.choose(config.effective, config);
      },
      error: (err) => this.error.set(err?.error?.detail || err?.message || 'Не вдалося завантажити конфігурацію движка'),
    });
  }

  /** Keep the form in sync with the API answer without overwriting the operator's input. */
  private choose(effective: string, config: EffectiveEngineConfig): void {
    this.choice.set(effective);
    // The address is read from the same place the input is rendered from, so the form
    // never starts out describing something other than what it displays.
    const field = (config.required_fields?.[effective] ?? config.fields ?? [])
      .find(item => item.name === 'endpoint');
    if (field?.value) {
      this.endpoint.set(field.value);
    }
  }

  apply(): void {
    const config = this.config();
    if (!config || this.applying()) return;
    const target = this.choice();
    const field = this.endpointField();
    this.applying.set(true);
    this.applyError.set(null);
    this.rollbackNotice.set(null);
    const endpoint = field && this.endpoint().trim() ? this.endpoint().trim() : undefined;
    this.settingsApi.switchVideoEngine(target, endpoint).subscribe({
      next: (result) => {
        this.applying.set(false);
        const applied = result.effective_engine ?? null;
        if (applied?.effective && applied.effective !== target) {
          this.rollbackNotice.set(
            `Застосування не підтверджено: рух залишився на «${this.labelOf(applied.effective)}».`,
          );
        } else {
          this.toast.show(`Движок відео: ${this.labelOf(target)}`, 'success');
        }
        this.load(true);
      },
      error: (err) => {
        this.applying.set(false);
        this.applyError.set(err?.error?.detail || err?.message || 'Не вдалося застосувати движок');
        this.rollbackNotice.set('Попередній движок збережено.');
        this.load(false);
      },
    });
  }

  returnNative(): void {
    this.choice.set('native');
    this.apply();
  }

  ngOnInit(): void {
    this.loading.set(true);
    this.settingsApi.videoEngine(false).subscribe({
      next: (config) => {
        this.config.set(config);
        this.choose(config.effective, config);
        this.loading.set(false);
      },
      error: (err) => {
        this.error.set(err?.error?.detail || err?.message || 'Не вдалося завантажити конфігурацію движка');
        this.loading.set(false);
      },
    });
  }
}