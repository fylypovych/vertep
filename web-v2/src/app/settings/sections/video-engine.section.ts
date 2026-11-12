import { TPipe } from '../../core/i18n/t.pipe';
import { Component, OnDestroy, OnInit, computed, signal } from '@angular/core';
import { Subscription, timer } from 'rxjs';
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
  imports: [TPipe, CommonModule, FormsModule, LoadingStateComponent, ErrorStateComponent],
  template: `<div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-video-engine">
    <h3 class="text-lg font-semibold mb-1">{{ 'Движок відео' | t }}</h3>
    <p class="text-sm text-slate-500 mb-4">{{ 'Движок, яким збирається фінальне відео. Зміна застосовується одразу з перевіркою готовності; невдале застосування повертає попередній движок.' | t }}</p>
    <app-loading-state *ngIf="loading()" />
    <app-error-state *ngIf="error()" [message]="error()!" />
    <div *ngIf="config() as cfg" data-testid="video-engine-panel">
      <div class="grid grid-cols-1 md:grid-cols-3 gap-3 text-sm">
        <p>{{ 'Обрано:' | t }} <span data-testid="video-engine-selected">{{ (selectedLabel()) | t }}</span></p>
        <p>{{ 'Діє:' | t }} <span data-testid="video-engine-effective">{{ (effectiveLabel()) | t }}</span></p>
        <p>{{ 'Готовність движка складання:' | t }} <span data-testid="video-engine-ready">{{ (cfg.ready ? 'Готовий' : 'Не готовий') | t }}</span></p>
        <p *ngIf="cfg.reason" class="text-amber-700 md:col-span-3" data-testid="video-engine-reason">{{ 'Причина:' | t }} {{ (reasonLabel(cfg.reason)) | t }}</p>
        <p *ngIf="!cfg.agree" class="text-red-600 md:col-span-3" data-testid="video-engine-mismatch">{{ 'Обраний движок не відповідає застосованому — виконання відхилено без підміни.' | t }}</p>
        <p>{{ 'Ревізія конфігурації:' | t }} <code class="text-xs" data-testid="video-engine-revision">{{ cfg.config_revision }}</code></p>
        <p>{{ 'Режим системи:' | t }} <span data-testid="video-engine-system-state">{{ (cfg.system_state) | t }}</span></p>
        <p>Upstream: <code class="text-xs" data-testid="video-engine-upstream">{{ cfg.upstream_reference || '—' }}</code></p>
        <p class="md:col-span-3 text-xs text-slate-500" data-testid="video-engine-scope">{{ scopeNote() | t }}</p>
      </div>
      <p *ngIf="draftLabel() as draft" class="mt-3 text-sm text-amber-700" data-testid="video-engine-draft">{{ 'У формі вибрано' | t }} «{{ draft | t }}» — {{ 'ще не застосовано. Поля «Обрано» й «Діє» показують чинну конфігурацію.' | t }}</p>
      <div class="mt-4 grid grid-cols-1 md:grid-cols-2 gap-3">
        <label class="text-sm" for="video-engine-choice">{{ 'Движок' | t }}</label>
        <div class="flex items-center gap-2">
          <select id="video-engine-choice" class="border rounded px-2 py-1 grow" data-testid="video-engine-select"
                  [ngModel]="choice()" (ngModelChange)="choice.set($any($event)); infoOpen.set(false)"
                  [disabled]="!canEdit() || applying()">
            <option *ngFor="let option of cfg.options" [value]="option.id">{{ (option.label) | t }}</option>
          </select>
          <button type="button" class="w-6 h-6 shrink-0 rounded-full border border-amber-500 text-amber-700 text-sm font-bold"
                  data-testid="video-engine-info-toggle" aria-label="{{ 'Пояснення до обраного движка' | t }}"
                  [attr.aria-expanded]="infoOpen()" [title]="(choiceExplanation()) | t"
                  (click)="infoOpen.set(!infoOpen())">!</button>
        </div>
        <p *ngIf="infoOpen()" class="md:col-start-2 text-sm text-slate-600 bg-slate-50 border border-slate-200 rounded p-2"
           data-testid="video-engine-info">{{ (choiceExplanation()) | t }}</p>
      </div>
      <div *ngIf="localRuntime() as local" class="mt-3 text-sm" data-testid="video-engine-runtime-mode">
        <label class="mr-4"><input type="radio" name="engine-runtime" value="local" [ngModel]="runtimeMode()" (ngModelChange)="runtimeMode.set($event)"
               [disabled]="!canEdit() || applying() || !local.available" data-testid="video-engine-runtime-local"> {{ 'Локальний runtime у складі Vertep' | t }}</label>
        <label><input type="radio" name="engine-runtime" value="external" [ngModel]="runtimeMode()" (ngModelChange)="runtimeMode.set($event)"
               [disabled]="!canEdit() || applying()" data-testid="video-engine-runtime-external"> {{ 'Зовнішній runtime' | t }}</label>
        <p *ngIf="runtimeMode() === 'local'" class="mt-1 text-xs text-slate-500">{{ 'Vertep сам розгорне контейнер MoneyPrinterTurbo, підключить його за внутрішньою адресою й використає згенерований ключ. Нічого вводити не потрібно.' | t }}</p>
        <p *ngIf="!local.available" class="mt-1 text-xs text-amber-700" data-testid="video-engine-local-unavailable">{{ 'Локальний runtime недоступний на цьому сервері (образ випускається лише для amd64).' | t }}</p>
        <p *ngIf="local.deploying || deploying()" class="mt-1 text-sm text-slate-600" data-testid="video-engine-deploying">{{ 'Розгортається локальний runtime…' | t }} {{ (deploymentPhase()) | t }}</p>
        <p *ngIf="local.deployment_error" class="mt-1 text-sm text-red-600" data-testid="video-engine-deploy-error">{{ 'Розгортання runtime не вдалося' | t }}: {{ local.deployment_error }}</p>
      </div>
      <div *ngIf="endpointField() as field" class="mt-3 grid grid-cols-1 md:grid-cols-2 gap-3">
        <label class="text-sm" for="video-engine-endpoint">{{ 'Адреса runtime (' | t }}{{ field.env }})</label>
        <input id="video-engine-endpoint" class="border rounded px-2 py-1" data-testid="video-engine-endpoint"
               [ngModel]="endpoint()" (ngModelChange)="endpoint.set($any($event))"
               [disabled]="!canEdit() || applying()" placeholder="http://host:port" />
      </div>
      <p *ngIf="tokenField() as field" class="mt-2 text-sm" data-testid="video-engine-token-state">
        {{ 'Токен (' | t }}{{ field.env }}): {{ (field.configured ? 'налаштовано' : 'не налаштовано') | t }}
      </p>
      <div class="mt-4 flex flex-wrap gap-2">
        <button class="px-3 py-1 bg-emerald-600 text-white rounded text-sm"
                data-testid="video-engine-apply"
                [disabled]="!canApply()"
                (click)="apply()">{{ 'Застосувати' | t }}</button>
        <button class="px-3 py-1 bg-slate-700 text-white rounded text-sm"
                data-testid="video-engine-return-native"
                [disabled]="!canEdit() || applying() || (cfg.effective === 'native' && cfg.selected === 'native')"
                (click)="returnNative()">{{ 'Повернути Vertep Native' | t }}</button>
      </div>
      <p *ngIf="lockedReason() as reason" class="mt-2 text-amber-700 text-sm" data-testid="video-engine-locked">{{ (reason) | t }}</p>
      <div *ngIf="applyError()" class="text-red-600 text-sm mt-2" data-testid="video-engine-error">{{ (applyError()) | t }}</div>
      <div *ngIf="rollbackNotice()" class="text-amber-700 text-sm mt-2" data-testid="video-engine-rollback">{{ (rollbackNotice()) | t }}</div>
    </div>
  </div>`,
})
export class VideoEngineSectionComponent implements OnInit, OnDestroy {
  config = signal<EffectiveEngineConfig | null>(null);
  loading = signal(false);
  error = signal<string | null>(null);
  applying = signal(false);
  applyError = signal<string | null>(null);
  rollbackNotice = signal<string | null>(null);
  choice = signal('native');
  endpoint = signal('');
  infoOpen = signal(false);
  /** local: the MoneyPrinterTurbo runtime shipped with Vertep; external: an operator runtime (#456). */
  runtimeMode = signal<'local' | 'external'>('local');
  deploying = signal(false);
  private deployPoll?: Subscription;

  private readonly explanations: Record<string, string> = {
    native: 'Vertep Native: відео збирає сам Vertep. Зовнішня програма не потрібна, а затвердження, версії та відновлення працюють повністю. Підходить для щоденної роботи та як запасний варіант.',
    'money-printer': 'MoneyPrinterTurbo: фінальне відео збирає окремий runtime. Локальний runtime Vertep розгортає й підключає сам; для зовнішнього потрібні адреса й токен. Якщо runtime недоступний, завдання завершиться помилкою, а не перейде на Vertep Native без вашого відома.',
  };

  readonly choiceExplanation = computed(() => this.explanations[this.choice()]
    ?? 'Зовнішній движок: відео збирає окрема програма. Перед застосуванням переконайтеся, що вона запущена й налаштована.');

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

  /** The runtime shipped with Vertep, offered only for the engine that has one. */
  readonly localRuntime = computed(() => {
    const local = this.config()?.local_runtime;
    return this.choice() === 'money-printer' && local?.supported ? local : null;
  });

  readonly usesLocalRuntime = computed(() => !!this.localRuntime() && this.runtimeMode() === 'local');

  /** A choice in the form that is not applied yet is named as such, never as the effective engine. */
  readonly draftLabel = computed(() => {
    const config = this.config();
    if (!config || this.choice() === config.effective) return null;
    return this.labelOf(this.choice());
  });

  /** What "ready" covers: the assembly engine, not every generator of the pipeline. */
  readonly scopeNote = computed(() => this.config()?.effective === 'native'
    ? 'Готовність стосується лише складання: Vertep Native збирає відео в CORE через FFmpeg і не потребує GPU. Сценарій, озвучка й зображення — окремі етапи; їх стан показано на вкладці «Ролі». Режим системи NORMAL не означає готовність усіх генераторів.'
    : 'Готовність стосується лише runtime складання; стан інших етапів конвеєра показано на вкладці «Ролі». Режим системи NORMAL не означає готовність усіх генераторів.');

  readonly deploymentPhase = computed(() => ({ QUEUED: 'очікує системного виконавця', APPLYING: 'контейнер завантажується й запускається' } as Record<string, string>)[this.config()?.local_runtime?.deployment_state || ''] || '');

  private labelOf(id?: string | null): string {
    const options = this.config()?.options ?? [];
    return options.find(option => option.id === id)?.label ?? id ?? '—';
  }

  readonly effectiveLabel = computed(() => this.config()?.worker_activation?.slot === 'video_engine'
    && this.config()?.worker_activation?.status === 'AWAITING_WORKER'
    ? 'Очікує підтвердження Worker' : this.labelOf(this.config()?.effective));

  readonly endpointField = computed<EngineField | null>(
    () => this.usesLocalRuntime() ? null : this.activeFields().find(field => field.name === 'endpoint') ?? null,
  );

  /** The local runtime needs no token: its wrapper holds the generated key itself. */
  readonly tokenField = computed<EngineField | null>(
    () => this.usesLocalRuntime() ? null : this.activeFields().find(field => field.name === 'token') ?? null,
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
    if (!config || !this.canEdit() || this.applying() || this.deploying()) return false;
    if (this.usesLocalRuntime()) {
      return !!this.localRuntime()?.available && (config.effective !== 'money-printer' || config.runtime_mode !== 'local');
    }
    return this.choice() !== config.effective || this.choice() !== config.selected || this.endpointChanged();
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
      ffmpeg_missing: 'у CORE немає FFmpeg для складання відео',
      unknown: 'невідома причина',
      worker_activation_pending: 'очікується підтвердження Worker',
    };
    return labels[reason] ?? reason;
  }

  private load(probe: boolean): void {
    const endpointSnapshot = this.endpoint();
    this.settingsApi.videoEngine(probe).subscribe({
      next: (config) => {
        this.config.set(config);
        this.error.set(null);
        if (!this.deploying()) this.choose(config.effective, config, endpointSnapshot);
      },
      error: (err) => this.error.set(err?.error?.detail || err?.message || 'Не вдалося завантажити конфігурацію движка'),
    });
  }

  /** Keep the form in sync with the API answer without overwriting the operator's input.

      `endpointSnapshot` is the address the form showed when the request was sent. When the
      address changed while the answer was in flight, the operator has typed something new
      and the answer must not silently replace it (Issue #122 P8). */
  private choose(effective: string, config: EffectiveEngineConfig, endpointSnapshot?: string): void {
    const operatorEdited = endpointSnapshot !== undefined && this.endpoint() !== endpointSnapshot;
    if (operatorEdited) {
      return;
    }
    this.choice.set(effective);
    this.runtimeMode.set(config.runtime_mode === 'external' ? 'external' : 'local');
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
    const local = this.usesLocalRuntime();
    this.settingsApi.switchVideoEngine(target, endpoint, undefined, local ? 'local' : undefined).subscribe({
      next: (result) => {
        this.applying.set(false);
        if (result.state === 'RUNTIME_DEPLOYING') {
          // The deployment executor starts the runtime first; the switch completes after it.
          this.watchDeployment();
          return;
        }
        const applied = result.effective_engine ?? null;
        if (result.worker_activation?.status === 'AWAITING_WORKER') {
          this.rollbackNotice.set('Конфігурацію збережено; активацію на Worker ще не підтверджено.');
        } else if (applied?.effective && applied.effective !== target) {
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
        this.rollbackNotice.set(!err?.status || err.status >= 500
          ? 'Відновлення ще не підтверджено; перевіряю стан у CORE.'
          : 'Попередній движок збережено.');
        this.load(false);
      },
    });
  }

  /** Follow the deployment of the local runtime and finish the switch once it runs. */
  private watchDeployment(): void {
    this.deploying.set(true);
    this.deployPoll?.unsubscribe();
    this.deployPoll = timer(3000, 3000).subscribe(() => {
      this.settingsApi.videoEngine(false).subscribe({
        next: (config) => {
          this.config.set(config);
          const local = config.local_runtime;
          if (!local || local.deploying) return;
          this.stopDeployment();
          if (local.deployment_state === 'FAILED' || !local.in_plan) {
            this.applyError.set(local.deployment_error || 'Розгортання runtime не вдалося');
            this.rollbackNotice.set('Попередній движок збережено.');
            return;
          }
          this.apply();
        },
        error: () => { /* transient; keep following */ },
      });
    });
  }

  private stopDeployment(): void {
    this.deployPoll?.unsubscribe();
    this.deployPoll = undefined;
    this.deploying.set(false);
  }

  ngOnDestroy(): void { this.deployPoll?.unsubscribe(); }

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
        // A deployment started before a reload is followed again, not forgotten.
        if (config.local_runtime?.deploying) {
          this.choice.set('money-printer');
          this.runtimeMode.set('local');
          this.watchDeployment();
        }
      },
      error: (err) => {
        this.error.set(err?.error?.detail || err?.message || 'Не вдалося завантажити конфігурацію движка');
        this.loading.set(false);
      },
    });
  }
}
