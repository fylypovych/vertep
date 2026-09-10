import { Component, OnInit, OnDestroy, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Subscription, timeout, take } from 'rxjs';
import { VertepApiService } from '../core/api.service';
import { ToastService } from '../core/services/toast.service';
import { ConfirmService } from '../core/services/confirm.service';
import { SystemStatus, SystemRole, SystemRolesResponse, TelegramStatus, TelegramBotInfo, IntegrationStatus, ModelInfo, BackupInfo, UpdateReadiness, RollingStatus, CertificateStatus, UpdateStatus } from '../core/models';
import { VertepDatePipe } from '../shared/vertep-date.pipe';

interface SecretGroup {
  label: string;
  secrets: string[];
}

@Component({
  selector: 'app-settings',
  standalone: true,
  imports: [CommonModule, FormsModule, VertepDatePipe],
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
              <p class="text-sm font-medium text-slate-900" data-testid="status-update-current-version">{{ systemStatus()?.update?.['current_version'] || '—' }}</p>
            </div>
            <div>
              <span class="text-xs text-slate-500">Доступна версія</span>
              <p class="text-sm font-medium text-slate-900" data-testid="status-update-available-version">{{ systemStatus()?.update?.['available_version'] || '—' }}</p>
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

      <!-- Roles and Capabilities (V2-404) -->
      <div class="bg-white rounded-xl border border-slate-200 p-5">
        <h3 class="text-lg font-semibold text-slate-900 mb-4">Ролі та можливості</h3>
        @if (rolesLoading()) {
          <div class="animate-pulse space-y-2">
            <div class="h-5 bg-slate-200 rounded w-full"></div>
            <div class="h-5 bg-slate-200 rounded w-3/4"></div>
          </div>
        } @else if (rolesError()) {
          <p class="text-red-600">{{ rolesError() }}</p>
        } @else {
          <div class="text-xs text-slate-500 mb-3">Активні ролі: {{ selectedRoles.length ? selectedRoles.join(', ') : 'базова конфігурація' }}</div>
          <div class="flex flex-wrap gap-2 mb-3">
            @for (role of allRoles; track role.id) {
              <span class="px-2 py-1 text-xs border border-slate-200 rounded-lg"
                    [class.bg-emerald-50]="selectedRoles.includes(role.id)"
                    [class.border-emerald-300]="selectedRoles.includes(role.id)">
                <input type="checkbox" [checked]="selectedRoles.includes(role.id)"
                       (change)="toggleRole(role.id, $event)" class="mr-1">
                {{ role.label }}
                @if (role.services && role.services.length) {
                  <span class="text-slate-400 ml-1">({{ role.services.join(', ') }})</span>
                }
              </span>
            }
          </div>
          @if (rolesSaveMessage()) {
            <div class="mb-3 text-sm"
                 [class.text-amber-600]="rolesSaveState() === 'QUEUED' || rolesSaveState() === 'APPLYING'"
                 [class.text-red-600]="rolesSaveState() === 'ERROR' || rolesSaveState() === 'FAILED'"
                 [class.text-emerald-600]="rolesSaveState() === 'COMPLETED'"
                 data-testid="roles-deployment-status">
              {{ rolesSaveMessage() }}
              @if (rolesSaveState() === 'APPLYING' || rolesSaveState() === 'QUEUED') {
                <span class="inline-block ml-2 animate-pulse">⏳</span>
              }
            </div>
          }
          @if (rolesResponse?.deployment) {
            <div class="mb-3 text-xs text-slate-500" data-testid="roles-deployment-detail">
              @if (rolesResponse!.deployment!.services && rolesResponse!.deployment!.services!.length) {
                <div>Сервіси: {{ rolesResponse!.deployment!.services!.join(', ') }}</div>
              }
              @if (rolesResponse!.deployment!.error) {
                <div class="text-red-600 mt-1">Помилка: {{ rolesResponse!.deployment!.error }}</div>
              }
            </div>
          }
          <button (click)="saveRoles()" [disabled]="savingRoles()" data-testid="roles-save-button" class="px-3 py-1.5 text-sm bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 disabled:opacity-50">
            {{ savingRoles() ? 'Збереження...' : 'Зберегти ролі' }}
          </button>
        }
      </div>

      <!-- Telegram Settings (V2-405) -->
      <div class="bg-white rounded-xl border border-slate-200 p-5">
        <h3 class="text-lg font-semibold text-slate-900 mb-4">Telegram</h3>
        @if (tgLoading()) {
          <div class="animate-pulse space-y-2">
            <div class="h-5 bg-slate-200 rounded w-full"></div>
            <div class="h-5 bg-slate-200 rounded w-1/2"></div>
          </div>
        } @else if (tgError()) {
          <p class="text-red-600">{{ tgError() }}</p>
        } @else if (tgStatus()) {
          <div class="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-2 text-sm">
            <div class="flex justify-between py-2 border-b border-slate-100">
              <span class="text-slate-500">Статус</span>
              <span class="font-medium" [class.text-emerald-600]="tgStatus()!.configured" [class.text-slate-400]="!tgStatus()!.configured">
                {{ tgStatus()!.configured ? 'Налаштовано' : 'Не налаштовано' }}
              </span>
            </div>
            <div class="flex justify-between py-2 border-b border-slate-100">
              <span class="text-slate-500">Bot username</span>
              <span class="font-medium">{{ tgStatus()!.bot_username || '—' }}</span>
            </div>
            <div class="flex justify-between py-2 border-b border-slate-100">
              <span class="text-slate-500">Режим</span>
              <span class="font-medium">{{ tgStatus()!.polling_enabled ? 'Polling' : 'Webhook' }}</span>
            </div>
            <div class="flex justify-between py-2 border-b border-slate-100">
              <span class="text-slate-500">Webhook URL</span>
              <span class="font-mono text-xs break-all">{{ tgStatus()!.webhook_url || '—' }}</span>
            </div>
            <div class="flex justify-between py-2 border-b border-slate-100">
              <span class="text-slate-500">Allowed chat IDs</span>
              <span class="font-mono text-xs break-all">{{ tgStatus()!.allowed_chat_ids || '—' }}</span>
            </div>
            <div class="flex justify-between py-2 border-b border-slate-100">
              <span class="text-slate-500">Admin chat IDs</span>
              <span class="font-mono text-xs break-all">{{ tgStatus()!.admin_chat_ids || '—' }}</span>
            </div>
            @if (tgBotInfo()) {
              <div class="flex justify-between py-2 border-b border-slate-100">
                <span class="text-slate-500">Bot info</span>
                <span class="font-mono text-xs">{{ tgBotInfo()!['first_name'] || '—' }}</span>
              </div>
            }
          </div>
        }
      </div>

      <!-- Secrets Store (V2C-601) -->
      <div class="bg-white rounded-xl border border-slate-200 p-5">
        <h3 class="text-lg font-semibold text-slate-900 mb-4">Сховище секретів</h3>
        @if (secretsLoading()) {
          <div class="animate-pulse space-y-2"><div class="h-5 bg-slate-200 rounded w-full"></div><div class="h-5 bg-slate-200 rounded w-1/2"></div></div>
        } @else if (secretsError()) {
          <p class="text-red-600">{{ secretsError() }}</p>
        } @else {
          <div class="text-xs text-slate-500 mb-3">Значення ніколи не читаються назад. Показується лише configured/missing.</div>
          @for (group of secretGroups; track group.label) {
            <div class="mb-3">
              <h4 class="text-xs font-medium text-slate-500 uppercase mb-1">{{ group.label }}</h4>
              @for (name of group.secrets; track name) {
                <div class="flex items-center justify-between py-2 border-b border-slate-100">
                  <span class="font-mono text-sm">{{ name }}</span>
                  <div class="flex items-center gap-2">
                    <span class="text-xs" [class.text-emerald-600]="secrets()?.[name]" [class.text-slate-400]="!secrets()?.[name]">
                      {{ secrets()?.[name] ? 'Налаштовано' : 'Відсутнє' }}
                    </span>
                    <button (click)="editSecret(name)" class="text-xs text-emerald-600 hover:text-emerald-700" data-testid="secret-edit-btn">Змінити</button>
                    @if (secrets()?.[name]) {
                      <button (click)="confirmDeleteSecret(name)" class="text-xs text-red-500 hover:text-red-600" data-testid="secret-delete-btn">Видалити</button>
                    }
                  </div>
                </div>
              }
            </div>
          }
          @if (editingSecret()) {
            <div class="mt-3 bg-slate-50 rounded-lg p-3 border border-slate-200" data-testid="secret-form">
              <div class="text-sm font-medium text-slate-700 mb-2">
                {{ secrets()?.[editingSecret()!] ? 'Оновити' : 'Додати' }}: <span class="font-mono">{{ editingSecret() }}</span>
              </div>
              <input type="password" #secretInput placeholder="Значення секрету" class="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500" data-testid="secret-value-input">
              <div class="flex gap-2 mt-2">
                <button (click)="saveSecret(secretInput)" [disabled]="secretSaving()" class="px-3 py-1.5 text-sm bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 disabled:opacity-50" data-testid="secret-save-btn">
                  {{ secretSaving() ? 'Збереження...' : 'Зберегти' }}
                </button>
                <button (click)="cancelSecretEdit()" class="px-3 py-1.5 text-sm text-slate-600 hover:text-slate-800">Скасувати</button>
              </div>
            </div>
          }
        }
      </div>

      <!-- Provider/Models Management (V2-502) -->
      <div class="bg-white rounded-xl border border-slate-200 p-5">
        <h3 class="text-lg font-semibold text-slate-900 mb-4">Моделі (Ollama)</h3>
        @if (modelsLoading()) {
          <div class="animate-pulse"><div class="h-5 bg-slate-200 rounded w-full"></div></div>
        } @else if (modelsError()) {
          <p class="text-red-600">{{ modelsError() }}</p>
        } @else if (models().length === 0) {
          <p class="text-sm text-slate-500">Моделі не знайдено або Ollama недоступний</p>
        } @else {
          <div class="overflow-x-auto">
            <table class="w-full text-sm text-left">
              <thead class="text-xs text-slate-500 uppercase bg-slate-50">
                <tr><th class="px-4 py-2">Назва</th><th class="px-4 py-2">Розмір</th><th class="px-4 py-2">Дії</th></tr>
              </thead>
              <tbody>
                @for (model of models(); track model.name) {
                  <tr class="border-t border-slate-100">
                    <td class="px-4 py-2 font-mono text-xs">{{ model.name }}</td>
                    <td class="px-4 py-2">{{ model.size ? (model.size / 1024 / 1024 | number:'1.1-1') + ' MB' : '—' }}</td>
                    <td class="px-4 py-2">
                      <button (click)="deleteModel(model.name)" class="text-xs text-red-600 hover:text-red-700">Видалити</button>
                    </td>
                  </tr>
                }
              </tbody>
            </table>
          </div>
        }
        <div class="mt-3 flex gap-2">
          <input [(ngModel)]="modelName" placeholder="наприклад, llama3:8b" class="flex-1 px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-sm">
          <button (click)="pullModel()" [disabled]="!modelName || pullingModel()" class="px-4 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 text-sm disabled:opacity-50">{{ pullingModel() ? 'Завантаження...' : 'Pull' }}</button>
        </div>
      </div>

      <!-- Update Center (V2-503) -->
      <div class="bg-white rounded-xl border border-slate-200 p-5">
        <h3 class="text-lg font-semibold text-slate-900 mb-4">Центр оновлень</h3>
        @if (updateLoading()) {
          <div class="animate-pulse space-y-2"><div class="h-5 bg-slate-200 rounded w-full"></div><div class="h-5 bg-slate-200 rounded w-3/4"></div></div>
        } @else if (updateError()) {
          <p class="text-red-600">{{ updateError() }}</p>
        } @else if (updateStatus()) {
          <div class="space-y-4">
            <div class="grid grid-cols-1 sm:grid-cols-2 gap-4 text-sm">
              <div><dt class="text-slate-500">Стан</dt><dd class="font-medium">{{ updateStatus()!.state }}</dd></div>
              <div><dt class="text-slate-500">Поточна версія</dt><dd class="font-medium">{{ updateStatus()!.current_version }}</dd></div>
              <div><dt class="text-slate-500">Доступна версія</dt><dd class="font-medium">{{ updateStatus()!.available_version || '—' }}</dd></div>
              <div><dt class="text-slate-500">Progress</dt><dd class="font-medium">{{ updateStatus()!.progress }}%</dd></div>
              <div><dt class="text-slate-500">Фаза</dt><dd class="font-medium">{{ updateStatus()!.phase || '—' }}</dd></div>
            </div>
            @if (updateStatus()!.log?.length) {
              <pre class="text-xs text-slate-700 bg-slate-50 p-3 rounded overflow-y-auto max-h-32">{{ updateStatus()!.log.join('\n') }}</pre>
            }
            <div class="flex gap-2">
              <button (click)="checkUpdate()" class="px-3 py-1.5 text-sm border border-slate-200 rounded-lg hover:bg-slate-50">Перевірити оновлення</button>
              <button (click)="installUpdate()" [disabled]="updateInstalling()" class="px-3 py-1.5 text-sm bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 disabled:opacity-50">{{ updateInstalling() ? 'Встановлення...' : 'Встановити' }}</button>
              <button (click)="restartSystem()" class="px-3 py-1.5 text-sm border border-slate-200 rounded-lg hover:bg-slate-50">Перезапустити</button>
            </div>
            @if (readiness()) {
              <div class="mt-3 text-xs">
                <div>Active jobs: {{ readiness()!.active_jobs.length }}</div>
                <div>Busy workers: {{ readiness()!.busy_workers.length }}</div>
                <div>Drain acknowledged: {{ readiness()!.acknowledged_workers.length }}</div>
                <div>Unacknowledged: {{ readiness()!.unacknowledged_workers.length }}</div>
                <div>Queue paused: {{ readiness()!.queue_paused ? 'Так' : 'Ні' }}</div>
                @if (readiness()!.drain_operation_id) {
                  <div class="mt-1">Drain operation: <span class="font-mono">{{ readiness()!.drain_operation_id }}</span></div>
                }
              </div>
            }
            @if (rollingStatus()) {
              <div class="mt-3 text-xs">
                <div>Rolling: {{ rollingStatus()!.state || '—' }}</div>
                <div>Batch: {{ rollingStatus()!.current_batch }}/{{ rollingStatus()!.total_batches }}</div>
                <div class="flex gap-2 mt-2">
                  <button (click)="cancelRolling()" class="px-2 py-1 text-xs border border-slate-200 rounded">Скасувати</button>
                  <button (click)="promoteCanary()" class="px-2 py-1 text-xs border border-slate-200 rounded">Promote canary</button>
                  <button (click)="rollbackCanary()" class="px-2 py-1 text-xs border border-slate-200 rounded">Rollback canary</button>
                </div>
              </div>
            }
          </div>
        }
      </div>

      <!-- Backup/Recovery (V2C-604) -->
      <div class="bg-white rounded-xl border border-slate-200 p-5">
        <h3 class="text-lg font-semibold text-slate-900 mb-4">Бекапи та відновлення</h3>
        @if (backupsLoading()) {
          <div class="animate-pulse space-y-2"><div class="h-5 bg-slate-200 rounded w-full"></div></div>
        } @else if (backupsError()) {
          <p class="text-red-600">{{ backupsError() }}</p>
        } @else if (backups().length === 0) {
          <p class="text-sm text-slate-500">Бекапів не знайдено</p>
        } @else {
          <div class="space-y-2">
            @for (backup of backups(); track backup.snapshot_id) {
              <div class="flex items-center justify-between py-2 border-b border-slate-100">
                <div class="flex-1">
                  <span class="font-mono text-xs">{{ backup.snapshot_id }}</span>
                  <span class="text-xs text-slate-500 ml-2">{{ backup.created_at | vertepDate }}</span>
                  @if (backup.description) { <span class="text-xs text-slate-400 ml-2">{{ backup.description }}</span> }
                </div>
                <div class="flex items-center gap-2">
                  @if (backup.size_bytes) { <span class="text-xs text-slate-500">{{ formatBytes(backup.size_bytes) }}</span> }
                  @if (backup.state) {
                    <span class="text-xs px-1.5 py-0.5 rounded" [class.bg-emerald-50]="backup.state === 'ok'" [class.text-emerald-700]="backup.state === 'ok'"
                      [class.bg-red-50]="backup.state === 'error'" [class.text-red-700]="backup.state === 'error'">{{ backup.state }}</span>
                  }
                  <button (click)="confirmRestore(backup.snapshot_id)" class="text-xs text-blue-600 hover:text-blue-700" data-testid="backup-restore">Відновити</button>
                </div>
              </div>
            }
          </div>
        }
        <div class="mt-3 flex gap-2">
          <button (click)="createBackup()" class="px-3 py-1.5 text-sm bg-emerald-600 text-white rounded-lg hover:bg-emerald-700" data-testid="backup-create">Створити бекап</button>
          <button (click)="recoverToNormal()" class="px-3 py-1.5 text-sm border border-slate-200 rounded-lg hover:bg-slate-50">Відновити до NORMAL</button>
        </div>
      </div>

      <!-- General Settings / Integrations (V2-505) -->
      <div class="bg-white rounded-xl border border-slate-200 p-5">
        <h3 class="text-lg font-semibold text-slate-900 mb-4">Інтеграції</h3>
        @if (integrationsLoading()) {
          <div class="animate-pulse space-y-2"><div class="h-5 bg-slate-200 rounded w-full"></div><div class="h-5 bg-slate-200 rounded w-1/2"></div></div>
        } @else if (integrationsError()) {
          <p class="text-red-600">{{ integrationsError() }}</p>
        } @else if (integrations()) {
          <div class="space-y-2">
            <div class="flex justify-between py-2 border-b border-slate-100">
              <span class="text-sm">Ollama</span>
              <span class="text-xs font-medium" [class.text-emerald-600]="integrations()!.ollama.status === 'ONLINE'" [class.text-red-600]="integrations()!.ollama.status !== 'ONLINE'">{{ integrations()!.ollama.status }}</span>
            </div>
            <div class="flex justify-between py-2 border-b border-slate-100">
              <span class="text-sm">ComfyUI</span>
              <span class="text-xs font-medium" [class.text-emerald-600]="integrations()!.comfyui.status === 'ONLINE'" [class.text-red-600]="integrations()!.comfyui.status !== 'ONLINE'">{{ integrations()!.comfyui.status }}</span>
            </div>
          </div>
        }
      </div>

      <!-- Logo (V2C-605) -->
      <div class="bg-white rounded-xl border border-slate-200 p-5">
        <h3 class="text-lg font-semibold text-slate-900 mb-4">Логотип</h3>
        @if (logoError()) { <p role="alert" class="text-sm text-red-600 mb-3">{{ logoError() }}</p> }
        <div class="flex items-center gap-4">
          <div class="w-16 h-16 rounded-lg border border-slate-200 flex items-center justify-center overflow-hidden bg-slate-50">
            @if (logoUrl()) {
              <img [src]="logoUrl()" alt="Logo" class="max-w-full max-h-full object-contain">
            } @else {
              <span class="text-2xl text-slate-300">V</span>
            }
          </div>
          <div class="flex gap-2">
            <label class="px-3 py-1.5 text-sm bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 cursor-pointer">
              {{ logoUploading() ? 'Завантаження...' : 'Завантажити' }}
              <input [disabled]="logoUploading()" type="file" accept="image/*" (change)="uploadLogoFile($event)" class="hidden" data-testid="logo-upload">
            </label>
            @if (logoUrl()) {
              <button (click)="deleteLogoFile()" class="px-3 py-1.5 text-sm text-red-600 hover:text-red-700 border border-red-200 rounded-lg" data-testid="logo-delete">Видалити</button>
            }
          </div>
        </div>
      </div>

      <!-- Security Check (V2C-605) -->
      <div class="bg-white rounded-xl border border-slate-200 p-5">
        <h3 class="text-lg font-semibold text-slate-900 mb-4">Безпека</h3>
        @if (secLoading()) {
          <p class="text-sm text-slate-500">Завантаження...</p>
        } @else if (secCheck()) {
          <div class="space-y-2">
            <div class="flex justify-between py-2 border-b border-slate-100">
              <span class="text-sm">Статус</span>
              <span class="text-sm font-medium" [class.text-emerald-600]="secCheck()!.ok" [class.text-red-600]="!secCheck()!.ok">{{ secCheck()!.ok ? 'OK' : 'Увага' }}</span>
            </div>
            @if (secCheck()!.weak_or_missing.length) {
              <div class="text-sm text-slate-700">
                <p class="font-medium mb-1">Слабкі/відсутні:</p>
                <ul class="list-disc pl-5 space-y-0.5">
                  @for (item of secCheck()!.weak_or_missing; track item) {
                    <li class="text-xs text-slate-600">{{ item }}</li>
                  }
                </ul>
              </div>
            }
            <p class="text-xs text-slate-500">{{ secCheck()!.recommendation }}</p>
          </div>
        }
      </div>

      <!-- Certificates -->
      <div class="bg-white rounded-xl border border-slate-200 p-5">
        <h3 class="text-lg font-semibold text-slate-900 mb-4">Сертифікати</h3>
        @if (certLoading()) {
          <p class="text-sm text-slate-500">Завантаження...</p>
        } @else if (certError()) {
          <p class="text-red-600">{{ certError() }}</p>
        } @else if (certificates()) {
          <pre class="text-xs text-slate-700 bg-slate-50 p-3 rounded overflow-x-auto max-h-48">{{ certificates() | json }}</pre>
          <button (click)="renewCertificate()" class="mt-2 px-3 py-1.5 text-sm bg-emerald-600 text-white rounded-lg hover:bg-emerald-700">Оновити сертифікат</button>
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
  private subs = new Subscription();
  allRoles: SystemRole[] = [];
  rolesResponse: SystemRolesResponse | null = null;
  rolesLoading = signal(false);
  rolesError = signal<string | null>(null);
  savingRoles = signal(false);
  rolesSaveMessage = signal<string | null>(null);
  rolesSaveState = signal<string | null>(null);
  selectedRoles: string[] = [];
  tgStatus = signal<TelegramStatus | null>(null);
  tgBotInfo = signal<TelegramBotInfo | null>(null);
  tgLoading = signal(false);
  tgError = signal<string | null>(null);

  secrets = signal<Record<string, boolean> | null>(null);
  secretsLoading = signal(false);
  secretsError = signal<string | null>(null);
  editingSecret = signal<string | null>(null);
  secretSaving = signal(false);

  readonly secretGroups: SecretGroup[] = [
    { label: 'Telegram', secrets: ['telegram_bot_token'] },
    { label: 'YouTube', secrets: ['youtube_client_secret'] },
    { label: 'Facebook', secrets: ['facebook_access_token'] },
    { label: 'TikTok', secrets: ['tiktok_client_secret'] },
    { label: 'Email (SMTP)', secrets: ['smtp_password'] },
    { label: 'Зовнішній AI', secrets: ['external_ai_api_key'] },
  ];

  models = signal<ModelInfo[]>([]);
  modelsLoading = signal(false);
  modelsError = signal<string | null>(null);
  modelName = '';
  pullingModel = signal(false);

  updateStatus = signal<UpdateStatus | null>(null);
  updateLoading = signal(false);
  updateError = signal<string | null>(null);
  updateInstalling = signal(false);
  readiness = signal<UpdateReadiness | null>(null);
  rollingStatus = signal<RollingStatus | null>(null);

  backups = signal<BackupInfo[]>([]);
  backupsLoading = signal(false);
  backupsError = signal<string | null>(null);

  integrations = signal<IntegrationStatus | null>(null);
  integrationsLoading = signal(false);
  integrationsError = signal<string | null>(null);

  certificates = signal<Record<string, unknown> | null>(null);
  certLoading = signal(false);
  certError = signal<string | null>(null);

  logoUrl = signal<string | null>(null);
  logoUploading = signal(false);
  logoError = signal<string | null>(null);
  secCheck = signal<{ ok: boolean; weak_or_missing: string[]; recommendation: string } | null>(null);
  secLoading = signal(false);

  constructor(private api: VertepApiService, private toast: ToastService, private confirm: ConfirmService) {}

  ngOnInit(): void {
    this.loadStatus();
    this.loadRoles();
    this.loadTelegram();
    this.loadSecrets();
    this.loadModels();
    this.loadUpdateStatus();
    this.loadBackups();
    this.loadIntegrations();
    this.loadCertificates();
    this.loadLogo();
    this.loadSecurityCheck();
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

  backendSlots(): Array<{ slot: string; label: string; backend: string; configured: boolean; options: string; env: string }> {
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
    this.updateLoading.set(true);
    this.updateError.set(null);
    this.api.checkUpdate().subscribe({
      next: (status) => { this.updateStatus.set(status); this.updateLoading.set(false); },
      error: (err) => { this.updateError.set(err.message); this.updateLoading.set(false); },
    });
  }

  installUpdate(): void {
    this.updateInstalling.set(true);
    this.api.installUpdate().subscribe({
      next: (status) => { this.updateStatus.set(status); this.updateInstalling.set(false); },
      error: (err) => { this.updateError.set(err.message); this.updateInstalling.set(false); },
    });
  }

  restartSystem(): void {
    this.api.restartSystem().subscribe({
      next: () => this.toast.show('Систему перезапущено', 'success'),
      error: (err) => this.updateError.set(err.message),
    });
  }

  loadSecrets(): void {
    this.secretsLoading.set(true);
    this.secretsError.set(null);
    this.api.getSecrets().subscribe({
      next: (status) => { this.secrets.set(status.secrets); this.secretsLoading.set(false); },
      error: (err) => { this.secretsError.set(err.message); this.secretsLoading.set(false); },
    });
  }

  editSecret(name: string): void {
    this.editingSecret.set(name);
  }

  cancelSecretEdit(): void {
    this.editingSecret.set(null);
  }

  saveSecret(input: HTMLInputElement): void {
    const name = this.editingSecret();
    const value = input.value;
    if (!name || !value) return;
    this.secretSaving.set(true);
    this.api.updateSecret(name, value).subscribe({
      next: () => {
        this.secretSaving.set(false);
        this.editingSecret.set(null);
        this.toast.show('Секрет збережено', 'success');
        this.loadSecrets();
      },
      error: (err) => {
        this.secretSaving.set(false);
        this.secretsError.set(err.message);
      },
    });
  }

  confirmDeleteSecret(name: string): void {
    this.confirm.confirm({
      title: 'Видалити секрет',
      message: `Ви впевнені, що хочете видалити ${name}? Ця дія необоротна.`,
    }).subscribe((ok) => {
      if (!ok) return;
      this.api.deleteSecret(name).subscribe({
        next: () => {
          this.toast.show('Секрет видалено', 'success');
          this.loadSecrets();
        },
        error: (err) => this.secretsError.set(err.message),
      });
    });
  }

  loadModels(): void {
    this.modelsLoading.set(true);
    this.modelsError.set(null);
    this.api.getModels().subscribe({
      next: (data) => { this.models.set((data['models'] as ModelInfo[]) || []); this.modelsLoading.set(false); },
      error: (err) => { this.modelsError.set(err.message); this.modelsLoading.set(false); },
    });
  }

  pullModel(): void {
    if (!this.modelName || this.pullingModel()) return;
    this.pullingModel.set(true);
    this.api.pullModel(this.modelName).subscribe({
      next: () => { this.pullingModel.set(false); this.loadModels(); },
      error: () => this.pullingModel.set(false),
    });
  }

  deleteModel(name: string): void {
    if (!confirm(`Видалити модель ${name}?`)) return;
    this.api.deleteModel(name).subscribe({
      next: () => this.loadModels(),
      error: (err) => this.modelsError.set(err.message),
    });
  }

  loadUpdateStatus(): void {
    this.updateLoading.set(true);
    this.updateError.set(null);
    this.api.getUpdateStatus().subscribe({
      next: (status) => {
        this.updateStatus.set(status);
        this.updateLoading.set(false);
        this.loadReadiness();
        this.loadRollingStatus();
      },
      error: (err) => { this.updateError.set(err.message); this.updateLoading.set(false); },
    });
  }

  loadReadiness(): void {
    this.api.getUpdateReadiness().subscribe({
      next: (r) => this.readiness.set(r),
      error: () => {},
    });
  }

  loadRollingStatus(): void {
    this.api.getRollingStatus().subscribe({
      next: (s) => this.rollingStatus.set(s),
      error: () => {},
    });
  }

  cancelRolling(): void {
    this.api.cancelRolling().subscribe({
      next: () => this.loadRollingStatus(),
      error: (err) => this.updateError.set(err.message),
    });
  }

  promoteCanary(): void {
    this.api.promoteCanary().subscribe({
      next: () => this.loadRollingStatus(),
      error: (err) => this.updateError.set(err.message),
    });
  }

  rollbackCanary(): void {
    this.api.rollbackCanary().subscribe({
      next: () => this.loadRollingStatus(),
      error: (err) => this.updateError.set(err.message),
    });
  }

  loadBackups(): void {
    this.backupsLoading.set(true);
    this.backupsError.set(null);
    this.api.getBackups().subscribe({
      next: (data) => { this.backups.set((data['snapshots'] as BackupInfo[]) || []); this.backupsLoading.set(false); },
      error: (err) => { this.backupsError.set(err.message); this.backupsLoading.set(false); },
    });
  }

  createBackup(): void {
    this.api.createBackup().subscribe({
      next: () => this.loadBackups(),
      error: (err) => this.backupsError.set(err.message),
    });
  }

  restoreBackup(id: string): void {
    if (!confirm(`Відновити бекап ${id}? Ця дія необоротна.`)) return;
    this.api.restoreBackup(id).subscribe({
      next: () => this.loadBackups(),
      error: (err) => this.backupsError.set(err.message),
    });
  }

  recoverToNormal(): void {
    this.api.recoverToNormal().subscribe({
      next: () => this.loadUpdateStatus(),
      error: (err) => this.updateError.set(err.message),
    });
  }

  loadIntegrations(): void {
    this.integrationsLoading.set(true);
    this.integrationsError.set(null);
    this.api.getIntegrations().subscribe({
      next: (status) => { this.integrations.set(status as unknown as IntegrationStatus); this.integrationsLoading.set(false); },
      error: (err) => { this.integrationsError.set(err.message); this.integrationsLoading.set(false); },
    });
  }

  loadCertificates(): void {
    this.certLoading.set(true);
    this.certError.set(null);
    this.api.getCertificates().subscribe({
      next: (certs) => { this.certificates.set(certs); this.certLoading.set(false); },
      error: (err) => { this.certError.set(err.message); this.certLoading.set(false); },
    });
  }

  renewCertificate(): void {
    this.api.renewCertificate().subscribe({
      next: () => this.loadCertificates(),
      error: (err) => this.certError.set(err.message),
    });
  }

  loadRoles(): void {
    this.rolesLoading.set(true);
    this.rolesError.set(null);
    this.api.getSystemRoles().subscribe({
      next: (resp) => {
        this.rolesResponse = resp;
        this.allRoles = resp.available_roles || [];
        this.selectedRoles = resp.active_roles || [];
        this.rolesLoading.set(false);
      },
      error: (err) => { this.rolesError.set(err.message); this.rolesLoading.set(false); },
    });
  }

  toggleRole(id: string, event: Event): void {
    const checked = (event.target as HTMLInputElement).checked;
    if (checked) {
      if (!this.selectedRoles.includes(id)) this.selectedRoles.push(id);
    } else {
      this.selectedRoles = this.selectedRoles.filter(r => r !== id);
    }
  }

  saveRoles(): void {
    this.savingRoles.set(true);
    this.rolesSaveMessage.set(null);
    this.rolesSaveState.set(null);
    this.api.updateSystemRoles(this.selectedRoles).subscribe({
      next: (resp) => {
        this.savingRoles.set(false);
        this.rolesSaveState.set(resp.state);
        this.rolesSaveMessage.set(resp.message || null);
        if (resp.state === 'QUEUED') {
          this.pollDeploymentProgress();
        } else {
          this.loadRoles();
        }
      },
      error: (err) => {
        this.savingRoles.set(false);
        this.rolesSaveMessage.set(err.message || 'Помилка збереження');
        this.rolesSaveState.set('ERROR');
      },
    });
  }

  private pollDeploymentProgress(attempts = 0): void {
    const maxAttempts = 30;
    if (attempts >= maxAttempts) {
      this.loadRoles();
      return;
    }
    setTimeout(() => {
      this.api.getSystemRoles().subscribe({
        next: (resp) => {
          this.rolesResponse = resp;
          const state = resp.deployment?.state;
          this.rolesSaveState.set(state || resp.queued ? 'QUEUED' : 'IDLE');
          if (state === 'APPLYING' || resp.queued) {
            this.pollDeploymentProgress(attempts + 1);
          } else {
            this.loadRoles();
          }
        },
        error: () => this.pollDeploymentProgress(attempts + 1),
      });
    }, 5000);
  }

  loadLogo(): void {
    this.api.getLogo().subscribe({
      next: (blob) => {
        const url = URL.createObjectURL(blob);
        this.logoUrl.set(url);
      },
      error: () => this.logoUrl.set(null),
    });
  }

  uploadLogoFile(event: Event): void {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file || this.logoUploading()) return;
    input.value = '';
    this.logoError.set(null);
    if (!file.type.startsWith('image/') || file.size > 2 * 1024 * 1024 || !file.size) {
      this.logoError.set('Виберіть непорожнє зображення розміром до 2 МБ.');
      return;
    }
    this.logoUploading.set(true);
    this.api.uploadLogo(file).subscribe({
      next: () => {
        this.logoUploading.set(false);
        this.toast.show('Логотип завантажено', 'success');
        this.loadLogo();
      },
      error: (err) => {
        this.logoUploading.set(false);
        this.logoError.set(err.message || 'Не вдалося завантажити логотип');
      },
    });
  }

  deleteLogoFile(): void {
    this.confirm.confirm({ title: 'Видалити логотип', message: 'Видалити логотип дашборду?' }).subscribe((ok) => {
      if (!ok) return;
      this.api.deleteLogo().subscribe({
        next: () => { this.toast.show('Логотип видалено', 'success'); this.logoUrl.set(null); },
        error: (err) => this.toast.show(err.message || 'Помилка видалення', 'error'),
      });
    });
  }

  loadSecurityCheck(): void {
    this.secLoading.set(true);
    this.api.getSecurityCheck().subscribe({
      next: (check) => { this.secCheck.set(check); this.secLoading.set(false); },
      error: () => this.secLoading.set(false),
    });
  }

  loadTelegram(): void {
    this.tgLoading.set(true);
    this.tgError.set(null);
    this.api.getTelegramStatus().subscribe({
      next: (status) => { this.tgStatus.set(status); this.tgLoading.set(false); },
      error: (err) => { this.tgError.set(err.message); this.tgLoading.set(false); },
    });
    this.api.getTelegramBotInfo().subscribe({
      next: (info) => this.tgBotInfo.set(info),
      error: () => {},
    });
  }

  confirmRestore(snapshotId: string): void {
    this.confirm.confirm({ title: 'Відновити бекап', message: `Відновити snapshot ${snapshotId}? Поточна конфігурація буде замінена.` }).subscribe((ok) => {
      if (!ok) return;
      this.api.restoreBackup(snapshotId).subscribe({
        next: () => { this.toast.show('Бекап відновлено', 'success'); this.loadUpdateStatus(); },
        error: (err) => this.toast.show(err.message || 'Помилка відновлення', 'error'),
      });
    });
  }

  formatBytes(bytes: number): string {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return (bytes / Math.pow(k, i)).toFixed(1) + ' ' + sizes[i];
  }
}
