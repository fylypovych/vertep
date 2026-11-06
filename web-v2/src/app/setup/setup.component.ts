import { TPipe } from '../core/i18n/t.pipe';
import { Component, computed, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { SetupCompleteResult, SetupStatus } from '../core/models';

/** Section order in the template: the health check is the last step before completion. */
const HEALTH_STEP = 7;
const MANIFEST_STEP = 8;
const BACKUP_KEY_STEP = 9;
const LAST_STEP = 10;

type RoleEntry = { id: string; label: string; modules: string[]; capabilities: string[] };

@Component({
  selector: 'app-setup',
  standalone: true,
  imports: [TPipe, CommonModule, FormsModule],
  templateUrl: './setup.component.html',
})
export class SetupComponent implements OnInit {
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
  hardware = signal('Виявлення…');
  healthText = signal('Core … OK\nAPI … OK\nWeb UI … OK\nPostgreSQL … перевіряється\nRedis … перевіряється\nWorker/GPU … згідно з конфігурацією');
  healthReady = signal(false);
  submitting = signal(false);
  result = signal<(SetupCompleteResult & Record<string, unknown>) | null>(null);
  keyDownloaded = signal(false);
  keySaved = signal(false);

  selectedRole = computed(() => this.roles().find(entry => entry.id === this.role()));
  manifestText = computed(() => {
    const done = this.result();
    if (!done) return 'Маніфест буде створено після перевірки налаштувань.';
    const { installation_id, installation_name, version, node_role, modules, hardware, ai_backend,
            core_url, created_at, manifest_sha256, web_domain } = done as Record<string, unknown>;
    return JSON.stringify({ installation_id, installation_name, version, node_role, modules, hardware,
                            ai_backend, core_url, created_at, manifest_sha256, web_domain }, null, 2);
  });
  resultText = computed(() => {
    const done = this.result() as Record<string, unknown> | null;
    if (!done) return '';
    const { installation_id, core_url, registration_token, web_domain } = done;
    return JSON.stringify({ installation_id, core_url, registration_token, web_domain }, null, 2);
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
    fetch('/api/setup', { headers: this.headers() }).then(async response => {
      if (!response.ok) throw new Error((await response.text()) || `Помилка запиту (${response.status})`);
      return response.json() as Promise<SetupStatus>;
    }).then(status => {
      if (status.configured) { window.location.href = '/'; return; }
      const entries = Object.entries(status.roles).map(([id, role]) => ({ id, ...role }));
      this.roles.set(entries);
      if (status.selected_role && status.roles[status.selected_role]) {
        this.role.set(status.selected_role);
        this.roleLocked.set(true);
      } else if (entries.length && !status.roles[this.role()]) {
        this.role.set(entries[0].id);
      }
      this.hardware.set(JSON.stringify(status.hardware, null, 2));
    }).catch(error => {
      if (this.token) this.error.set(`Не вдалося відкрити майстер: ${error.message}`);
      else this.askCode.set(true);
    });
  }

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
    if (step === HEALTH_STEP && !this.result()) void this.loadHealth();
  }

  private async loadHealth(): Promise<void> {
    this.healthReady.set(false);
    this.healthText.set('Перевірка…');
    try {
      const response = await fetch(`/api/setup/health?role=${encodeURIComponent(this.role())}`, { headers: this.headers() });
      if (!response.ok) throw new Error('Перевірка недоступна');
      const health = await response.json();
      this.healthText.set(Object.entries(health.checks).map(([name, value]) => `${name} … ${value}`).join('\n'));
      this.healthReady.set(!!health.ready);
    } catch (error) {
      this.healthText.set((error as Error).message);
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
