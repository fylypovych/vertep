import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { VertepApiService } from '../../core/api.service';
import { IntegrationStatus } from '../../core/models';

@Component({
  selector: 'app-settings-integrations',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-integrations">
      <h3 class="text-lg font-semibold text-slate-900 mb-4">Інтеграції</h3>
      @if (loading()) {
        <div class="animate-pulse space-y-2"><div class="h-5 bg-slate-200 rounded w-full"></div></div>
      } @else if (error()) {
        <p class="text-red-600">{{ error() }}</p>
      } @else if (integrations()) {
        <div class="space-y-2">
          <div class="flex justify-between py-2 border-b border-slate-100">
            <span class="text-sm">Ollama</span>
            <span class="text-xs font-medium" [class.text-emerald-600]="integrations()!.ollama.status === 'ONLINE'" [class.text-red-600]="integrations()!.ollama.status !== 'ONLINE'">
              {{ integrations()!.ollama.status }}
            </span>
          </div>
          <div class="flex justify-between py-2 border-b border-slate-100">
            <span class="text-sm">ComfyUI</span>
            <span class="text-xs font-medium" [class.text-emerald-600]="integrations()!.comfyui.status === 'ONLINE'" [class.text-red-600]="integrations()!.comfyui.status !== 'ONLINE'">
              {{ integrations()!.comfyui.status }}
            </span>
          </div>
        </div>
      }
    </div>
  `,
})
export class IntegrationsSectionComponent implements OnInit {
  integrations = signal<IntegrationStatus | null>(null);
  loading = signal(false);
  error = signal<string | null>(null);

  constructor(private api: VertepApiService) {}

  ngOnInit(): void {
    this.loading.set(true);
    this.api.getIntegrations().subscribe({
      next: (data) => { this.integrations.set(data as unknown as IntegrationStatus); this.loading.set(false); },
      error: (err) => { this.error.set(err.message); this.loading.set(false); },
    });
  }
}
