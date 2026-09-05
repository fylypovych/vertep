import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { VertepApiService } from '../core/vertep-api.service';
import { SystemStatus } from '../core/models';

@Component({
  selector: 'app-settings',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="space-y-6">
      <div class="bg-white rounded-xl border border-slate-200 p-5">
        <h3 class="text-lg font-semibold text-slate-900 mb-4">Система</h3>
        <pre class="text-sm text-slate-600 bg-slate-50 p-4 rounded-lg overflow-auto">{{ systemStatus | json }}</pre>
      </div>
      <div class="bg-white rounded-xl border border-slate-200 p-5">
        <h3 class="text-lg font-semibold text-slate-900 mb-4">Оновлення</h3>
        <div class="space-y-4">
          <div class="flex items-center justify-between">
            <div>
              <p class="font-medium text-slate-900">Безпечне оновлення Vertep</p>
              <p class="text-sm text-slate-500">Перевірка та встановлення оновлень</p>
            </div>
            <button class="px-4 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 text-sm font-medium">
              Перевірити оновлення
            </button>
          </div>
        </div>
      </div>
      <div class="bg-white rounded-xl border border-slate-200 p-5">
        <h3 class="text-lg font-semibold text-slate-900 mb-4">Ліцензія</h3>
        <p class="text-sm text-slate-600">Інформація про ліцензію буде відображена тут.</p>
      </div>
    </div>
  `,
})
export class SettingsComponent implements OnInit {
  systemStatus: SystemStatus | null = null;

  constructor(private api: VertepApiService) {}

  ngOnInit(): void {
    this.api.getStatus().subscribe({
      next: (status) => (this.systemStatus = status),
      error: () => {},
    });
  }
}
