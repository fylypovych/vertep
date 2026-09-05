import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { VertepApiService } from '../core/api.service';
import { ToastService } from '../core/services/toast.service';
import { ConfirmService } from '../core/services/confirm.service';
import { Worker } from '../core/models';

@Component({
  selector: 'app-workers',
  standalone: true,
  imports: [CommonModule, FormsModule],
  template: `
    <div class="bg-white rounded-xl border border-slate-200 p-5">
      <div class="flex items-center justify-between mb-4">
        <h3 class="text-lg font-semibold text-slate-900">Воркери</h3>
        <button (click)="openWizard()" class="px-4 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 text-sm font-medium">
          Додати вузол
        </button>
      </div>

      <div class="mb-4">
        <input [(ngModel)]="search" placeholder="Пошук за назвою або ID..." class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-sm">
      </div>

      @if (loading) {
        <div class="space-y-3">
          @for (_ of [1,2,3]; track $index) {
            <div class="animate-pulse bg-slate-100 rounded-lg h-16"></div>
          }
        </div>
      } @else if (error) {
        <div class="bg-red-50 border border-red-200 rounded-xl p-5">
          <p class="text-red-700">{{ error }}</p>
          <button (click)="loadWorkers()" class="mt-2 text-sm text-red-600 hover:text-red-700 font-medium">Повторити</button>
        </div>
      } @else {
        <div class="overflow-x-auto">
          <table class="w-full text-sm text-left">
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
                    <div class="font-medium text-slate-900">{{ worker.node_name }}</div>
                    <div class="text-xs text-slate-500">{{ worker.node_id }}</div>
                  </td>
                  <td class="px-4 py-3">{{ worker.role }}</td>
                  <td class="px-4 py-3 text-xs text-slate-600">{{ worker.capabilities ? worker.capabilities.join(', ') : '-' }}</td>
                  <td class="px-4 py-3">
                    <span class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium"
                      [class.bg-emerald-50]="worker.status === 'ONLINE'"
                      [class.text-emerald-700]="worker.status === 'ONLINE'"
                      [class.bg-slate-100]="worker.status !== 'ONLINE'"
                      [class.text-slate-600]="worker.status !== 'ONLINE'">
                      <span class="w-1.5 h-1.5 rounded-full"
                        [class.bg-emerald-500]="worker.status === 'ONLINE'"
                        [class.bg-slate-400]="worker.status !== 'ONLINE'"></span>
                      {{ worker.status }}
                    </span>
                  </td>
                  <td class="px-4 py-3">{{ worker.load || 0 }}%</td>
                  <td class="px-4 py-3">
                    <button (click)="openSettings(worker)" class="text-emerald-600 hover:text-emerald-700 text-sm font-medium mr-2">Налаштування</button>
                    <button (click)="deleteWorker(worker)" class="text-red-600 hover:text-red-700 text-sm font-medium">Видалити</button>
                  </td>
                </tr>
              } @empty {
                <tr><td colspan="6" class="px-4 py-6 text-center text-slate-500">Воркерів не знайдено</td></tr>
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

    <!-- Worker Wizard Modal -->
    <div *ngIf="showWizard" class="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div class="bg-white rounded-xl p-6 w-full max-w-md mx-4">
        <h3 class="text-lg font-semibold text-slate-900 mb-4">Додати вузол</h3>
        <div class="space-y-4">
          <div>
            <label class="block text-sm font-medium text-slate-700 mb-1">Назва вузла</label>
            <input [(ngModel)]="wizard.name" placeholder="worker-01" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500">
          </div>
          <div>
            <label class="block text-sm font-medium text-slate-700 mb-1">Роль</label>
            <select [(ngModel)]="wizard.role" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500">
              <option value="gpu">GPU-вузол</option>
              <option value="text">Текстовий вузол</option>
              <option value="voice">Голосовий вузол</option>
              <option value="publisher">Вузол публікації</option>
              <option value="backup">Вузол резервного копіювання</option>
              <option value="monitoring">Вузол моніторингу</option>
            </select>
          </div>
        </div>
        <div class="flex justify-end gap-2 mt-6">
          <button (click)="showWizard = false" class="px-4 py-2 text-slate-600 hover:text-slate-800 text-sm font-medium">Скасувати</button>
          <button (click)="createWorker()" [disabled]="creating" class="px-4 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 text-sm font-medium disabled:opacity-50">{{ creating ? 'Створення...' : 'Додати' }}</button>
        </div>
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
  workers: Worker[] = [];
  loading = false;
  error: string | null = null;
  showWizard = false;
  showSettings = false;
  selectedWorker: Worker | null = null;
  creating = false;
  actioning = false;
  wizard: any = { name: '', role: 'gpu' };
  workerAction = '';
  search = '';
  page = 1;
  pageSize = 10;

  constructor(private api: VertepApiService, private toast: ToastService, private confirm: ConfirmService) {}

  ngOnInit(): void {
    this.loadWorkers();
  }

  get filteredWorkers(): Worker[] {
    if (!this.search.trim()) return this.workers;
    const term = this.search.toLowerCase();
    return this.workers.filter(w =>
      w.node_name.toLowerCase().includes(term) ||
      w.node_id.toLowerCase().includes(term)
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
    this.loading = true;
    this.error = null;
    this.api.getWorkers().subscribe({
      next: (workers) => { this.workers = workers; this.loading = false; },
      error: (err) => { this.error = err.message; this.loading = false; },
    });
  }

  openWizard(): void {
    this.wizard = { name: '', role: 'gpu' };
    this.showWizard = true;
  }

  createWorker(): void {
    if (!this.wizard.name) return;
    this.creating = true;
    this.api.createWorker(this.wizard).subscribe({
      next: () => {
        this.showWizard = false;
        this.loadWorkers();
        this.toast.show('Вузол додано', 'success');
      },
      error: (err) => {
        this.error = err.message;
        this.creating = false;
        this.toast.show(err.message || 'Помилка створення', 'error');
      },
    });
  }

  openSettings(worker: Worker): void {
    this.selectedWorker = worker;
    this.workerAction = '';
    this.showSettings = true;
  }

  applyAction(): void {
    if (!this.selectedWorker || !this.workerAction) return;
    this.actioning = true;
    this.api.workerAction(this.selectedWorker.node_id, this.workerAction).subscribe({
      next: () => {
        this.showSettings = false;
        this.loadWorkers();
        this.toast.show('Дію застосовано', 'success');
      },
      error: (err) => {
        this.error = err.message;
        this.actioning = false;
        this.toast.show(err.message || 'Помилка дії', 'error');
      },
    });
  }

  deleteWorker(worker: Worker): void {
    this.confirm.confirm({ title: 'Видалити вузол', message: `Ви впевнені, що хочете видалити ${worker.node_name}?` }).subscribe((ok) => {
      if (!ok) return;
      this.api.deleteWorker(worker.node_id).subscribe({
        next: () => {
          this.loadWorkers();
          this.toast.show('Вузол видалено', 'success');
        },
        error: (err) => this.toast.show(err.message || 'Помилка видалення', 'error'),
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
