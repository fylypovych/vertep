import { TPipe } from '../core/i18n/t.pipe';
import { Component, computed, OnDestroy, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { SetupCompleteResult, SetupStatus } from '../core/models';

/** Section order in the template: the health check is the last step before completion. */
const HEALTH_STEP = 7;
const MANIFEST_STEP = 8;
const BACKUP_KEY_STEP = 9;
const LAST_STEP = 10;

type RoleEntry = { id: string; label: string; modules: string[]; capabilities: string[] };
const HEALTH_NAMES: Record<string, string> = {
  docker: 'Docker', postgres: 'PostgreSQL', redis: 'Redis', core_api: 'API CORE',
  ollama: 'Ollama', monitoring: 'Моніторинг', gpu: 'Відеокарта', cuda: 'Обчислення GPU',
  comfyui: 'ComfyUI', gpu_worker: 'GPU Worker', tts: 'Синтез мовлення',
  publisher: 'Публікація', backup: 'Резервні копії',
};
const HEALTH_MESSAGES: Record<string, string> = {
  health_observation_expired: 'Результат host-перевірки застарів або має некоректний час.',
  health_observation_unavailable: 'Немає коректного результату host-перевірки. Перевірте host probe.',
  health_probe_failed: 'Перевірка залежності завершилася помилкою.',
  health_docker_unavailable: 'Docker daemon недоступний.',
  health_endpoint_unavailable: 'Вибрана служба недоступна. Перевірте її запуск і мережу.',
  health_postgres_unavailable: 'PostgreSQL не відповідає на перевірочний запит.',
  health_redis_unavailable: 'Redis не відповідає на PING.',
  health_gpu_unavailable: 'GPU недоступний у середовищі виконання.',
  health_gpu_runtime_failed: 'Обчислення GPU завершилося помилкою.',
  health_worker_not_ready: 'GPU Worker не має актуальної реєстрації та успішного self-test.',
};

@Component({
  selector: 'app-setup',
  standalone: true,
  imports: [TPipe, CommonModule, FormsModule],
  templateUrl: './setup.component.html',
})
export class SetupComponent implements OnInit, OnDestroy {
  readonly steps = Array.from({ length: LAST_STEP + 1 }, (_, index) => index);
  readonly healthStep = HEALTH_STEP;
  readonly manifestStep = MANIFEST_STEP;
  readonly backupKeyStep = BACKUP_KEY_STEP;
  readonly lastStep = LAST_STEP;

  step = signal(0);
  error = signal<string | null>(null);
  askCode = signal(false);
  roleLocked = signal(false);
  role = signal('core');
  roles = signal<RoleEntry[]>([]);
  additionalRoles = signal<string[]>([]);
  additionalRoleOptions = computed(() => this.roles().filter(entry => entry.id !== 'core'));
  hardware = signal<Record<string, unknown> | null>(null);
  hardwareRows = computed(() => {
    const hardware = this.hardware();
    if (!hardware) return [];
    const gpu = hardware['gpu'] as Record<string, unknown> | null;
    const value = (item: unknown) => typeof item === 'string' && item.trim() ? item
      : typeof item === 'number' && Number.isFinite(item) ? String(item) : 'Не визначено';
    const memory = (item: unknown) => typeof item === 'number' && Number.isFinite(item) && item > 0
      ? `${Math.round(item / 1024 * 10) / 10}` : null;
    const rows = [
      { label: 'Архітектура системи', value: value(hardware['architecture']), unit: '' },
      { label: 'Логічні процесори', value: value(hardware['cpu_count']), unit: '' },
      { label: 'Оперативна пам’ять', value: memory(hardware['ram_mb']) || 'Не визначено', unit: memory(hardware['ram_mb']) ? 'ГБ' : '' },
      { label: 'Відеокарта', value: gpu?.['vendor'] === 'none' ? 'Не виявлено' : value(gpu?.['name']), unit: '' },
    ];
    if (gpu && gpu['vendor'] !== 'none') {
      rows.push({ label: 'Відеопам’ять', value: memory(gpu['vram_mb']) || 'Не визначено', unit: memory(gpu['vram_mb']) ? 'ГБ' : '' });
      if (gpu['driver']) rows.push({ label: 'Драйвер GPU', value: value(gpu['driver']), unit: '' });
      if (gpu['cuda']) rows.push({ label: 'CUDA', value: value(gpu['cuda']), unit: '' });
    }
    if (hardware['docker_version']) rows.push({ label: 'Docker', value: value(hardware['docker_version']).replace(/^Docker version\s+/i, ''), unit: '' });
    return rows;
  });
  healthText = signal('Core … OK\nAPI … OK\nWeb UI … OK\nPostgreSQL … перевіряється\nRedis … перевіряється\nWorker/GPU … згідно з конфігурацією');
  healthReady = signal(false);
  healthRows = signal<{ name: string; status: string; required: boolean; code: string }[]>([]);
  private healthTimer?: ReturnType<typeof setInterval>;
  private preparingHealth = false;
  private loadingHealth = false;
  private selectionPrepared = false;
  submitting = signal(false);
  result = signal<(SetupCompleteResult & Record<string, unknown>) | null>(null);
  keyDownloaded = signal(false);
  keySaved = signal(false);

  selectedRole = computed(() => this.roles().find(entry => entry.id === this.role()));
  manifestRows = computed(() => {
    const done = this.result();
    if (!done) return [];
    const value = (item: unknown) => typeof item === 'string' && item.trim() ? item : 'Не визначено';
    const backend = done['ai_backend'] as Record<string, unknown> | null;
    const role = this.roles().find(entry => entry.id === done['node_role']);
    const created = typeof done['created_at'] === 'string' ? new Date(done['created_at']) : null;
    const rows = [
      { label: 'Назва системи', value: value(done['installation_name']) },
      { label: 'Версія', value: value(done['version']) },
      { label: 'Роль вузла', value: role?.label || value(done['node_role']) },
      { label: 'Адреса CORE', value: value(done.core_url) },
      { label: 'Домен', value: typeof done['web_domain'] === 'string' && done['web_domain'].trim() ? done['web_domain'] : 'Не налаштовано' },
      { label: 'AI Backend', value: backend?.['type'] === 'skip' ? 'Пропустити' : value(backend?.['type']) },
    ];
    if (backend?.['model']) rows.push({ label: 'Модель', value: value(backend['model']) });
    if (backend?.['url']) rows.push({ label: 'URL API', value: value(backend['url']) });
    rows.push({ label: 'Ідентифікатор інсталяції', value: done.installation_id });
    if (created && !Number.isNaN(created.getTime())) rows.push({ label: 'Дата створення', value: created.toLocaleString('uk-UA') });
    return rows;
  });
  resultRows = computed(() => {
    const done = this.result() as Record<string, unknown> | null;
    if (!done) return [];
    const rows = [{ label: 'Ідентифікатор інсталяції', value: String(done['installation_id'] || 'Не визначено') }];
    if (done['core_url']) rows.push({ label: 'Адреса CORE', value: String(done['core_url']) });
    if (done['web_domain']) rows.push({ label: 'Домен', value: String(done['web_domain']) });
    const token = done['registration_token'];
    if (typeof token === 'string' && token) {
      rows.push({ label: 'Одноразовий токен підключення', value: token });
    } else if (token && typeof token === 'object') {
      const registration = token as Record<string, unknown>;
      if (typeof registration['token'] === 'string') rows.push({ label: 'Одноразовий токен підключення', value: registration['token'] });
      if (typeof registration['role'] === 'string') {
        rows.push({ label: 'Роль вузла для підключення', value: this.roles().find(role => role.id === registration['role'])?.label || registration['role'] });
      }
      if (typeof registration['expires_at'] === 'string') {
        const expires = new Date(registration['expires_at']);
        if (!Number.isNaN(expires.getTime())) rows.push({ label: 'Токен дійсний до', value: expires.toLocaleString('uk-UA') });
      }
    }
    return rows;
  });
  nextLabel = computed(() => this.step() === LAST_STEP ? 'Відкрити Dashboard'
    : this.step() === HEALTH_STEP && !this.result() ? 'Завершити налаштування' : 'Далі');
  nextDisabled = computed(() => {
    if (this.submitting()) return true;
    if (this.step() === HEALTH_STEP && !this.result()) return !this.healthReady();
    return this.step() === BACKUP_KEY_STEP && !!this.result()?.['backup_key'] && !this.keySaved();
  });

  form = {
    name: 'Vertep Production', domain: '', core_url: '', core_certificate: '', registration_token: '',
    username: '', password: '', confirmation: '', backend: 'ollama', backend_url: '',
    backend_model: 'llama3.2', backend_api_key: '', telegram_token: '', telegram_admins: '', telegram_allowed: '',
  };
  code = '';
  private token = new URLSearchParams(window.location.search).get('token') || '';

  ngOnInit(): void {
    this.healthTimer = setInterval(() => {
      if (this.step() === HEALTH_STEP && !this.result() && !this.submitting() && !this.preparingHealth && this.selectionPrepared) void this.loadHealth();
    }, 5000);
    fetch('/api/setup', { headers: this.headers() }).then(async response => {
      if (!response.ok) throw new Error((await response.text()) || `Помилка запиту (${response.status})`);
      return response.json() as Promise<SetupStatus>;
    }).then(status => {
      if (status.configured) { window.location.href = '/'; return; }
      const entries = Object.entries(status.roles).map(([id, role]) => ({ id, ...role }));
      this.roles.set(entries);
      this.additionalRoles.set(status.selected_additional_roles || []);
      if (status.selected_role && status.roles[status.selected_role]) {
        this.role.set(status.selected_role);
        this.roleLocked.set(true);
      } else if (entries.length && !status.roles[this.role()]) {
        this.role.set(entries[0].id);
      }
      this.hardware.set(status.hardware || {});
    }).catch(error => {
      if (this.token) this.error.set(`Не вдалося відкрити майстер: ${error.message}`);
      else this.askCode.set(true);
    });
  }

  ngOnDestroy(): void { clearInterval(this.healthTimer); }

  useCode(): void {
    const target = new URL(window.location.href);
    target.searchParams.set('token', this.code.trim());
    window.location.replace(target);
  }

  back(): void { this.error.set(null); this.go(this.step() - 1); }

  next(): void {
    this.error.set(null);
    if (this.step() === 2 && (this.form.password !== this.form.confirmation || this.form.password.length < 12)) {
      this.error.set('Паролі мають збігатися та містити щонайменше 12 символів');
      return;
    }
    if (this.step() === HEALTH_STEP && !this.result()) { void this.complete(); return; }
    if (this.step() === LAST_STEP) { window.location.href = '/'; return; }
    this.go(this.step() + 1);
  }

  downloadManifest(): void {
    const done = this.result();
    if (!done) return;
    const { backup_key: _omitted, ...manifest } = done;
    this.save(JSON.stringify(manifest, null, 2), 'manifest.json', 'application/json');
  }

  downloadKey(): void {
    const key = this.result()?.['backup_key'];
    if (typeof key !== 'string') return;
    this.save(key, 'vertep-backup.key', 'text/plain');
    this.keyDownloaded.set(true);
  }

  private go(step: number): void {
    this.step.set(step);
    if (step === HEALTH_STEP && !this.result()) void this.prepareHealth();
  }

  toggleAdditionalRole(id: string, enabled: boolean): void {
    this.additionalRoles.update(roles => enabled ? [...new Set([...roles, id])] : roles.filter(role => role !== id));
    this.selectionPrepared = false;
    this.healthReady.set(false);
  }

  async prepareHealth(): Promise<void> {
    if (this.preparingHealth) return;
    this.preparingHealth = true;
    this.selectionPrepared = false;
    this.healthReady.set(false);
    this.healthRows.set([]);
    this.healthText.set('Перевірка…');
    try {
      const form = this.form;
      const response = await fetch('/api/setup/selection', {
        signal: AbortSignal.timeout(15000),
        method: 'POST', headers: { 'Content-Type': 'application/json', ...this.headers() },
        body: JSON.stringify({ node_role: this.role(), ai_backend: form.backend,
          additional_roles: this.role() === 'core' ? this.additionalRoles() : [],
          backend_url: form.backend_url || null, backend_model: form.backend_model,
          core_url: form.core_url || null }),
      });
      if (!response.ok) throw new Error('Не вдалося підготувати вибрані служби');
      this.selectionPrepared = true;
      await this.loadHealth();
    } catch {
      this.healthRows.set([]);
      this.healthText.set('Не вдалося підготувати вибрані служби');
    } finally {
      this.preparingHealth = false;
    }
  }

  private async loadHealth(): Promise<void> {
    if (this.loadingHealth) return;
    this.loadingHealth = true;
    try {
      const extras = this.role() === 'core' ? this.additionalRoles().join(',') : '';
      const selection = new URLSearchParams({ role: this.role(), backend: this.form.backend, additional_roles: extras });
      const response = await fetch(`/api/setup/health?${selection}`, { headers: this.headers(), signal: AbortSignal.timeout(15000) });
      if (!response.ok) throw new Error('Перевірка недоступна');
      const health = await response.json();
      if (this.step() !== HEALTH_STEP || selection.get('role') !== this.role() || selection.get('backend') !== this.form.backend
        || extras !== (this.role() === 'core' ? this.additionalRoles().join(',') : '')) return;
      this.healthRows.set(Object.entries(health.checks as Record<string, string>).map(([name, status]) => ({
        name: HEALTH_NAMES[name] || name, status: status === 'OK' ? 'Готово' : status === 'OPTIONAL' ? 'Не застосовується' : 'Помилка',
        required: (health.required || []).includes(name),
        code: HEALTH_MESSAGES[health.details?.[name]?.code] || 'Перевірка залежності завершилася помилкою.',
      })));
      this.healthText.set(Object.entries(health.checks).map(([name, value]) => `${name} … ${value}`).join('\n'));
      this.healthReady.set(!!health.ready);
    } catch (error) {
      this.healthReady.set(false);
      this.healthRows.set([]);
      this.healthText.set((error as Error).message);
    } finally {
      this.loadingHealth = false;
    }
  }

  private async complete(): Promise<void> {
    this.submitting.set(true);
    const form = this.form;
    try {
      const response = await fetch('/api/setup/complete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...this.headers() },
        body: JSON.stringify({
          node_role: this.role(), installation_name: form.name, web_domain: form.domain || null,
          core_url: form.core_url || null, core_certificate: form.core_certificate || null,
          registration_token: form.registration_token || null, username: form.username,
          password: form.password, password_confirmation: form.confirmation, ai_backend: form.backend,
          backend_url: form.backend_url || null, backend_model: form.backend_model || null,
          backend_api_key: form.backend_api_key || null, telegram_bot_token: form.telegram_token || null,
          telegram_admin_chat_ids: form.telegram_admins || null, telegram_allowed_chat_ids: form.telegram_allowed || null,
        }),
      });
      if (!response.ok) {
        const failure = await response.json().catch(() => ({ detail: response.statusText }));
        this.error.set(typeof failure.detail === 'string' ? failure.detail : JSON.stringify(failure.detail));
        return;
      }
      this.result.set(await response.json());
      this.step.set(MANIFEST_STEP);
    } catch (error) {
      this.error.set((error as Error).message);
    } finally {
      this.submitting.set(false);
    }
  }

  private headers(): Record<string, string> { return { 'X-Vertep-Setup-Token': this.token }; }

  private save(content: string, filename: string, type: string): void {
    const url = URL.createObjectURL(new Blob([content], { type }));
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    link.click();
    URL.revokeObjectURL(url);
  }
}
