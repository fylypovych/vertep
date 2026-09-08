import { Component, OnInit, signal, Input, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterModule, ActivatedRoute, Router } from '@angular/router';
import { VertepApiService } from '../core/api.service';
import { ToastService } from '../core/services/toast.service';
import { ConfirmService } from '../core/services/confirm.service';
import { Character } from '../core/models';
import { ConfigSection } from './config-section.component';

@Component({
  selector: 'app-character-detail',
  standalone: true,
  imports: [CommonModule, RouterModule, ConfigSection],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="space-y-5" data-testid="character-detail-page">
      <div class="flex items-center gap-4">
        <a routerLink="/characters" class="text-emerald-600 hover:text-emerald-700 text-sm font-medium">&larr; Назад до персонажів</a>
        @if (character()) {
          <h2 class="text-xl font-semibold text-slate-900">{{ character()!.name }}</h2>
        }
      </div>

      @if (loading()) {
        <div class="animate-pulse space-y-4">
          <div class="h-6 bg-slate-200 rounded w-3/4"></div>
          <div class="h-4 bg-slate-200 rounded w-1/2"></div>
          <div class="h-64 bg-slate-200 rounded"></div>
        </div>
      } @else if (error()) {
        <div class="bg-red-50 border border-red-200 rounded-xl p-4">
          <p class="text-red-700">{{ error() }}</p>
        </div>
      } @else if (character()) {
        <div class="space-y-5">
          <div class="bg-white rounded-xl border border-slate-200 p-5">
            <h3 class="text-sm font-medium text-slate-500 mb-3">Основна інформація</h3>
            <dl class="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
              <div><dt class="text-slate-500">ID</dt><dd class="font-mono text-xs break-all">{{ character()!.id }}</dd></div>
              <div><dt class="text-slate-500">Мова</dt><dd>{{ character()!.language }}</dd></div>
              <div><dt class="text-slate-500">Активний</dt><dd>{{ character()!.enabled ? 'Так' : 'Ні' }}</dd></div>
              <div><dt class="text-slate-500">Workflow</dt><dd>{{ character()!.workflow || '—' }}</dd></div>
            </dl>
          </div>

          <div class="bg-white rounded-xl border border-slate-200 p-5">
            <h3 class="text-sm font-medium text-slate-500 mb-3">Системний промпт</h3>
            <pre class="text-sm text-slate-700 bg-slate-50 p-3 rounded overflow-x-auto whitespace-pre-wrap">{{ character()!.system_prompt }}</pre>
          </div>

          <app-config-section title="Голос (voice.json)" [data]="character()!.voice" />
          <app-config-section title="Візуал (visual.json)" [data]="character()!.visual" />
          <app-config-section title="Генерація (generation.json)" [data]="character()!.generation" />
          <app-config-section title="Публікація (publishing.json)" [data]="character()!.publishing" />

          <div class="flex gap-2 pt-2">
            <a [routerLink]="['/characters']" class="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm font-medium">Редагувати</a>
            <button (click)="delete()" class="px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700 text-sm font-medium">Видалити</button>
          </div>
        </div>
      }
    </div>
  `,
})
export class CharacterDetailComponent implements OnInit {
  character = signal<Character | null>(null);
  loading = signal(true);
  error = signal<string | null>(null);

  constructor(
    private route: ActivatedRoute,
    private router: Router,
    private api: VertepApiService,
    private toast: ToastService,
    private confirm: ConfirmService,
  ) {}

  ngOnInit(): void {
    this.loadCharacter();
  }

  loadCharacter(): void {
    this.loading.set(true);
    this.error.set(null);
    this.api.getCharacter(this.route.snapshot.paramMap.get('id') || '').subscribe({
      next: (c) => { this.character.set(c); this.loading.set(false); },
      error: (err) => { this.error.set(err.message || 'Не вдалося завантажити персонажа'); this.loading.set(false); },
    });
  }

  delete(): void {
    const id = this.character()?.id;
    if (!id) return;
    this.confirm.confirm({ title: 'Видалити персонажа', message: `Ви впевнені, що хочете видалити ${id}?` }).subscribe((ok) => {
      if (!ok) return;
      this.api.deleteCharacter(id).subscribe({
        next: () => { this.toast.show('Персонаж видалено', 'success'); this.router.navigate(['/characters']); },
        error: (err) => this.toast.show(err.message || 'Помилка видалення', 'error'),
      });
    });
  }
}
