import { Component, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { VertepApiService } from '../core/api.service';
import { SetupStatus, SetupHealth, SetupCompleteResult } from '../core/models';

@Component({
  selector: 'app-setup',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './setup.component.html',
})
export class SetupComponent {
  loading = signal(true);
  error = signal<string | null>(null);
  step = signal(0);
  status = signal<SetupStatus | null>(null);
  health = signal<SetupHealth | null>(null);
  healthLoading = signal(false);
  completing = signal(false);
  completeResult = signal<SetupCompleteResult | null>(null);
  setupToken = "";
  hardwareJson = "";
  form = { role: "core", core_url: "", core_certificate: "", registration_token: "", installation_name: "", username: "", password: "", password_confirmation: "", backend: "ollama", backend_model: "", backend_api_key: "" };
  roleEntries: { id: string; label: string; modules: string[]; capabilities: string[] }[] = [];

  constructor(private api: VertepApiService, private router: Router) {}

  ngOnInit(): void {
    this.setupToken = new URLSearchParams(window.location.search).get("token") || "";
    if (!this.setupToken) { this.error.set("Vidsutniy setup token."); this.loading.set(false); return; }
    this.api.getSetupStatus(this.setupToken).subscribe({
      next: (s) => {
        if (s.configured) { this.router.navigate(["/"]); return; }
        this.status.set(s);
        this.roleEntries = Object.entries(s.roles).map(([id, r]) => ({ id, ...r }));
        if (this.roleEntries.length) this.form.role = this.roleEntries[0].id;
        if (s.selected_role) this.form.role = s.selected_role;
        this.hardwareJson = JSON.stringify(s.hardware, null, 2);
        this.loading.set(false);
      },
      error: (err) => { this.error.set(err.message); this.loading.set(false); },
    });
  }

  get selectedRoleDef() { return this.roleEntries.find(r => r.id === this.form.role); }
  get healthEntries() { return this.health() ? Object.entries(this.health()!.checks).map(([k, v]) => ({ key: k, value: v })) : []; }

  nextStep(): void { if (this.step() === 3) this.loadHealth(); this.step.update(s => s + 1); }
  prevStep(): void { this.step.update(s => Math.max(0, s - 1)); }

  loadHealth(): void {
    this.healthLoading.set(true);
    this.api.getSetupHealth(this.setupToken).subscribe({
      next: (h) => { this.health.set(h); this.healthLoading.set(false); },
      error: () => this.healthLoading.set(false),
    });
  }

  complete(): void {
    this.completing.set(true);
    const p: Record<string, unknown> = { node_role: this.form.role, installation_name: this.form.installation_name, username: this.form.username, password: this.form.password, password_confirmation: this.form.password_confirmation, backend: this.form.backend, backend_model: this.form.backend_model || null, backend_api_key: this.form.backend_api_key || null };
    if (this.form.role !== "core") { p["core_url"] = this.form.core_url; p["core_certificate"] = this.form.core_certificate || null; p["registration_token"] = this.form.registration_token; }
    this.api.completeSetup(this.setupToken, p).subscribe({
      next: (r) => { this.completeResult.set(r); this.completing.set(false); this.step.set(5); },
      error: (err) => { this.error.set(err.message); this.completing.set(false); },
    });
  }

  openDashboard(): void { window.location.href = "/"; }
}
