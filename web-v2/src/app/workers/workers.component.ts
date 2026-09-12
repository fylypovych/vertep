import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterModule } from '@angular/router';
import { Subscription, timer } from 'rxjs';
import { VertepApiService } from '../core/api.service';
import { ToastService } from '../core/services/toast.service';
import { ConfirmService } from '../core/services/confirm.service';
import { Worker, RegistrationTokenResponse, NodeActionPayload, WizardState } from '../core/models';
import { roleLabel, workerStatusLabel } from '../core/presentation';
import { VertepDatePipe } from '../shared/vertep-date.pipe';
import { LoadingStateComponent } from '../shared/loading-state.component';
import { ErrorStateComponent } from '../shared/error-state.component';
import { EmptyStateComponent } from '../shared/empty-state.component';

@Component({
  selector: 'app-workers',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterModule, VertepDatePipe, LoadingStateComponent, ErrorStateComponent, EmptyStateComponent],
  template: `
    <div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="workers-page">
      <div class="flex items-center justify-between mb-4">
        <h3 class="text-lg font-semibold text-slate-900">Вузли Vertep</h3>
        <button (click)="openWizard()" data-testid="create-worker-button" class="px-4 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 text-sm font-medium">
          Додати вузол
        </button>
      </div>

      <div class="mb-4">
        <input [(ngModel)]="search" data-testid="workers-search" placeholder="Пошук за назвою або ID..." class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-sm">
      </div>

      @if (loading()) {
        <app-loading-state />
      } @else if (error()) {
        <app-error-state [message]="error()!" (retry)="loadWorkers()" />
      } @else {
        <div class="overflow-x-auto">
          <table class="w-full text-sm text-left" data-testid="workers-table">
            <thead class="text-xs text-slate-500 uppercase bg-slate-50">
              <tr>
                <th class="px-4 py-3">Назва</th>
                <th class="px-4 py-3">Роль</th>
                <th class="px-4 py-3">Можливості</th>
                <th class="px-4 py-3">Статус</th>
                <th class="px-4 py-3">Навантаження</th>
                <th class="px-4 py-3">Дії</th>
              </tr>
            </thead>
            <tbody>
               @for (worker of pagedWorkers; track worker.node_id) {
                 <tr class="border-t border-slate-100">
                   <td class="px-4 py-3">
                     <a [routerLink]="['/workers', worker.node_id]" class="font-medium text-slate-900 hover:text-emerald-600">{{ worker.node_name }}</a>
                     <div class="text-xs text-slate-500">{{ worker.node_id }}</div>
                   </td>
                  <td class="px-4 py-3">{{ nodeRoleLabel(worker.role) }}</td>
                  <td class="px-4 py-3 text-xs text-slate-600">{{ worker.capabilities ? worker.capabilities.join(', ') : '-' }}</td>
                  <td class="px-4 py-3">
                    <span class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium"
                      [class.bg-emerald-50]="['READY', 'ONLINE', 'FREE'].includes(worker.status)"
                      [class.text-emerald-700]="['READY', 'ONLINE', 'FREE'].includes(worker.status)"
                      [class.bg-slate-100]="!['READY', 'ONLINE', 'FREE'].includes(worker.status)"
                      [class.text-slate-600]="!['READY', 'ONLINE', 'FREE'].includes(worker.status)">
                      <span class="w-1.5 h-1.5 rounded-full"
                        [class.bg-emerald-500]="['READY', 'ONLINE', 'FREE'].includes(worker.status)"
                        [class.bg-slate-400]="!['READY', 'ONLINE', 'FREE'].includes(worker.status)"></span>
                      {{ nodeStatusLabel(worker.status) }}
                    </span>
                  </td>
                  <td class="px-4 py-3">{{ nodeLoad(worker) }}{{ worker.temperature != null ? ' · ' + worker.temperature + '°C' : '' }}</td>
                  <td class="px-4 py-3">
                    <button (click)="openSettings(worker)" class="text-emerald-600 hover:text-emerald-700 text-sm font-medium mr-2">Налаштування</button>
                    <button (click)="deleteWorker(worker)" class="text-red-600 hover:text-red-700 text-sm font-medium">Видалити</button>
                  </td>
                </tr>
              } @empty {
                <tr><td colspan="6" class="px-4 py-6 text-center text-slate-500" data-testid="workers-empty">Воркерів не знайдено</td></tr>
              }
            </tbody>
          </table>
        </div>
        @if (pages > 1) {
          <div class="flex items-center justify-between mt-4">
            <button (click)="prevPage()" [disabled]="page === 1" class="px-3 py-1.5 text-sm border border-slate-200 rounded-lg disabled:opacity-50">Назад</button>
            <span class="text-sm text-slate-600">Сторінка {{ page }} з {{ pages }}</span>
            <button (click)="nextPage()" [disabled]="page === pages" class="px-3 py-1.5 text-sm border border-slate-200 rounded-lg disabled:opacity-50">Вперед</button>
          </div>
        }
      }
    </div>

    <!-- Worker Onboarding Wizard -->
    <div *ngIf="showWizard" data-testid="worker-wizard-modal" class="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div class="bg-white rounded-xl p-6 w-full max-w-md mx-4">
        <h3 class="text-lg font-semibold text-slate-900 mb-4">Додати вузол</h3>

        @if (!tokenResult()) {
          <div class="space-y-4">
            <div>
              <label class="block text-sm font-medium text-slate-700 mb-1">Роль</label>
              <select [(ngModel)]="wizard.role" data-testid="worker-role-select" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500">
                <option value="gpu">GPU-вузол</option>
                <option value="text">Текстовий вузол</option>
                <option value="voice">Голосовий вузол</option>
                <option value="publisher">Вузол публікації</option>
                <option value="backup">Вузол резервного копіювання</option>
                <option value="monitoring">Вузол моніторингу</option>
              </select>
            </div>
            <div class="flex justify-end gap-2 mt-6">
              <button (click)="showWizard = false" class="px-4 py-2 text-slate-600 hover:text-slate-800 text-sm font-medium">Скасувати</button>
              <button (click)="generateToken()" [disabled]="creating" data-testid="generate-token-button" class="px-4 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 text-sm font-medium disabled:opacity-50">{{ creating ? 'Генерація...' : 'Згенерувати токен' }}</button>
            </div>
          </div>
        } @else if (pollingNode()) {
          <div class="space-y-3">
            <p class="text-sm text-slate-700">{{ onboardingStatus() }}</p>
            <div class="flex items-center gap-3">
              <div class="w-5 h-5 border-2 border-emerald-500 border-t-transparent rounded-full animate-spin"></div>
              <span class="text-sm text-slate-600">Перевірка /api/nodes</span>
            </div>
            <div class="bg-slate-50 rounded-lg p-3 text-xs space-y-1" data-testid="token-display">
              <p><span class="font-medium">Токен:</span> {{ tokenResult()!.token }}</p>
              <p><span class="font-medium">Діє до:</span> {{ tokenResult()!.expires_at | vertepDate }}</p>
              <p><span class="font-medium">Роль:</span> {{ tokenResult()!.role }}</p>
            </div>
          </div>
        } @else if (registeredNode()) {
          <div class="space-y-3">
            <h4 class="text-sm font-medium text-emerald-900">Вузол зареєстровано</h4>
            <div class="bg-slate-50 rounded-lg p-3 text-xs space-y-1" data-testid="token-display">
              <p><span class="font-medium">ID:</span> {{ registeredNode()!.node_id }}</p>
              <p><span class="font-medium">Назва:</span> {{ registeredNode()!.node_name }}</p>
              <p><span class="font-medium">Роль:</span> {{ registeredNode()!.role }}</p>
              <p><span class="font-medium">Статус:</span> {{ registeredNode()!.status }}</p>
            </div>
            <div class="flex justify-end gap-2 mt-6">
              <button (click)="closeWizard()" class="px-4 py-2 text-slate-600 hover:text-slate-800 text-sm font-medium">Закрити</button>
            </div>
          </div>
        } @else {
          <div class="space-y-3">
            <p class="text-sm text-slate-700">Використовуйте ці дані для реєстрації вузла:</p>
            <div class="bg-slate-50 rounded-lg p-3 text-xs space-y-1" data-testid="token-display">
              <p><span class="font-medium">Токен:</span> {{ tokenResult()!.token }}</p>
              <p><span class="font-medium">Діє до:</span> {{ tokenResult()!.expires_at | vertepDate }}</p>
              <p><span class="font-medium">Роль:</span> {{ tokenResult()!.role }}</p>
              <p><span class="font-medium">Core URL:</span> {{ locationOrigin }}/api/nodes/register</p>
            </div>
            <div class="flex justify-end gap-2 mt-6">
              <button (click)="showWizard = false" class="px-4 py-2 text-slate-600 hover:text-slate-800 text-sm font-medium">Закрити</button>
              <button (click)="startPolling()" [disabled]="polling" class="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm font-medium disabled:opacity-50">{{ polling ? 'Очікування...' : 'Перевірити статус' }}</button>
            </div>
          </div>
        }
      </div>
    </div>

    <!-- Worker Settings Modal -->
    <div *ngIf="showSettings" class="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div class="bg-white rounded-xl p-6 w-full max-w-md mx-4">
        <h3 class="text-lg font-semibold text-slate-900 mb-4">Налаштування: {{ selectedWorker?.node_name }}</h3>
        <div class="space-y-4">
          <div>
            <label class="block text-sm font-medium text-slate-700 mb-1">Дія</label>
            <select [(ngModel)]="workerAction" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500">
              <option value="">Обрати дію...</option>
              <option value="drain">Завершити поточні завдання</option>
              <option value="disable">Вимкнути</option>
              <option value="enable">Увімкнути</option>
              <option value="restart">Перезапустити</option>
            </select>
          </div>
        </div>
        <div class="flex justify-end gap-2 mt-6">
          <button (click)="showSettings = false" class="px-4 py-2 text-slate-600 hover:text-slate-800 text-sm font-medium">Закрити</button>
          <button (click)="applyAction()" [disabled]="actioning" class="px-4 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 text-sm font-medium disabled:opacity-50">{{ actioning ? 'Застосування...' : 'Застосувати' }}</button>
        </div>
      </div>
    </div>
  `,
})
export class WorkersComponent implements OnInit {
  workers = signal<Worker[]>([]);
  loading = signal(false);
  error = signal<string | null>(null);
  showWizard = false;
  showSettings = false;
  selectedWorker: Worker | null = null;
  creating = false;
  actioning = false;
  wizard: WizardState = { role: 'gpu' };
  workerAction = '';
  search = '';
  page = 1;
  pageSize = 10;
  tokenResult = signal<RegistrationTokenResponse | null>(null);
  pollingNode = signal(false);
  registeredNode = signal<Worker | null>(null);
  polling = false;
  locationOrigin = window.location.origin;
  onboardingStatus = signal('Очікування реєстрації вузла...');
  private knownNodeIds = new Set<string>();
  private pollTimer: Subscription | null = null;
  private pollingInterval = 5000;

  constructor(private api: VertepApiService, private toast: ToastService, private confirm: ConfirmService) {}

  ngOnInit(): void {
    this.loadWorkers();
    this.startAutoPolling();
  }

  ngOnDestroy(): void {
    this.stopAutoPolling();
  }

  private startAutoPolling(): void {
    this.stopAutoPolling();
    this.pollTimer = timer(0, this.pollingInterval).subscribe(() => {
      if (!this.showWizard) {
        this.loadWorkers();
      }
    });
  }

  private stopAutoPolling(): void {
    if (this.pollTimer) {
      this.pollTimer.unsubscribe();
      this.pollTimer = null;
    }
  }

  get filteredWorkers(): Worker[] {
    const list = this.workers();
    if (!this.search.trim()) return list;
    const term = this.search.toLowerCase();
    return list.filter(w =>
      (w.node_name || '').toLowerCase().includes(term) ||
      (w.node_id || '').toLowerCase().includes(term)
    );
  }

  get pagedWorkers(): Worker[] {
    const start = (this.page - 1) * this.pageSize;
    return this.filteredWorkers.slice(start, start + this.pageSize);
  }

  get pages(): number {
    return Math.max(1, Math.ceil(this.filteredWorkers.length / this.pageSize));
  }

  loadWorkers(): void {
    this.loading.set(true);
    this.error.set(null);
    this.api.getWorkers().subscribe({
      next: (workers) => { this.workers.set(workers); this.loading.set(false); },
      error: (err) => { this.error.set(err.message); this.loading.set(false); },
    });
  }

  nodeRoleLabel(role: string): string { return roleLabel(role); }
  nodeStatusLabel(status: string): string { return workerStatusLabel(status); }
  nodeLoad(worker: Worker): string {
    const value = worker.gpu_load ?? worker.cpu_load;
    return value == null ? 'Немає даних' : `${value}%`;
  }

  openWizard(): void {
    this.wizard = { role: 'gpu' };
    this.tokenResult.set(null);
    this.pollingNode.set(false);
    this.registeredNode.set(null);
    this.knownNodeIds = new Set(this.workers().map(item => item.node_id));
    this.onboardingStatus.set('Очікування реєстрації вузла...');
    this.showWizard = true;
  }

  closeWizard(): void {
    this.showWizard = false;
    this.tokenResult.set(null);
    this.pollingNode.set(false);
    this.registeredNode.set(null);
    this.loadWorkers();
  }

  generateToken(): void {
    if (!this.wizard.role) return;
    this.creating = true;
    this.api.createRegistrationToken(this.wizard.role).subscribe({
      next: (token) => {
        this.tokenResult.set(token);
        this.creating = false;
        this.toast.show('Токен згенеровано', 'success');
      },
      error: (err) => {
        this.error.set(err.message);
        this.creating = false;
        this.toast.show(err.message || 'Помилка генерації токена', 'error');
      },
    });
  }

  startPolling(): void {
    this.polling = true;
    this.pollingNode.set(true);
    const token = this.tokenResult();
    if (!token) return;

    const maxAttempts = 12;
    let attempts = 0;
    const interval = setInterval(() => {
      attempts++;
      this.api.getNodes().subscribe({
        next: (nodes) => {
          const found = nodes.find(n => !this.knownNodeIds.has(n.node_id) && n.role === token.role);
          if (found?.certificate_serial && found.status === 'SELF_TESTING') {
            this.onboardingStatus.set('Сертифікат видано. Виконується self-test...');
          } else if (found?.certificate_serial) {
            this.onboardingStatus.set('Вузол зареєстровано, сертифікат видано. Очікування self-test...');
          } else if (found) {
            this.onboardingStatus.set('Вузол зареєстровано. Очікування сертифіката...');
          }
          if (found && (found.status === 'READY' || found.status === 'ONLINE')) {
            clearInterval(interval);
            this.registeredNode.set(found);
            this.pollingNode.set(false);
            this.polling = false;
            this.toast.show('Вузол зареєстровано', 'success');
          } else if (attempts >= maxAttempts) {
            clearInterval(interval);
            this.pollingNode.set(false);
            this.polling = false;
            this.toast.show('Таймаут очікування реєстрації', 'error');
          }
        },
        error: () => {
          if (attempts >= maxAttempts) {
            clearInterval(interval);
            this.pollingNode.set(false);
            this.polling = false;
          }
        },
      });
    }, 5000);
  }

  openSettings(worker: Worker): void {
    this.selectedWorker = worker;
    this.workerAction = '';
    this.showSettings = true;
  }

  applyAction(): void {
    if (!this.selectedWorker || !this.workerAction) return;
    this.actioning = true;
    this.api.workerAction(this.selectedWorker.node_id, { action: this.workerAction as NodeActionPayload['action'] }).subscribe({
      next: () => {
        this.showSettings = false;
        this.loadWorkers();
        this.toast.show('Дію застосовано', 'success');
      },
      error: (err) => {
        this.error.set(err.message);
        this.actioning = false;
        this.toast.show(err.message || 'Помилка дії', 'error');
      },
    });
  }

  deleteWorker(worker: Worker): void {
    this.confirm.confirm({ title: 'Видалити вузол', message: `Ви впевнені, що хочете відкликати ${worker.node_name}?` }).subscribe((ok) => {
      if (!ok) return;
      this.api.revokeNode(worker.node_id).subscribe({
        next: () => {
          this.loadWorkers();
          this.toast.show('Вузол відкликано', 'success');
        },
        error: (err) => this.toast.show(err.message || 'Помилка відкликання', 'error'),
      });
    });
  }

  prevPage(): void {
    if (this.page > 1) this.page--;
  }

  nextPage(): void {
    if (this.page < this.pages) this.page++;
  }
}
