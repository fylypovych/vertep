import { Component, OnInit, OnDestroy, signal, computed, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterModule } from '@angular/router';
import { ActivatedRoute, Router } from '@angular/router';
import { timer, Subscription } from 'rxjs';
import { VertepApiService } from '../core/api.service';
import { ToastService } from '../core/services/toast.service';
import { NodeDetail, NodeActionPayload, SelfTestResult, SystemRole, SystemRolesResponse } from '../core/models';
import { roleLabel, statusLabel as localizedStatus } from '../core/presentation';
import { VertepDatePipe } from '../shared/vertep-date.pipe';

@Component({
  selector: 'app-worker-detail',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterModule, VertepDatePipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="space-y-5" data-testid="worker-detail-page">
      <div class="flex items-center gap-4">
        <a routerLink="/workers" class="text-emerald-600 hover:text-emerald-700 text-sm font-medium">&larr; Назад до воркерів</a>
        @if (node()) {
          <h2 class="text-xl font-semibold text-slate-900">{{ node()!.node_name }}</h2>
        }
      </div>

      @if (loading()) {
        <div class="animate-pulse space-y-4">
          <div class="h-6 bg-slate-200 rounded w-3/4"></div>
          <div class="h-4 bg-slate-200 rounded w-1/2"></div>
          <div class="h-64 bg-slate-200 rounded"></div>
        </div>
      } @else if (error()) {
        <div class="bg-red-50 border border-red-200 rounded-xl p-4">
          <p class="text-red-700">{{ error() }}</p>
        </div>
      } @else if (node()) {
        <div class="grid grid-cols-1 lg:grid-cols-3 gap-5">
          <div class="lg:col-span-2 space-y-5">
            <div class="bg-white rounded-xl border border-slate-200 p-5">
              <h3 class="text-sm font-medium text-slate-500 mb-3">Інформація про вузол</h3>
              <dl class="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
                <div><dt class="text-slate-500">ID</dt><dd class="font-mono text-xs">{{ node()!.node_id }}</dd></div>
                <div><dt class="text-slate-500">Роль</dt><dd>{{ roleName(node()!.role) }}</dd></div>
                <div><dt class="text-slate-500">Статус</dt><dd>{{ nodeStatus(node()!.status) }}</dd></div>
                <div><dt class="text-slate-500">Runtime версія</dt><dd>{{ node()!.runtime_version || node()!.version || '—' }}</dd></div>
                <div><dt class="text-slate-500">GPU</dt><dd>{{ node()!.gpu_name || '—' }}</dd></div>
                <div><dt class="text-slate-500">VRAM</dt><dd>{{ node()!.vram_mb ? (node()!.vram_mb + ' MB') : '—' }}@if (node()!.free_vram_mb) { ({{ node()!.free_vram_mb }} MB вільно) }</div>
                <div><dt class="text-slate-500">CPU навантаження</dt><dd>{{ node()!.cpu_load != null ? (node()!.cpu_load + '%') : '—' }}</dd></div>
                <div><dt class="text-slate-500">GPU навантаження</dt><dd>{{ node()!.gpu_load != null ? (node()!.gpu_load + '%') : '—' }}</dd></div>
                <div><dt class="text-slate-500">Температура</dt><dd>{{ node()!.temperature != null ? (node()!.temperature + '°C') : '—' }}</dd></div>
                <div><dt class="text-slate-500">RAM</dt><dd>{{ node()!.ram_mb ? (node()!.ram_mb + ' MB') : '—' }}</dd></div>
                <div><dt class="text-slate-500">Диск вільно</dt><dd>{{ node()!.disk_free_mb ? (node()!.disk_free_mb + ' MB') : '—' }}</dd></div>
                <div><dt class="text-slate-500">Поточна задача</dt><dd>{{ node()!.current_task || '—' }}</dd></div>
                <div><dt class="text-slate-500">Поточний job</dt><dd>{{ node()!.current_job || '—' }}</dd></div>
              </dl>
            </div>

            <div class="bg-white rounded-xl border border-slate-200 p-5">
              <h3 class="text-sm font-medium text-slate-500 mb-3">Можливості та сертифікати</h3>
              <dl class="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
                <div><dt class="text-slate-500">Можливості</dt><dd class="text-xs">{{ (node()!.capabilities || []).length ? node()!.capabilities.join(', ') : '—' }}</dd></div>
                <div><dt class="text-slate-500">Перевірені можливості</dt><dd class="text-xs">{{ (node()!.tested_capabilities || []).length ? (node()!.tested_capabilities || []).join(', ') : '—' }}</dd></div>
                <div><dt class="text-slate-500">Підтримувані workflow</dt><dd class="text-xs">{{ (node()!.supported_workflows || []).length ? (node()!.supported_workflows || []).join(', ') : '—' }}</dd></div>
                <div><dt class="text-slate-500">Модулі</dt><dd class="text-xs">{{ (node()!.modules || []).length ? node()!.modules!.join(', ') : '—' }}</dd></div>
                <div><dt class="text-slate-500">Сервіси</dt><dd class="text-xs">{{ (node()!.services || []).length ? node()!.services!.join(', ') : '—' }}</dd></div>
                <div><dt class="text-slate-500">Серійний номер сертифіката</dt><dd class="font-mono text-xs">{{ node()!.certificate_serial || '—' }}</dd></div>
                <div><dt class="text-slate-500">Сертифікат діє до</dt><dd>{{ node()!.certificate_expires_at | vertepDate }}</dd></div>
                <div><dt class="text-slate-500">Покоління credentials</dt><dd>{{ node()!.credential_generation || '—' }}</dd></div>
                <div><dt class="text-slate-500">Зареєстровано</dt><dd>{{ node()!.registered_at | vertepDate }}</dd></div>
                <div><dt class="text-slate-500">Відкликано</dt><dd>{{ node()!.revoked_at || 'Ні' }}</dd></div>
              </dl>
            </div>

            <div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="node-backends">
              <h3 class="text-sm font-medium text-slate-500 mb-3">Движки можливостей</h3>
              @for (entry of capabilityBackends(); track entry.capability) {
                <div class="flex justify-between gap-4 py-1 text-sm"><span>{{ entry.capability }}</span><span class="font-medium">{{ entry.backend }}</span></div>
              } @empty { <p class="text-sm text-slate-500">Немає даних</p> }
            </div>

            @if (node()!.role === 'core') {
              <div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="node-roles">
                <h3 class="text-sm font-medium text-slate-500 mb-3">Ролі цього вузла</h3>
                <div class="grid grid-cols-1 sm:grid-cols-2 gap-2">
                  @for (role of availableRoles(); track role.id) {
                    <label class="flex items-start gap-2 rounded border border-slate-200 p-3 text-sm">
                      <input type="checkbox" [checked]="selectedRoles().includes(role.id)" (change)="toggleRole(role.id, $event)">
                      <span><strong>{{ roleName(role.id) }}</strong><small class="block text-slate-500">{{ (role.capabilities || []).join(', ') }}</small></span>
                    </label>
                  }
                </div>
                <button (click)="saveRoles()" [disabled]="rolesSaving()" class="mt-3 px-3 py-2 bg-emerald-600 text-white rounded-lg text-sm disabled:opacity-50" data-testid="node-roles-save">
                  {{ rolesSaving() ? 'Застосування…' : 'Застосувати ролі' }}
                </button>
              </div>
            }

            <div class="bg-white rounded-xl border border-slate-200 p-5">
              <h3 class="text-sm font-medium text-slate-500 mb-3">Самодіагностика (self-test)</h3>
              @if (selfTestStatus()) {
                <pre class="text-xs text-slate-700 bg-slate-50 p-3 rounded overflow-x-auto">{{ selfTestStatus() }}</pre>
              } @else {
                <p class="text-sm text-slate-500">Немає даних про самодіагностику</p>
              }
            </div>

            <div class="bg-white rounded-xl border border-slate-200 p-5">
              <h3 class="text-sm font-medium text-slate-500 mb-3">Дії з вузлом</h3>
              <div class="grid grid-cols-2 sm:grid-cols-3 gap-2">
                @for (action of availableActions(); track action.value) {
                  <button (click)="runAction({action: action.value})"
                          [disabled]="actioning()"
                          class="px-3 py-2 text-sm border border-slate-200 rounded-lg hover:bg-slate-50 disabled:opacity-50">
                    {{ action.label }}
                  </button>
                }
              </div>
            </div>
          </div>

          <div class="space-y-5">
            <div class="bg-white rounded-xl border border-slate-200 p-5">
              <h3 class="text-sm font-medium text-slate-500 mb-3">Статус онлайну</h3>
              <div class="flex items-center gap-2 mb-2">
                <span class="w-2.5 h-2.5 rounded-full"
                      [class.bg-emerald-500]="isOnline()"
                      [class.bg-slate-400]="!isOnline()"></span>
                <span class="text-sm font-medium">{{ statusLabel() }}</span>
              </div>
              @if (heartbeatAge()) {
                <p class="text-xs text-slate-500">Останній heartbeat: {{ heartbeatAge() }} тому</p>
              }
              <button (click)="runAction({action: 'self-test'})"
                      [disabled]="actioning() || isBusy()"
                      data-testid="self-test-button"
                      class="mt-3 w-full px-3 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50">
                    Самодіагностика
              </button>
            </div>

            <div class="bg-white rounded-xl border border-slate-200 p-5">
              <h3 class="text-sm font-medium text-slate-500 mb-3">Стан оновлення</h3>
              <dl class="text-sm space-y-1">
                <div><dt class="text-slate-500">Бажаний стан</dt><dd>{{ node()!.update_state.desired_state ? nodeStatus(node()!.update_state.desired_state!) : 'Немає' }}</dd></div>
                <div><dt class="text-slate-500">Цільова версія</dt><dd>{{ node()!.update_state.update_target_version || '—' }}</dd></div>
                <div><dt class="text-slate-500">Rollback версія</dt><dd>{{ node()!.update_state.rollback_target_version || '—' }}</dd></div>
              </dl>
            </div>

            <div class="bg-white rounded-xl border border-slate-200 p-5">
              <h3 class="text-sm font-medium text-slate-500 mb-3">Видалення вузла</h3>
              <p class="text-xs text-slate-500 mb-3">Відклик сертифіката та видалення з реєстру.</p>
              <button (click)="revoke()"
                      class="w-full px-3 py-2 text-sm bg-red-600 text-white rounded-lg hover:bg-red-700">
                    Відкликати
              </button>
            </div>
          </div>
        </div>
      }
    </div>
  `,
})
export class WorkerDetailComponent implements OnInit, OnDestroy {
  node = signal<NodeDetail | null>(null);
  loading = signal(true);
  error = signal<string | null>(null);
  actioning = signal(false);
  availableRoles = signal<SystemRole[]>([]);
  selectedRoles = signal<string[]>([]);
  rolesSaving = signal(false);
  private pollSub: Subscription | null = null;

  readonly allActions: { value: NodeActionPayload['action']; label: string }[] = [
    { value: 'drain', label: 'Завершити поточну роботу' },
    { value: 'resume', label: 'Відновити роботу' },
    { value: 'quarantine', label: 'Ізолювати' },
    { value: 'unquarantine', label: 'Зняти ізоляцію' },
    { value: 'self-test', label: 'Самодіагностика' },
    { value: 'disable', label: 'Вимкнути' },
    { value: 'enable', label: 'Увімкнути' },
    { value: 'restart', label: 'Перезапустити' },
    { value: 'logs', label: 'Переглянути логи' },
    { value: 'update', label: 'Оновити' },
  ];

  constructor(
    private route: ActivatedRoute,
    private router: Router,
    private api: VertepApiService,
    private toast: ToastService,
  ) {}

  ngOnInit(): void {
    this.loadNode();
    this.pollSub = timer(0, 10000).subscribe(() => this.loadNode());
  }

  ngOnDestroy(): void {
    this.pollSub?.unsubscribe();
  }

  nodeOutFunc = this.node;

  isOnline = computed(() => {
    const n = this.node();
    return !!n && n.status !== 'OFFLINE';
  });

  statusLabel = computed(() => {
    const n = this.node();
    if (!n) return 'Не визначено';
    return localizedStatus(n.status);
  });

  roleName(role: string): string { return roleLabel(role); }
  nodeStatus(status: string): string { return localizedStatus(status); }
  capabilityBackends(): Array<{ capability: string; backend: string }> {
    return Object.entries(this.node()?.capability_backends || {}).map(([capability, backend]) => ({ capability, backend }));
  }

  heartbeatAge = computed(() => {
    const n = this.node();
    if (!n || !n.runtime) return null;
    const lastSeen = n.runtime.last_seen;
    if (!lastSeen) return null;
    const last = new Date(lastSeen);
    const now = new Date();
    const diffSec = Math.floor((now.getTime() - last.getTime()) / 1000);
    if (diffSec < 60) return diffSec + 'c';
    const diffMin = Math.floor(diffSec / 60);
    return diffMin + 'хв';
  });

  selfTestStatus = computed(() => {
    const n = this.node();
    if (!n) return null;
    const combined = n.runtime?.self_test || n.self_test;
    if (!combined || typeof combined !== 'object') return null;
    return JSON.stringify(combined, null, 2);
  });

  isBusy = computed(() => {
    const n = this.node();
    return !!n && (n.status === 'BUSY' || !!n.current_task);
  });

  isQuarantined = computed(() => {
    const n = this.node();
    return !!n && (n.status === 'QUARANTINED' || n.update_state?.desired_state === 'QUARANTINED');
  });

  isDisabled = computed(() => {
    const n = this.node();
    return !!n && (n.status === 'OFFLINE' && n.update_state?.desired_state === 'DISABLED');
  });

  availableActions = computed(() => {
    const n = this.node();
    if (!n) return [];
    const status = n.status;
    const desired = n.update_state?.desired_state;
    return this.allActions.filter((a: { value: NodeActionPayload['action']; label: string }) => {
      switch (a.value) {
        case 'drain': return status !== 'DRAINING' && status !== 'OFFLINE' && desired !== 'DISABLED';
        case 'resume': return status === 'DRAINING' || status === 'UPDATING' || desired === 'DRAINING';
        case 'quarantine': return status !== 'QUARANTINED' && status !== 'OFFLINE' && desired !== 'DISABLED';
        case 'unquarantine': return status === 'QUARANTINED' || desired === 'QUARANTINED';
        case 'self-test': return status !== 'BUSY' && status !== 'SELF_TESTING' && status !== 'OFFLINE';
        case 'disable': return status !== 'OFFLINE' && desired !== 'DISABLED';
        case 'enable': return desired === 'DISABLED';
        case 'restart': return status !== 'OFFLINE' && status !== 'UPDATING';
        case 'update': return status !== 'OFFLINE' && status !== 'UPDATING';
        case 'logs': return true;
        default: return true;
      }
    });
  });

  loadNode(): void {
    this.loading.set(true);
    this.error.set(null);
    this.api.getNode(this.route.snapshot.paramMap.get('id') || '').subscribe({
      next: (n) => {
        this.node.set(n);
        this.loading.set(false);
        if (n.role === 'core') this.loadRoles();
      },
      error: (err) => { this.error.set(err.message || 'Не вдалося завантажити вузол'); this.loading.set(false); },
    });
  }

  loadRoles(): void {
    this.api.getSystemRoles().subscribe({
      next: (response: SystemRolesResponse) => {
        this.availableRoles.set(response.available_roles || []);
        this.selectedRoles.set(response.active_roles || []);
      },
      error: (err) => this.toast.show(err.message || 'Не вдалося завантажити ролі вузла', 'error'),
    });
  }

  toggleRole(role: string, event: Event): void {
    const checked = (event.target as HTMLInputElement).checked;
    this.selectedRoles.update(current => checked
      ? Array.from(new Set([...current, role]))
      : current.filter(item => item !== role));
  }

  saveRoles(): void {
    this.rolesSaving.set(true);
    this.api.updateSystemRoles(this.selectedRoles()).subscribe({
      next: () => {
        this.rolesSaving.set(false);
        this.toast.show('Зміни ролей передано системному виконавцю', 'success');
        this.loadRoles();
      },
      error: (err) => {
        this.rolesSaving.set(false);
        this.toast.show(err.message || 'Не вдалося змінити ролі', 'error');
      },
    });
  }

  runAction(action: NodeActionPayload): void {
    if (action.action === 'logs') {
      const nodeName = this.node()?.node_name;
      if (nodeName) {
        this.router.navigate(['/logs'], { queryParams: { node_name: nodeName } });
      }
      return;
    }
    this.actioning.set(true);
    this.api.workerAction(this.route.snapshot.paramMap.get('id') || '', action).subscribe({
      next: () => {
        this.actioning.set(false);
        this.toast.show('Дія застосована', 'success');
        this.loadNode();
      },
      error: (err) => {
        this.actioning.set(false);
        this.toast.show(err.message || 'Помилка виконання дії', 'error');
      },
    });
  }

  revoke(): void {
    this.actioning.set(true);
    this.api.revokeNode(this.route.snapshot.paramMap.get('id') || '').subscribe({
      next: () => {
        this.actioning.set(false);
        this.toast.show('Вузол відкликано', 'success');
        this.router.navigate(['/workers']);
      },
      error: (err) => {
        this.actioning.set(false);
        this.toast.show(err.message || 'Помилка відкликання', 'error');
      },
    });
  }
}
