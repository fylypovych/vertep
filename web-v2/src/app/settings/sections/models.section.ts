import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { SettingsApiService } from '../../core/api/settings.api';
import { ToastService } from '../../core/services/toast.service';
import { ConfirmService } from '../../core/services/confirm.service';
import { ModelInfo } from '../../core/models';
import { LoadingStateComponent } from '../../shared/loading-state.component';
import { ErrorStateComponent } from '../../shared/error-state.component';

@Component({
  selector: 'app-settings-models',
  standalone: true,
  imports: [CommonModule, FormsModule, LoadingStateComponent, ErrorStateComponent],
  template: `<div class="bg-white rounded-xl border border-slate-200 p-5" data-testid="settings-models"><h3 class="text-lg font-semibold mb-4">Моделі (Ollama)</h3><app-loading-state *ngIf="loading()" /><app-error-state *ngIf="error()" [message]="error()!" /><div *ngIf="!loading() && !error()"><div class="flex gap-2 mb-4"><input [(ngModel)]="modelName" class="flex-1 border rounded px-3 py-2" placeholder="Назва моделі"><button (click)="pullModel()" [disabled]="!modelName || pulling()" class="px-4 py-2 bg-emerald-600 text-white rounded">{{ pulling() ? 'Завантаження...' : 'Завантажити' }}</button></div><div *ngFor="let model of models()" class="flex justify-between py-2 border-b"><span>{{ model.name }}</span><button (click)="deleteModel(model.name)" class="text-red-600">Видалити</button></div></div></div>`,
})
export class ModelsSectionComponent implements OnInit {
  models = signal<ModelInfo[]>([]);
  loading = signal(false);
  error = signal<string | null>(null);
  modelName = '';
  pulling = signal(false);

  constructor(
    private settings: SettingsApiService,
    private toast: ToastService,
    private confirm: ConfirmService,
  ) {}

  ngOnInit(): void {
    this.loadModels();
  }

  loadModels(): void {
    this.loading.set(true);
    this.error.set(null);
    this.settings.models().subscribe({
      next: (data) => { this.models.set((data['models'] as ModelInfo[]) || []); this.loading.set(false); },
      error: (err) => { this.error.set(err.message); this.loading.set(false); },
    });
  }

  pullModel(): void {
    if (!this.modelName || this.pulling()) return;
    this.pulling.set(true);
    this.settings.pullModel(this.modelName).subscribe({
      next: () => { this.pulling.set(false); this.loadModels(); },
      error: () => { this.pulling.set(false); },
    });
  }

  deleteModel(name: string): void {
    this.confirm.confirm({
      title: 'Видалити модель',
      message: `Видалити модель ${name}?`,
    }).subscribe((confirmed) => {
      if (!confirmed) return;
      this.settings.deleteModel(name).subscribe({
        next: () => this.loadModels(),
        error: (err) => this.error.set(err.message),
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
