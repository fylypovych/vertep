import { Component, OnDestroy, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { HttpClient } from '@angular/common/http';
import { catchError } from 'rxjs/operators';
import { TPipe } from '../../core/i18n/t.pipe';
import { BaseApiService } from '../../core/api/base-api.service';
import { ToastService } from '../../core/services/toast.service';

interface Recommendation {
  module_id: string; current: string; available: string | null; recommended: boolean; risk: string | null;
  profile?: string; current_profile?: string; urgent?: boolean; reasons: string[]; warnings?: string[];
}
interface PlanNode { node: string; status: string; }
interface PlanStep { kind: string; component: string; from: string; to: string; status: string; nodes?: PlanNode[]; stage?: number; }
interface Plan { id: string; status: string; steps: PlanStep[]; stages: string[][]; approved_by: string | null; }
interface RollbackPoint { operation: string; version: string; created_at: number; compatible: boolean; package_available: boolean; database: boolean; }
interface LogEntry { timestamp?: string; message?: string; operation_id?: string; component?: string; }

/**
 * #515 Updates panel for modules: recommendations, approval, execution through the Update
 * Manager, progress, suitable rollback points and the linked UPDATE journal entries.
 * Every action is a call to the Manager API; nothing is decided in the browser.
 */
@Component({
  selector: 'app-module-updates',
  standalone: true,
  imports: [CommonModule, TPipe],
  template: `
    <div class="mt-6 border-t border-slate-200 pt-4" data-testid="module-updates">
      <h4 class="text-base font-semibold text-slate-900 mb-3">{{ 'Оновлення модулів' | t }}</h4>
      @if (error()) { <div class="text-sm text-red-600 mb-2" data-testid="module-updates-error">{{ error() }}</div> }
      <table class="w-full text-sm mb-4">
        <thead><tr class="text-left text-slate-500">
          <th>{{ 'Модуль' | t }}</th><th>{{ 'Встановлено' | t }}</th><th>{{ 'Доступно' | t }}</th><th>{{ 'Ризик' | t }}</th><th></th>
        </tr></thead>
        <tbody>
          @for (item of recommendations(); track item.module_id) {
            <tr class="border-t border-slate-100" [attr.data-testid]="'update-recommendation-' + item.module_id">
              <td class="py-2 font-medium">{{ item.module_id }}</td>
              <td>{{ item.current }}</td>
              <td>{{ item.available || '—' }}</td>
              <td [attr.data-testid]="'update-risk-' + item.module_id">{{ (item.risk || '—') | t }}@if (item.urgent) { · {{ 'безпека' | t }} }</td>
              <td class="text-right">
                @if (item.available && item.recommended) {
                  <button class="px-3 py-1 text-xs bg-emerald-600 text-white rounded-lg" (click)="plan(item)"
                          [attr.data-testid]="'update-plan-' + item.module_id">{{ 'Запланувати' | t }}</button>
                }
              </td>
            </tr>
            @if (item.reasons.length || item.warnings?.length) {
              <tr><td colspan="5" class="text-xs text-slate-500 pb-2">
                @for (reason of item.reasons; track reason) { <div>{{ reason }}</div> }
                @for (warning of item.warnings || []; track warning) { <div class="text-amber-700">{{ warning }}</div> }
              </td></tr>
            }
          } @empty {
            <tr><td colspan="5" class="py-2 text-slate-500">{{ 'Немає модулів із закріпленим джерелом' | t }}</td></tr>
          }
        </tbody>
      </table>

      @for (plan of plans(); track plan.id) {
        <div class="rounded-lg border border-slate-200 p-3 mb-3" [attr.data-testid]="'update-plan-card-' + plan.id">
          <div class="flex justify-between items-center mb-2">
            <span class="text-sm font-medium">{{ 'План' | t }} {{ plan.id.slice(0, 8) }}</span>
            <span class="text-xs" data-testid="update-plan-status">{{ plan.status | t }}</span>
          </div>
          @for (step of plan.steps; track step.component) {
            <div class="text-xs py-1" [attr.data-testid]="'update-step-' + step.component">
              <b>{{ step.component }}</b> {{ step.from }} → {{ step.to }} · {{ step.status | t }}
              @for (node of step.nodes || []; track node.node) {
                <span class="ml-2" [attr.data-testid]="'update-node-' + step.component + '-' + node.node">{{ node.node }}: {{ node.status | t }}</span>
              }
            </div>
          }
          <div class="mt-2 flex gap-2">
            @if (plan.status === 'PENDING_APPROVAL') {
              <button class="px-3 py-1 text-xs border rounded-lg" (click)="approve(plan)" data-testid="update-approve">{{ 'Погодити' | t }}</button>
            }
            @if (plan.status === 'APPROVED' || plan.status === 'WAITING_CORE') {
              <button class="px-3 py-1 text-xs bg-emerald-600 text-white rounded-lg" (click)="execute(plan)" data-testid="update-execute">{{ 'Запустити' | t }}</button>
            }
            <button class="px-3 py-1 text-xs border rounded-lg" (click)="showLog(plan)" data-testid="update-show-log">{{ 'Журнал UPDATE' | t }}</button>
          </div>
          @if (logPlan() === plan.id) {
            <div class="mt-2 text-xs text-slate-600" data-testid="update-log">
              @for (entry of log(); track $index) { <div>{{ entry.timestamp }} · {{ entry.message }}@if (entry.component) { · {{ entry.component }} }</div> }
            </div>
          }
        </div>
      }

      <div class="mt-4">
        <h5 class="text-sm font-semibold mb-2">{{ 'Точки rollback' | t }}</h5>
        <div class="flex gap-2 mb-2">
          @for (item of recommendations(); track item.module_id) {
            <button class="px-2 py-1 text-xs border rounded" (click)="loadPoints(item.module_id)" [attr.data-testid]="'rollback-points-' + item.module_id">{{ item.module_id }}</button>
          }
        </div>
        @for (point of points(); track point.operation) {
          <div class="text-xs flex items-center gap-2 py-1" [attr.data-testid]="'rollback-point-' + point.version">
            <span>{{ point.version }} · {{ point.created_at * 1000 | date:'yyyy-MM-dd HH:mm' }}</span>
            @if (point.compatible && point.package_available) {
              <label class="inline-flex items-center gap-1"><input type="checkbox" [checked]="acceptLoss()" (change)="acceptLoss.set($any($event.target).checked)" data-testid="rollback-accept-loss">
                {{ 'Погоджуюся з втратою пізніших записів БД' | t }}</label>
              <button class="px-2 py-1 border rounded" (click)="rollback(point)" data-testid="rollback-to-point">{{ 'Повернути' | t }}</button>
            } @else { <span class="text-slate-500">{{ 'Непридатна точка' | t }}</span> }
          </div>
        }
      </div>
    </div>
  `,
})
export class ModuleUpdatesComponent implements OnInit, OnDestroy {
  recommendations = signal<Recommendation[]>([]);
  plans = signal<Plan[]>([]);
  points = signal<RollbackPoint[]>([]);
  pointsFor = signal<string | null>(null);
  log = signal<LogEntry[]>([]);
  logPlan = signal<string | null>(null);
  acceptLoss = signal(false);
  error = signal<string | null>(null);
  private timer?: ReturnType<typeof setInterval>;

  constructor(private http: HttpClient, private base: BaseApiService, private toast: ToastService) {}

  private get api(): string { return `${this.base.url}/nodes/module-registry`; }
  private fail = (error: { message?: string; error?: { detail?: string } }) => {
    this.error.set(error?.error?.detail || error?.message || 'Помилка');
    return [];
  };

  ngOnInit(): void {
    this.refresh();
    // Progress of running plans comes from the Manager.
    this.timer = setInterval(() => this.loadPlans(), 2000);
  }

  ngOnDestroy(): void { if (this.timer) clearInterval(this.timer); }

  refresh(): void {
    this.http.get<{ recommendations: Recommendation[] }>(`${this.api}/updates/recommendations`, { headers: this.base.headers() })
      .pipe(catchError(this.fail)).subscribe((value) => this.recommendations.set((value as { recommendations: Recommendation[] }).recommendations || []));
    this.loadPlans();
  }

  loadPlans(): void {
    this.http.get<{ plans: Plan[] }>(`${this.api}/updates`, { headers: this.base.headers() })
      .pipe(catchError(() => [])).subscribe((value) => {
        const plans = (value as { plans: Plan[] }).plans;
        if (plans) this.plans.set([...plans].reverse());
      });
    if (this.logPlan()) this.loadLog(this.logPlan()!);
  }

  plan(item: Recommendation): void {
    this.error.set(null);
    this.http.post<Plan>(`${this.api}/updates/plan`, { targets: { [item.module_id]: item.available } }, { headers: this.base.headers() })
      .pipe(catchError(this.fail)).subscribe(() => this.loadPlans());
  }

  approve(plan: Plan): void {
    this.http.post<Plan>(`${this.api}/updates/${plan.id}/approve`, {}, { headers: this.base.headers() })
      .pipe(catchError(this.fail)).subscribe(() => this.loadPlans());
  }

  execute(plan: Plan): void {
    this.http.post<Plan>(`${this.api}/updates/${plan.id}/execute`, {}, { headers: this.base.headers() })
      .pipe(catchError(this.fail)).subscribe(() => this.loadPlans());
  }

  showLog(plan: Plan): void {
    this.logPlan.set(plan.id);
    this.loadLog(plan.id);
  }

  private loadLog(planId: string): void {
    this.http.get<LogEntry[]>(`${this.base.url}/logs?category=UPDATE&operation_id=${encodeURIComponent(planId)}&limit=200`,
      { headers: this.base.headers() }).pipe(catchError(() => [])).subscribe((entries) => this.log.set((entries as LogEntry[]) || []));
  }

  loadPoints(moduleId: string): void {
    this.pointsFor.set(moduleId);
    this.http.get<{ points: RollbackPoint[] }>(`${this.api}/rollback-points/${encodeURIComponent(moduleId)}`, { headers: this.base.headers() })
      .pipe(catchError(this.fail)).subscribe((value) => this.points.set((value as { points: RollbackPoint[] }).points || []));
  }

  rollback(point: RollbackPoint): void {
    const moduleId = this.pointsFor();
    if (!moduleId) return;
    this.http.post(`${this.api}/lifecycle/rollback_point`,
      { module_id: moduleId, operation: point.operation, accept_data_loss: this.acceptLoss() }, { headers: this.base.headers() })
      .pipe(catchError(this.fail)).subscribe((result) => {
        if (result && !Array.isArray(result)) { this.toast.show('Модуль повернуто до вибраної точки', 'success'); this.refresh(); this.loadPoints(moduleId); }
      });
  }
}
