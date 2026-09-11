import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { VertepApiService } from '../../core/api.service';
import { ToastService } from '../../core/services/toast.service';
import { ModelInfo } from '../../core/models';

@Component({
  selector: 'app-settings-models',
  standalone: true,
  imports: [CommonModule, FormsModule],
  template: `
    <div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-models">
      <h3 class="text-lg font-semibold text-slate-900 mb-4">Моделі (Ollama)</h3>
      @if (loading()) {
        <div class="animate-pulse space-y-2"><div class="h-5 bg-slate-200 rounded w-full"></div><div class="h-5 bg-slate-200 rounded w-1/2"></div></div>
      } @else if (error()) {
        <p class="text-red-600">{{ error() }}</p>
      } @else {
        <div class="mb-4 flex gap-2">
          <input [(ngModel)]="modelName" placeholder="Назва моделі (напр. llama3)" class="flex-1 px-3 py-2 border border-slate-200 rounded-lg text-sm">
          <button (click)="pullModel()" [disabled]="!modelName || pulling()" class="px-4 py-2 bg-emerald-600 text-white rounded-lg text-sm hover:bg-emerald-700 disabled:opacity-50">
            {{ pulling() ? 'Завантаження...' : 'Завантажити' }}
          </button>
        </div>
        @if (models().length === 0) {
          <p class="text-sm text-slate-500">Моделі не знайдено</p>
        } @else {
          <div class="space-y-2">
            @for (model of models(); track model.name) {
              <div class="flex items-center justify-between py-2 border-b border-slate-100">
                <div>
                  <span class="text-sm font-medium text-slate-900">{{ model.name }}</span>
                  <span class="text-xs text-slate-500 ml-2">{{ model.size ? formatBytes(model.size) : '' }}</span>
                </div>
                <button (click)="deleteModel(model.name)" class="text-xs text-red-600 hover:text-red-700">Видалити</button>
              </div>
            }
          </div>
        }
      }
    </div>
  `,
})
export class ModelsSectionComponent implements OnInit {
  models = signal<ModelInfo[]>([]);
  loading = signal(false);
  error = signal<string | null>(null);
  modelName = '';
  pulling = signal(false);

  constructor(private api: VertepApiService, private toast: ToastService) {}

  ngOnInit(): void {
    this.loadModels();
  }

  loadModels(): void {
    this.loading.set(true);
    this.error.set(null);
    this.api.getModels().subscribe({
      next: (data) => { this.models.set((data['models'] as ModelInfo[]) || []); this.loading.set(false); },
      error: (err) => { this.error.set(err.message); this.loading.set(false); },
    });
  }

  pullModel(): void {
    if (!this.modelName || this.pulling()) return;
    this.pulling.set(true);
    this.api.pullModel(this.modelName).subscribe({
      next: () => { this.pulling.set(false); this.loadModels(); },
      error: () => this.pulling.set(false),
    });
  }

  deleteModel(name: string): void {
    if (!confirm(`Видалити модель ${name}?`)) return;
    this.api.deleteModel(name).subscribe({
      next: () => this.loadModels(),
      error: (err) => this.error.set(err.message),
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
