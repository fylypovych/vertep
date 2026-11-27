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
            <a class="text-blue-600 text-xs" [attr.data-testid]="'shared-view-' + item.job_id" [href]="'/api/jobs/' + item.job_id + '/videos/' + item.version + '?sha256=' + item.sha256" target="_blank" rel="noopener">{{ 'Переглянути' | t }}</a>
            <button class="text-emerald-700 text-xs" (click)="decide(item, 'approve')">{{ 'Погодити' | t }}</button>
            <button class="text-red-600 text-xs" (click)="decide(item, 'reject')">{{ 'Відхилити' | t }}</button>
            @if (item.owners[brandId] === 'approved') {
              <button class="text-amber-700 text-xs" (click)="revoke(item)">{{ 'Відкликати' | t }}</button>
              <button class="text-indigo-700 text-xs" (click)="openOwn(item)" [attr.data-testid]="'own-publication-' + item.job_id">{{ 'Опублікувати у своїх каналах' | t }}</button>
            }
          }
        </div>
        @if (ownFor() === item.job_id) {
          <div class="grid grid-cols-1 md:grid-cols-2 gap-2 text-sm my-2" [attr.data-testid]="'own-form-' + item.job_id">
            <input class="border rounded px-2 py-1" placeholder="{{ 'Назва' | t }}" [value]="own.title" (input)="own.title = $any($event.target).value" data-testid="own-title">
            <input class="border rounded px-2 py-1" placeholder="{{ 'Хештеги через кому' | t }}" [value]="own.hashtags" (input)="own.hashtags = $any($event.target).value" data-testid="own-hashtags">
            <textarea class="border rounded px-2 py-1 md:col-span-2" rows="2" placeholder="{{ 'Опис' | t }}" (input)="own.description = $any($event.target).value" data-testid="own-description"></textarea>
            <input class="border rounded px-2 py-1 md:col-span-2" placeholder="{{ 'Обкладинка (URL)' | t }}" (input)="own.cover = $any($event.target).value" data-testid="own-cover">
            <button class="px-3 py-1 bg-indigo-600 text-white rounded md:col-span-2" (click)="publishOwn(item)" data-testid="own-create">{{ 'Створити власну публікацію' | t }}</button>
          </div>
        }
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
      // An answer without a list (older module, proxy page) shows «nothing shared», not a broken page.
      next: (data) => this.items.set(Array.isArray(data?.items) ? data.items : []),
      error: () => this.items.set([]),
    });
  }

  decide(item: SharedContentItem, decision: 'approve' | 'reject'): void {
    if (item.version === null || !item.sha256) return;
    this.api.sharedDecision(item.job_id, { owner_brand: this.brandId, decision, version: item.version, expected_sha256: item.sha256 })
      .subscribe({ next: () => this.load(), error: (error) => this.toast.show(error?.message || 'Помилка', 'error') });
  }

  ownFor = signal<string | null>(null);
  own = { title: '', description: '', hashtags: '', cover: '' };

  openOwn(item: SharedContentItem): void {
    this.own = { title: '', description: '', hashtags: '', cover: '' };
    this.ownFor.set(item.job_id);
  }

  /** The same approved video as this brand's own job, with this brand's own texts. */
  publishOwn(item: SharedContentItem): void {
    const hashtags = this.own.hashtags.split(',').map((tag) => tag.trim()).filter(Boolean);
    const body: { brand_id: string; title?: string; description?: string; hashtags?: string[]; cover?: string } = { brand_id: this.brandId };
    if (this.own.title.trim()) body.title = this.own.title.trim();
    if (this.own.description.trim()) body.description = this.own.description.trim();
    if (hashtags.length) body.hashtags = hashtags;
    if (this.own.cover.trim()) body.cover = this.own.cover.trim();
    this.api.sharedPublication(item.job_id, body)
      .subscribe({ next: (job) => this.router.navigate(['/jobs', job.job_id]),
                   error: (error) => this.toast.show(error?.message || 'Помилка', 'error') });
  }

  revoke(item: SharedContentItem): void {
    this.api.sharedRevoke(item.job_id, this.brandId)
      .subscribe({ next: () => this.load(), error: (error) => this.toast.show(error?.message || 'Помилка', 'error') });
  }
}
