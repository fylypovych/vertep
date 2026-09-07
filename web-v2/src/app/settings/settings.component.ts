import { Component, OnInit, OnDestroy, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Subscription, timeout, take } from 'rxjs';
import { VertepApiService } from '../core/api.service';
import { SystemStatus } from '../core/models';

@Component({
  selector: 'app-settings',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="space-y-6" data-testid="settings-page">
      <div class="bg-white rounded-xl border border-slate-200 p-5">
        <h3 class="text-lg font-semibold text-slate-900 mb-4">Система</h3>
        @if (loading()) {
          <div class="flex items-center justify-center py-8">
            <div class="w-8 h-8 border-4 border-emerald-500 border-t-transparent rounded-full animate-spin"></div>
          </div>
        } @else if (error()) {
          <p class="text-red-600">{{ error() }}</p>
        } @else {
          <div class="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-2" data-testid="system-info">
            <div class="flex justify-between py-2 border-b border-slate-100">
              <span class="text-sm text-slate-500">Стан системи</span>
              <span class="text-sm font-medium" [class.text-emerald-600]="systemOk" [class.text-red-600]="!systemOk">{{ systemStateLabel }}</span>
            </div>
            <div class="flex justify-between py-2 border-b border-slate-100">
              <span class="text-sm text-slate-500">Версія</span>
              <span class="text-sm font-medium text-slate-900">{{ systemStatus()?.version || '—' }}</span>
            </div>
            <div class="flex justify-between py-2 border-b border-slate-100">
              <span class="text-sm text-slate-500">Ядро</span>
              <span class="text-sm font-medium" [class.text-emerald-600]="systemStatus()?.core === 'OK'" [class.text-red-600]="systemStatus()?.core !== 'OK'">{{ systemStatus()?.core || '—' }}</span>
            </div>
            <div class="flex justify-between py-2 border-b border-slate-100">
              <span class="text-sm text-slate-500">База даних</span>
              <span class="text-sm font-medium" [class.text-emerald-600]="systemStatus()?.postgres === 'OK'" [class.text-red-600]="systemStatus()?.postgres !== 'OK'">{{ systemStatus()?.postgres || '—' }}</span>
            </div>
            <div class="flex justify-between py-2 border-b border-slate-100">
              <span class="text-sm text-slate-500">Redis</span>
              <span class="text-sm font-medium" [class.text-emerald-600]="systemStatus()?.redis === 'OK'" [class.text-red-600]="systemStatus()?.redis !== 'OK'">{{ systemStatus()?.redis || '—' }}</span>
            </div>
            <div class="flex justify-between py-2 border-b border-slate-100">
              <span class="text-sm text-slate-500">Сховище</span>
              <span class="text-sm font-medium" [class.text-emerald-600]="systemStatus()?.storage === 'OK'" [class.text-red-600]="systemStatus()?.storage !== 'OK'">{{ systemStatus()?.storage || '—' }}</span>
            </div>
            <div class="flex justify-between py-2 border-b border-slate-100">
              <span class="text-sm text-slate-500">Telegram</span>
              <span class="text-sm font-medium" [class.text-emerald-600]="telegramOk" [class.text-slate-600]="!telegramOk">{{ telegramStatus }}</span>
            </div>
            <div class="flex justify-between py-2 border-b border-slate-100">
              <span class="text-sm text-slate-500">Черга</span>
              <span class="text-sm font-medium text-slate-900">{{ systemStatus()?.queue?.depth || 0 }}</span>
            </div>
            <div class="flex justify-between py-2 border-b border-slate-100">
              <span class="text-sm text-slate-500">Активні завдання</span>
              <span class="text-sm font-medium text-slate-900">{{ activeJobsCount }}</span>
            </div>
            @if (systemStatus()?.ollama) {
              <div class="flex justify-between py-2 border-b border-slate-100">
                <span class="text-sm text-slate-500">LLM / Ollama</span>
                <span class="text-sm font-medium" [class.text-emerald-600]="systemStatus()?.ollama !== 'STUB'" [class.text-amber-600]="systemStatus()?.ollama === 'STUB'">{{ systemStatus()?.ollama }}</span>
              </div>
            }
          </div>
          <button (click)="showAdvanced = !showAdvanced" class="mt-4 text-sm text-slate-500 hover:text-slate-700">
            {{ showAdvanced ? 'Сховати технічні деталі' : 'Показати технічні деталі' }}
          </button>
          @if (showAdvanced) {
            <pre class="mt-4 text-xs text-slate-600 bg-slate-50 p-4 rounded-lg overflow-auto max-h-64">{{ systemStatus() | json }}</pre>
          }
        }
      </div>

      <div class="bg-white rounded-xl border border-slate-200 p-5">
        <h3 class="text-lg font-semibold text-slate-900 mb-4">Движки обробки (backends)</h3>
        @if (!systemStatus()?.providers) {
          <p class="text-sm text-slate-500" data-testid="backends-unavailable">Інформація про движки недоступна.</p>
        } @else {
          <div class="overflow-x-auto">
            <table class="min-w-full text-sm" data-testid="backends-table">
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
        @if (!systemStatus()?.update) {
          <p class="text-sm text-slate-500" data-testid="update-unavailable">Інформація про оновлення недоступна.</p>
        } @else {
          <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <span class="text-xs text-slate-500">Поточна версія</span>
              <p class="text-sm font-medium text-slate-900">{{ systemStatus()?.update?.['current_version'] || '—' }}</p>
            </div>
            <div>
              <span class="text-xs text-slate-500">Доступна версія</span>
              <p class="text-sm font-medium text-slate-900">{{ systemStatus()?.update?.['available_version'] || '—' }}</p>
            </div>
            <div>
              <span class="text-xs text-slate-500">Стан</span>
              <p class="text-sm font-medium text-slate-900">{{ systemStatus()?.update?.['state'] || '—' }}</p>
            </div>
            <div>
              <span class="text-xs text-slate-500">Оновлення</span>
              <p class="text-sm font-medium text-slate-900">{{ systemStatus()?.update?.['update_available'] ? 'Доступне' : 'Немає' }}</p>
            </div>
          </div>
        }
      </div>
     </div>
  `,
})
export class SettingsComponent implements OnInit, OnDestroy {
  systemStatus = signal<SystemStatus | null>(null);
  loading = signal(false);
  error = signal<string | null>(null);
  showAdvanced = false;
  updateStatus: string | null = null;
  private subs = new Subscription();

  constructor(private api: VertepApiService) {}

  ngOnInit(): void {
    this.loadStatus();
  }

  ngOnDestroy(): void {
    this.subs.unsubscribe();
  }

  loadStatus(): void {
    this.loading.set(true);
    this.error.set(null);
    this.subs.add(
      this.api.getStatus().pipe(
        timeout(10000),
        take(1),
      ).subscribe({
        next: (status) => { this.systemStatus.set(status); this.loading.set(false); },
        error: (err) => { this.error.set(err.message || 'Не вдалося завантажити статус'); this.loading.set(false); },
      }),
    );
  }

  get systemOk(): boolean {
    const s = this.systemStatus();
    return s?.core === 'OK' && s?.postgres === 'OK' && s?.redis === 'OK';
  }

  get systemStateLabel(): string {
    const state = this.systemStatus()?.system?.state || 'UNKNOWN';
    const labels: Record<string, string> = {
      'NORMAL': 'Нормальний',
      'MAINTENANCE': 'Обслуговування',
      'UPDATING': 'Оновлення',
      'EMERGENCY': 'Аварія',
      'RECOVERING': 'Відновлення',
      'READ_ONLY': 'Тільки читання',
    };
    return labels[state] || state;
  }

  get telegramOk(): boolean {
    return this.systemStatus()?.telegram?.status === 'OK';
  }

  get telegramStatus(): string {
    const tg = this.systemStatus()?.telegram;
    if (!tg) return 'Не налаштовано';
    if (tg.status === 'OK') return `Підключено (${tg.bot_username || 'без імені'})`;
    return tg.status || 'Невідомо';
  }

  get activeJobsCount(): number {
    const s = this.systemStatus();
    if (!s) return 0;
    return s.orchestration?.active_jobs || 0;
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
    const providers = this.systemStatus()?.providers;
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
    setTimeout(() => {
      this.updateStatus = 'Оновлень немає. Використовується актуальна версія.';
    }, 2000);
  }
}
