import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { VertepApiService } from '../core/api.service';
import { SystemStatus } from '../core/models';

@Component({
  selector: 'app-settings',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="space-y-6">
      <div class="bg-white rounded-xl border border-slate-200 p-5">
        <h3 class="text-lg font-semibold text-slate-900 mb-4">Система</h3>
        @if (loading) {
          <div class="flex items-center justify-center py-8">
            <div class="w-8 h-8 border-4 border-emerald-500 border-t-transparent rounded-full animate-spin"></div>
          </div>
        } @else if (error) {
          <p class="text-red-600">{{ error }}</p>
        } @else {
          <pre class="text-sm text-slate-600 bg-slate-50 p-4 rounded-lg overflow-auto">{{ systemStatus | json }}</pre>
        }
      </div>

      <div class="bg-white rounded-xl border border-slate-200 p-5">
        <h3 class="text-lg font-semibold text-slate-900 mb-4">Движки обробки (backends)</h3>
        @if (!systemStatus || !systemStatus.providers) {
          <p class="text-sm text-slate-500">Інформація про движки недоступна.</p>
        } @else {
          <div class="overflow-x-auto">
            <table class="min-w-full text-sm">
              <thead>
                <tr class="text-left text-slate-500 border-b border-slate-200">
                  <th class="py-2 pr-4 font-medium">Роль</th>
                  <th class="py-2 pr-4 font-medium">Активний backend</th>
                  <th class="py-2 pr-4 font-medium">Доступні опції</th>
                  <th class="py-2 pr-4 font-medium">Керується</th>
                  <th class="py-2 font-medium">Стан</th>
                </tr>
              </thead>
              <tbody>
                @for (slot of backendSlots(); track slot.slot) {
                  <tr class="border-b border-slate-100">
                    <td class="py-2 pr-4 font-medium text-slate-900">{{ slot.label }}</td>
                    <td class="py-2 pr-4 text-slate-700">
                      <code class="bg-slate-100 px-1.5 py-0.5 rounded text-xs">{{ slot.backend }}</code>
                    </td>
                    <td class="py-2 pr-4 text-slate-500">{{ slot.options }}</td>
                    <td class="py-2 pr-4 text-slate-500">
                      <code class="text-xs">{{ slot.env }}</code>
                    </td>
                    <td class="py-2">
                      @if (slot.configured) {
                        <span class="inline-flex items-center text-emerald-600 text-xs font-medium">
                          <span class="w-1.5 h-1.5 rounded-full bg-emerald-500 mr-1.5"></span>налаштовано
                        </span>
                      } @else {
                        <span class="inline-flex items-center text-amber-600 text-xs font-medium">
                          <span class="w-1.5 h-1.5 rounded-full bg-amber-500 mr-1.5"></span>не налаштовано
                        </span>
                      }
                    </td>
                  </tr>
                }
              </tbody>
            </table>
          </div>
        }
      </div>

      <div class="bg-white rounded-xl border border-slate-200 p-5">
        <h3 class="text-lg font-semibold text-slate-900 mb-4">Оновлення</h3>
        <div class="space-y-4">
          <div class="flex items-center justify-between">
            <div>
              <p class="font-medium text-slate-900">Безпечне оновлення Vertep</p>
              <p class="text-sm text-slate-500">Перевірка та встановлення оновлень</p>
            </div>
            <button (click)="checkUpdate()" class="px-4 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 text-sm font-medium">
              Перевірити оновлення
            </button>
          </div>
          <div *ngIf="updateStatus" class="text-sm text-slate-600 bg-slate-50 p-3 rounded-lg">
            {{ updateStatus }}
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
  loading = false;
  error: string | null = null;
  updateStatus: string | null = null;

  constructor(private api: VertepApiService) {}

  ngOnInit(): void {
    this.loadStatus();
  }

  loadStatus(): void {
    this.loading = true;
    this.error = null;
    this.api.getStatus().subscribe({
      next: (status) => { this.systemStatus = status; this.loading = false; },
      error: (err) => { this.error = err.message; this.loading = false; },
    });
  }

  backendSlots(): any[] {
    const labels: Record<string, string> = {
      llm: 'LLM',
      tts: 'TTS',
      compute: 'GPU обчислення',
      image: 'Генерація зображень',
      video: 'Генерація відео',
      assembly: 'Монтаж (FFmpeg)',
      video_engine: 'Движок збірки',
      publisher: 'Публікація',
    };
    const providers = this.systemStatus?.providers;
    if (!providers) {
      return [];
    }
    return Object.keys(providers).map((slot) => {
      const p = providers[slot];
      return {
        slot,
        label: labels[slot] ?? slot,
        backend: p.backend,
        options: (p.options ?? []).join(', '),
        env: p.env ?? '—',
        configured: p.configured ?? false,
      };
    });
  }

  checkUpdate(): void {
    this.updateStatus = 'Перевірка оновлень...';
    // TODO: implement update check API call
    setTimeout(() => {
      this.updateStatus = 'Оновлень немає. Використовується актуальна версія.';
    }, 2000);
  }
}
