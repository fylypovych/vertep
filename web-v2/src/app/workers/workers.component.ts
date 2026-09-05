import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { VertepApiService } from '../core/vertep-api.service';
import { Worker } from '../core/models';

@Component({
  selector: 'app-workers',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="bg-white rounded-xl border border-slate-200 p-5">
      <div class="flex items-center justify-between mb-4">
        <h3 class="text-lg font-semibold text-slate-900">Воркери</h3>
        <button class="px-4 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 text-sm font-medium">
          Додати вузол
        </button>
      </div>
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
            @for (worker of workers; track worker.node_id) {
              <tr class="border-t border-slate-100">
                <td class="px-4 py-3">
                  <div class="font-medium text-slate-900">{{ worker.node_name }}</div>
                  <div class="text-xs text-slate-500">{{ worker.node_id }}</div>
                </td>
                <td class="px-4 py-3">{{ worker.role }}</td>
                <td class="px-4 py-3 text-xs text-slate-600">{{ worker.capabilities?.join(', ') || '-' }}</td>
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
                  <button class="text-emerald-600 hover:text-emerald-700 text-sm font-medium">Налаштування</button>
                </td>
              </tr>
            }
          </tbody>
        </table>
      </div>
    </div>
  `,
})
export class WorkersComponent implements OnInit {
  workers: Worker[] = [];

  constructor(private api: VertepApiService) {}

  ngOnInit(): void {
    this.api.getWorkers().subscribe({
      next: (workers) => (this.workers = workers),
      error: () => {},
    });
  }
}
