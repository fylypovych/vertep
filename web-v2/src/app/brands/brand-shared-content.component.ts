import { TPipe } from '../core/i18n/t.pipe';
import { Component, Input, OnChanges, signal, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ResourcesApiService, SharedContentItem } from '../core/api/resources.api';
import { ToastService } from '../core/services/toast.service';
import { Router } from '@angular/router';

/** #500: «Спільний контент» — material of other brands with this brand's characters. */
@Component({
  selector: 'app-brand-shared-content',
  standalone: true,
  imports: [TPipe, CommonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="mt-4 border-t border-slate-100 pt-3" data-testid="brand-shared-content">
      <h4 class="text-sm font-semibold text-slate-800 mb-2">{{ 'Спільний контент' | t }}</h4>
      @if (items().length === 0) {
        <div class="text-xs text-slate-500">{{ 'Немає матеріалів інших брендів з вашими персонажами' | t }}</div>
      }
      @for (item of items(); track item.job_id) {
        <div class="flex flex-wrap items-center gap-2 text-sm py-1" [attr.data-testid]="'shared-' + item.job_id">
          <span class="font-mono text-xs">{{ item.job_id }}</span>
          <span class="text-slate-500">{{ item.brand_id }} · {{ item.character_id }} · v{{ item.version }}</span>
          <span class="px-2 py-0.5 rounded-full text-xs bg-slate-100">{{ status(item) | t }}</span>
          @if (item.version !== null) {
            <a class="text-blue-600 text-xs" [href]="'/api/jobs/' + item.job_id + '/final/video.mp4'" target="_blank" rel="noopener">{{ 'Переглянути' | t }}</a>
            <button class="text-emerald-700 text-xs" (click)="decide(item, 'approve')">{{ 'Погодити' | t }}</button>
            <button class="text-red-600 text-xs" (click)="decide(item, 'reject')">{{ 'Відхилити' | t }}</button>
            @if (item.owners[brandId] === 'approved') {
              <button class="text-amber-700 text-xs" (click)="revoke(item)">{{ 'Відкликати' | t }}</button>
              <button class="text-indigo-700 text-xs" (click)="publishOwn(item)" [attr.data-testid]="'own-publication-' + item.job_id">{{ 'Опублікувати у своїх каналах' | t }}</button>
            }
          }
        </div>
      }
    </div>
  `,
})
export class BrandSharedContentComponent implements OnChanges {
  @Input({ required: true }) brandId!: string;
  items = signal<SharedContentItem[]>([]);

  constructor(private api: ResourcesApiService, private toast: ToastService, private router: Router) {}

  ngOnChanges(): void {
    this.load();
  }

  status(item: SharedContentItem): string {
    return ({ pending: 'Очікує погодження', approved: 'Погоджено', rejected: 'Відхилено', revoked: 'Відкликано' } as Record<string, string>)[item.owners[this.brandId]] || 'Очікує погодження';
  }

  load(): void {
    this.api.sharedContent(this.brandId).subscribe({
      next: (data) => this.items.set(data.items),
      error: () => this.items.set([]),
    });
  }

  decide(item: SharedContentItem, decision: 'approve' | 'reject'): void {
    if (item.version === null || !item.sha256) return;
    this.api.sharedDecision(item.job_id, { owner_brand: this.brandId, decision, version: item.version, expected_sha256: item.sha256 })
      .subscribe({ next: () => this.load(), error: (error) => this.toast.show(error?.message || 'Помилка', 'error') });
  }

  /** The same approved video as this brand's own job; titles/texts are set there before publishing. */
  publishOwn(item: SharedContentItem): void {
    this.api.sharedPublication(item.job_id, { brand_id: this.brandId })
      .subscribe({ next: (job) => this.router.navigate(['/jobs', job.job_id]),
                   error: (error) => this.toast.show(error?.message || 'Помилка', 'error') });
  }

  revoke(item: SharedContentItem): void {
    this.api.sharedRevoke(item.job_id, this.brandId)
      .subscribe({ next: () => this.load(), error: (error) => this.toast.show(error?.message || 'Помилка', 'error') });
  }
}
