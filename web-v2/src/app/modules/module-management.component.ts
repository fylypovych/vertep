import { Component, OnDestroy, OnInit, computed, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Observable, Subscription, forkJoin, of, timer } from 'rxjs';
import { catchError } from 'rxjs/operators';
import { TPipe } from '../core/i18n/t.pipe';
import { PolicyService } from '../core/services/policy.service';
import { NavigationStateService } from '../core/services/navigation-state.service';
import { CatalogModule, CatalogNode, CoreRuntimeApiService, CoreRuntimeState, ModuleManagementApiService } from '../core/api/modules.api';

const STATE_TEXT: Record<string, string> = {
  NOT_INSTALLED: 'Не встановлено', INSTALLED: 'Встановлено', ACTIVE: 'Активний', INACTIVE: 'Вимкнено', FAILED: 'Збій',
  SYSTEM: 'Системний компонент',
};

const REASON_TEXT: Record<string, string> = {
  self_test_not_passed: 'Самоперевірка не пройдена', core_incompatible: 'Несумісна версія CORE',
  placement_required: 'Не вибрано вузол', not_placed_on_node: 'Не розміщено на цьому вузлі',
  permission_approval_required: 'Права не погоджено', lifecycle_installed: 'Ще не активовано',
  lifecycle_inactive: 'Вимкнено', lifecycle_failed: 'Збій модуля', bundled_not_managed: 'Працює як роль (не модуль)',
  interface_conflict: 'Конфлікт інтерфейсів', endpoint_missing: 'Немає адреси екземпляра', dependency_cycle: 'Циклічна залежність',
};

/**
 * One administrator choice: module and target node (#496). Worker, capabilities and
 * deployment roles stay internal; a node that cannot run a module shows why and
 * cannot be selected, and nothing is shown ready without a Worker report.
 */
@Component({
  selector: 'app-module-management',
  standalone: true,
  imports: [CommonModule, TPipe],
  template: `
  <div class="p-6 space-y-4" data-testid="module-management">
    <div>
      <h1 class="text-2xl font-semibold text-slate-900 dark:text-slate-100">{{ 'Модулі' | t }}</h1>
      <p class="text-sm text-slate-500">{{ 'Порядок для кожного модуля: встановити → розмістити на вузлі → погодити права → вузол запускає модуль → активувати. Вузол без потрібних ресурсів показує причину й не приймає модуль.' | t }}</p>
    </div>
    @if (error()) { <div class="rounded-lg border border-red-300 bg-red-50 p-3 text-sm text-red-800" role="alert" data-testid="module-error">{{ error() }}</div> }
    @if (core(); as runtime) {
      @if (deploymentText(runtime)) {
        <div class="rounded-lg border border-slate-200 dark:border-slate-700 p-3 text-sm space-y-1" data-testid="core-runtime">
          <p role="status" data-testid="deployment-state">{{ deploymentText(runtime) | t }}</p>
          @if (runtime.deployment?.message) { <p class="text-amber-700">{{ runtime.deployment!.message! | t }}</p> }
          @if (runtime.deployment?.error) { <p class="text-red-700" data-testid="deployment-error">{{ runtime.deployment!.error }}</p> }
        </div>
      }
    }
    @for (module of modules(); track module.module_id) {
      <section class="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-4 space-y-3" [attr.data-testid]="'module-' + module.module_id">
        <div class="flex flex-wrap items-center gap-2">
          <h2 class="text-lg font-semibold">{{ module.label | t }}</h2>
          <span class="text-xs text-slate-500">{{ module.module_id }} · {{ module.version }}</span>
          <span class="text-xs rounded px-2 py-0.5 bg-slate-100 dark:bg-slate-700" [attr.data-testid]="'module-state-' + module.module_id">{{ stateText(module.state) | t }}</span>
          @if (module.requires_gpu) { <span class="text-xs rounded px-2 py-0.5 bg-amber-100 text-amber-800">{{ 'Потрібен GPU' | t }}</span> }
          @if (module.target === 'isolated_lab') { <span class="text-xs rounded px-2 py-0.5 bg-sky-100 text-sky-800">{{ 'Лише окрема nonproduction VM' | t }}</span> }
        </div>
        @if (module.system) {
          <p class="text-sm" [attr.data-testid]="'system-health-' + module.module_id">
            {{ (module.health === 'RUNNING' ? 'Працює' : 'Недоступний') | t }} ·
            <span class="text-slate-500">{{ 'Розгортається й оновлюється разом із релізом Vertep' | t }}</span>
          </p>
        } @else {
        <table class="w-full text-sm">
          <thead><tr class="text-left text-slate-500"><th class="py-1">{{ 'Вузол' | t }}</th><th>{{ 'Розміщення' | t }}</th><th>{{ 'Готовність' | t }}</th></tr></thead>
          <tbody>
          @for (entry of module.nodes; track entry.node_id) {
            <tr class="border-t border-slate-100 dark:border-slate-700 align-top" [attr.data-testid]="'module-node-' + module.module_id + '-' + entry.node_id">
              <td class="py-1.5 pr-2">{{ entry.node_id }}</td>
              <td class="py-1.5 pr-2">
                @if (module.placement.includes(entry.node_id)) {
                  <span class="text-emerald-700" [attr.data-testid]="'placed-' + module.module_id + '-' + entry.node_id">{{ 'Розміщено' | t }}</span>
                  @if (isAdmin()) {
                    <button class="ml-2 text-xs underline text-slate-600 disabled:opacity-50" [disabled]="busy() || module.state === 'ACTIVE'"
                            (click)="place(module, entry.node_id, false)" [attr.data-testid]="'unplace-' + module.module_id + '-' + entry.node_id">{{ 'Прибрати' | t }}</button>
                  }
                } @else if (isAdmin()) {
                  <button class="px-2 py-1 rounded border text-xs disabled:opacity-50"
                          [disabled]="busy() || module.state === 'NOT_INSTALLED' || entry.compatible_reasons.length > 0"
                          (click)="place(module, entry.node_id, true)" [attr.data-testid]="'place-' + module.module_id + '-' + entry.node_id">{{ 'Розмістити тут' | t }}</button>
                }
                @for (reason of entry.compatible_reasons; track reason) {
                  <div class="text-xs text-amber-700" [attr.data-testid]="'incompatible-' + module.module_id + '-' + entry.node_id">{{ reason | t }}</div>
                }
                @if (isAdmin() && module.runtime_role && isCore(entry.node_id) && entry.runtime_missing) {
                  <button class="mt-1 text-xs text-emerald-700 underline disabled:opacity-50" [disabled]="busy() || runtimePending() || !core()"
                          (click)="prepareCore(module.runtime_role)" [attr.data-testid]="'prepare-core-' + module.module_id">{{ 'Підготувати середовище на центральному вузлі' | t }}</button>
                }
              </td>
              <td class="py-1.5" [attr.data-testid]="'ready-' + module.module_id + '-' + entry.node_id">
                @if (entry.instance; as instance) {
                  {{ (instance.state === 'READY' ? 'Готовий' : 'Не готовий') | t }}
                  @for (reason of instance.reasons; track reason) { <div class="text-xs text-slate-500">{{ reasonText(reason) | t }}</div> }
                } @else { <span class="text-slate-400">{{ 'Вузол не повідомляв модуль' | t }}</span> }
              </td>
            </tr>
          }
          </tbody>
        </table>
        }
        @if (moduleError()?.module === module.module_id) {
          <div class="rounded-lg border border-red-300 bg-red-50 p-2 text-sm text-red-800" role="alert" [attr.data-testid]="'module-action-error-' + module.module_id">{{ moduleError()!.message }}</div>
        }
        @if (isAdmin() && !module.system) {
          @if (progress(module); as step) {
            <div class="space-y-1" [attr.data-testid]="'progress-' + module.module_id">
              <div class="flex justify-between text-xs">
                <span [class]="step.waiting ? 'text-amber-700' : 'text-slate-500'">{{ step.label | t }}</span>
                <span class="text-slate-500">{{ step.done }}/5 · {{ step.percent }}%</span>
              </div>
              <div class="h-2 rounded-full bg-slate-200 dark:bg-slate-700 overflow-hidden" role="progressbar"
                   aria-valuemin="0" aria-valuemax="100" [attr.aria-valuenow]="step.percent" [attr.aria-label]="module.label | t">
                <div class="h-full rounded-full transition-all" [style.width.%]="step.percent"
                     [ngClass]="step.waiting ? 'bg-amber-500 animate-pulse' : 'bg-emerald-500'"></div>
              </div>
            </div>
          }
          <ol class="space-y-2 text-sm" [attr.data-testid]="'steps-' + module.module_id">
            <li class="flex flex-wrap items-center gap-2">
              <span class="w-5 text-slate-500">1.</span><span class="w-40">{{ 'Встановлення' | t }}</span>
              @if (module.state === 'NOT_INSTALLED') {
                @if (module.bundled) {
                  <button class="px-3 py-1.5 rounded-lg bg-emerald-600 text-white disabled:opacity-50" [disabled]="busy()" (click)="run('install', { bundled: module.module_id })" [attr.data-testid]="'install-' + module.module_id">{{ 'Встановити' | t }}</button>
                } @else { <span class="text-slate-500">{{ 'Встановлюється з пакета в «Оновленні»' | t }}</span> }
              } @else { <span class="text-emerald-700">✓ {{ 'Встановлено' | t }}</span> }
            </li>
            <li class="flex flex-wrap items-center gap-2">
              <span class="w-5 text-slate-500">2.</span><span class="w-40">{{ 'Вузол' | t }}</span>
              @if (module.placement.length) { <span class="text-emerald-700">✓ {{ module.placement.join(', ') }}</span> }
              @else { <span class="text-slate-500">{{ 'Натисніть «Розмістити тут» біля вузла в таблиці вище' | t }}</span> }
            </li>
            <li class="flex flex-wrap items-center gap-2">
              <span class="w-5 text-slate-500">3.</span><span class="w-40">{{ 'Права' | t }}</span>
              @if (!module.permissions.length) { <span class="text-emerald-700">✓ {{ 'Не потрібні' | t }}</span> }
              @else if (approved(module)) { <span class="text-emerald-700">✓ {{ module.permissions.join(', ') }}</span> }
              @else {
                <button class="px-3 py-1.5 rounded-lg border disabled:opacity-50" [disabled]="busy() || module.state === 'NOT_INSTALLED'" (click)="approve(module)" [attr.data-testid]="'approve-' + module.module_id">{{ 'Погодити права' | t }}: {{ module.permissions.join(', ') }}</button>
              }
            </li>
            <li class="flex flex-wrap items-center gap-2">
              <span class="w-5 text-slate-500">4.</span><span class="w-40">{{ 'Запуск на вузлі' | t }}</span>
              @if (reported(module)) { <span class="text-emerald-700">✓ {{ 'Вузол повідомив модуль' | t }}</span> }
              @else if (module.placement.length && approved(module)) { <span class="text-slate-500" [attr.data-testid]="'waiting-node-' + module.module_id">{{ 'Очікуємо, поки вузол запустить модуль і повідомить про нього' | t }}</span> }
              @else { <span class="text-slate-400">—</span> }
            </li>
            <li class="flex flex-wrap items-center gap-2">
              <span class="w-5 text-slate-500">5.</span><span class="w-40">{{ 'Активація' | t }}</span>
              @if (module.state === 'ACTIVE') {
                <span class="text-emerald-700">✓ {{ 'Активний' | t }}</span>
              } @else {
                <button class="px-3 py-1.5 rounded-lg bg-emerald-600 text-white disabled:opacity-50" [disabled]="busy() || !!activationBlocker(module)" (click)="run('activate', { module_id: module.module_id })" [attr.data-testid]="'activate-' + module.module_id">{{ 'Активувати' | t }}</button>
                @if (activationBlocker(module); as blocker) { <span class="text-xs text-slate-500" [attr.data-testid]="'activate-blocker-' + module.module_id">{{ blocker | t }}</span> }
              }
            </li>
          </ol>
          @if (module.state !== 'NOT_INSTALLED') {
            <div class="flex flex-wrap gap-2 text-sm border-t border-slate-100 dark:border-slate-700 pt-3">
              @if (module.state === 'ACTIVE') {
                <button class="px-3 py-1.5 rounded-lg border disabled:opacity-50" [disabled]="busy()" (click)="run('deactivate', { module_id: module.module_id })" [attr.data-testid]="'deactivate-' + module.module_id">{{ 'Вимкнути' | t }}</button>
              }
              <button class="px-3 py-1.5 rounded-lg border border-red-300 text-red-700 disabled:opacity-50" [disabled]="busy()" (click)="uninstall(module)" [attr.data-testid]="'uninstall-' + module.module_id">{{ 'Видалити (з архівом)' | t }}</button>
            </div>
          }
        }
      </section>
    }
  </div>`,
})
export class ModuleManagementComponent implements OnInit, OnDestroy {
  modules = signal<CatalogModule[]>([]);
  nodes = signal<CatalogNode[]>([]);
  core = signal<CoreRuntimeState | null>(null);
  error = signal<string | null>(null);
  busy = signal(false);
  isAdmin = computed(() => this.policy.userRole() === 'admin');

  constructor(
    private api: ModuleManagementApiService,
    private runtime: CoreRuntimeApiService,
    private policy: PolicyService,
    private navState: NavigationStateService,
  ) {}

  private poll?: Subscription;
  ngOnInit(): void { this.poll = timer(0, 3000).subscribe(() => this.refreshRuntime()); this.load(); }
  ngOnDestroy(): void { this.poll?.unsubscribe(); }
  private refreshRuntime(): void {
    this.runtime.state().pipe(catchError(() => of(null))).subscribe(core => this.core.set(core));
    // While a node is starting a module, keep its bar moving without a manual reload.
    if (!this.busy() && this.modules().some(module => !module.system && this.progress(module).waiting)) {
      this.api.catalog().pipe(catchError(() => of(null))).subscribe(catalog => {
        if (catalog) { this.modules.set(catalog.modules); this.nodes.set(catalog.nodes); }
      });
    }
  }

  load(): void {
    forkJoin({ catalog: this.api.catalog(), core: this.runtime.state().pipe(catchError(() => of(null))) }).subscribe({
      next: ({ catalog, core }) => {
        this.modules.set(catalog.modules);
        this.nodes.set(catalog.nodes);
        this.core.set(core);
      },
      error: (error: Error) => this.error.set(error.message),
    });
  }

  stateText(state: string): string { return STATE_TEXT[state] || state; }
  reasonText(reason: string): string {
    if (reason.startsWith('dependency_unmet:')) return 'Залежність не виконана';
    if (reason.startsWith('interface_unmet:')) return 'Потрібний інтерфейс недоступний';
    return REASON_TEXT[reason] || reason;
  }
  isCore(nodeId: string): boolean { return this.nodes().some(node => node.node_id === nodeId && node.role === 'core'); }
  approved(module: CatalogModule): boolean { return module.permissions.every(item => module.approved_permissions.includes(item)); }
  runtimePending(): boolean {
    const runtime = this.core();
    return !!runtime?.queued || ['QUEUED', 'APPLYING'].includes(runtime?.deployment?.state || '');
  }
  /** Same deployment states the system executor reports (formerly the roles tab). */
  deploymentText(runtime: CoreRuntimeState): string {
    const deployment = runtime.deployment || {};
    if (deployment.stalled) return 'Потрібна перевірка системного виконавця';
    if (deployment.state === 'FAILED') return 'Застосування не вдалося';
    if (deployment.state === 'SUCCEEDED') return 'Локальне розгортання застосовано; готовність показано окремо';
    return ({ QUEUED: 'Очікує системного виконавця', PREPARING: 'Підготовка розгортання', PULLING: 'Завантаження служб',
      STARTING: 'Запуск служб', VERIFYING: 'Перевірка готовності служб', STOPPING: 'Вимкнення зайвих служб' } as Record<string, string>)[deployment.phase || '']
      || (runtime.queued ? 'Очікує системного виконавця' : '');
  }

  /** One click places (or removes) the module on a node; it is saved at once. */
  place(module: CatalogModule, nodeId: string, on: boolean): void {
    const current = module.placement.filter(item => item !== nodeId);
    this.act(this.api.placement(module.module_id, on ? [...current, nodeId] : current), module.module_id);
  }
  /** A placed node has reported the module (step 4): the node runs it only once it is placed and its
   *  permissions are approved; activation runs the self-test against that report. */
  reported(module: CatalogModule): boolean {
    return module.nodes.some(entry => module.placement.includes(entry.node_id) && !!entry.instance);
  }
  /** Why «Активувати» is not available yet: the first unfinished step. */
  activationBlocker(module: CatalogModule): string | null {
    if (module.state === 'NOT_INSTALLED') return 'Спершу встановіть модуль';
    if (!module.placement.length) return 'Спершу розмістіть модуль на вузлі';
    if (module.permissions.length && !this.approved(module)) return 'Спершу погодьте права';
    if (!this.reported(module)) return 'Чекаємо на запуск модуля на вузлі';
    return null;
  }

  /** Five steps of the card as one bar: completed steps × 20%; amber while the node is starting the module. */
  progress(module: CatalogModule): { done: number; percent: number; label: string; waiting: boolean } {
    const steps = [module.state !== 'NOT_INSTALLED', module.placement.length > 0, this.approved(module),
      this.reported(module), module.state === 'ACTIVE'];
    const done = steps.findIndex(ok => !ok);
    const count = done < 0 ? 5 : done;
    const waiting = count === 3;
    const label = count === 5 ? 'Модуль активний'
      : waiting ? (this.runtimePending() && this.core() ? this.deploymentText(this.core()!) : '') || 'Вузол запускає модуль'
      : count === 4 ? 'Готовий до активації' : this.activationBlocker(module) || '';
    return { done: count, percent: count * 20, label, waiting };
  }

  /** The answer appears in the card the administrator acted on, not above the list (out of view). */
  moduleError = signal<{ module: string; message: string } | null>(null);

  private act(request: Observable<unknown>, moduleId?: string): void {
    this.busy.set(true);
    this.error.set(null);
    this.moduleError.set(null);
    request.subscribe({
      next: () => {
        this.busy.set(false);
        this.load();
        this.navState.refreshModules().subscribe();
        this.navState.refreshBackup();
      },
      error: (error: Error) => {
        this.busy.set(false);
        if (moduleId) this.moduleError.set({ module: moduleId, message: error.message });
        else this.error.set(error.message);
        this.load();
      },
    });
  }

  run(action: string, body: Record<string, unknown>): void {
    this.act(this.api.lifecycle(action, body), String(body['module_id'] ?? body['bundled'] ?? '') || undefined);
  }
  approve(module: CatalogModule): void { this.act(this.api.approve(module.module_id, module.permissions), module.module_id); }
  prepareCore(role: string): void {
    const runtime = this.core();
    if (!runtime) return;
    const active = runtime.active_roles || [];
    this.act(this.runtime.prepare([...new Set([...active, role])]));
  }
  uninstall(module: CatalogModule): void {
    if (window.confirm(`${module.label}: видалити модуль? Перед видаленням буде створено й перевірено архів даних.`)) {
      this.run('uninstall', { module_id: module.module_id, confirm: true });
    }
  }
}
