import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterModule } from '@angular/router';
import { VertepApiService } from '../core/api.service';
import { ToastService } from '../core/services/toast.service';
import { VertepDatePipe } from '../shared/vertep-date.pipe';
import { EmptyStateComponent } from '../shared/empty-state.component';
import { LoadingStateComponent } from '../shared/loading-state.component';
import { ErrorStateComponent } from '../shared/error-state.component';
import { Job, PublicationResult, Channel } from '../core/models';
import { statusLabel } from '../core/presentation';

@Component({
  selector: 'app-published',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterModule, VertepDatePipe, EmptyStateComponent, LoadingStateComponent, ErrorStateComponent],
  template: `
    <div class="space-y-6" data-testid="published-page">
      <div class="flex items-center justify-between">
        <h3 class="text-lg font-semibold text-slate-900">Опубліковані матеріали</h3>
        <button (click)="loadPublished()" class="text-sm text-emerald-600 hover:text-emerald-700 font-medium">Оновити</button>
      </div>

      <div class="flex gap-4 mb-4">
        <select [(ngModel)]="filterChannel" (change)="applyFilter()" class="px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500">
          <option value="">Усі канали</option>
          @for (ch of distinctChannels(); track ch) {
            <option [value]="ch">{{ channelLabel(ch) }}</option>
          }
        </select>
      </div>

      @if (loading()) {
        <app-loading-state />
      } @else if (error()) {
        <app-error-state [message]="error()!" (retry)="loadPublished()" />
      } @else if (filteredJobs().length === 0) {
        <app-empty-state message="Немає опублікованих матеріалів" />
      } @else {
        <div class="space-y-3">
          @for (job of filteredJobs(); track job.job_id) {
            <div class="bg-white rounded-xl border border-slate-200 p-4">
              <div class="flex items-start justify-between mb-3">
                <div>
                  <p class="text-sm font-medium text-slate-900">{{ job.topic }}</p>
                  <p class="text-xs text-slate-500">ID: {{ job.job_id }} · {{ job.created_at | vertepDate }}</p>
                </div>
                <span class="px-2 py-0.5 rounded-full text-xs font-medium bg-emerald-50 text-emerald-700">
                  {{ jobStatusLabel(job.status) }}
                </span>
              </div>

              <div class="space-y-2">
                @for (result of publicationResults(job); track result.channel) {
                  <div class="bg-slate-50 rounded-lg p-3 flex items-center justify-between">
                    <div>
                      <span class="text-sm font-medium text-slate-900">{{ channelLabel(result.channel) }}</span>
                      @if (result.url) {
                        <a [href]="result.url" target="_blank" class="text-xs text-emerald-600 hover:text-emerald-700 ml-2">{{ result.url }}</a>
                      }
                      @if (result.error) {
                        <p class="text-xs text-red-600 mt-0.5">{{ result.error }}</p>
                      }
                    </div>
                    <div class="flex items-center gap-2">
                      <span class="px-2 py-1 rounded-full text-xs font-medium"
                            [class.bg-emerald-50]="result.status === 'PUBLISHED'"
                            [class.text-emerald-700]="result.status === 'PUBLISHED'"
                            [class.bg-red-50]="result.status === 'FAILED' || result.status === 'NOT_CONFIGURED'"
                            [class.text-red-700]="result.status === 'FAILED' || result.status === 'NOT_CONFIGURED'"
                            [class.bg-amber-50]="result.status !== 'PUBLISHED' && result.status !== 'FAILED' && result.status !== 'NOT_CONFIGURED'"
                            [class.text-amber-700]="result.status !== 'PUBLISHED' && result.status !== 'FAILED' && result.status !== 'NOT_CONFIGURED'">
                        {{ result.status }}
                      </span>
                      @if (result.status === 'FAILED') {
                        <button (click)="retryChannel(job, result.channel)" [disabled]="retrying() === job.job_id + '-' + result.channel" class="text-xs px-2 py-1 bg-amber-600 text-white rounded hover:bg-amber-700 disabled:opacity-50">
                          {{ retrying() === job.job_id + '-' + result.channel ? '...' : 'Повторити' }}
                        </button>
                      }
                    </div>
                  </div>
                }
              </div>

              <div class="mt-3">
                <a [routerLink]="['/jobs', job.job_id]" class="text-xs text-emerald-600 hover:text-emerald-700 font-medium">Відкрити завдання</a>
              </div>
            </div>
          }
        </div>
      }
    </div>
  `,
})
export class PublishedComponent implements OnInit {
  loading = signal(false);
  error = signal<string | null>(null);
  publishedJobs = signal<Job[]>([]);
  filteredJobs = signal<Job[]>([]);
  publishedJobsAll = signal<Job[]>([]);
  filterChannel = '';
  retrying = signal<string | null>(null);

  channelLabels: Record<string, string> = {
    youtube: 'YouTube', tiktok: 'TikTok', instagram: 'Instagram',
    facebook: 'Facebook', threads: 'Threads', telegram: 'Telegram',
  };

  constructor(private api: VertepApiService, private toast: ToastService) {}

  ngOnInit(): void {
    this.loadPublished();
  }

  loadPublished(): void {
    this.loading.set(true);
    this.error.set(null);
    this.api.getJobs().subscribe({
      next: (jobs) => {
        const published = jobs.filter(j => j.status === 'PUBLISHED' || (j.published_to && j.published_to.length > 0));
        this.publishedJobsAll.set(published);
        this.applyFilter();
        this.loading.set(false);
      },
      error: (err) => {
        this.error.set(err.message || 'Не вдалося завантажити публікації');
        this.loading.set(false);
      },
    });
  }

  applyFilter(): void {
    const all = this.publishedJobsAll();
    if (!this.filterChannel) {
      this.publishedJobs.set(all);
      this.filteredJobs.set(all);
      return;
    }
    const filtered = all.filter(job => {
      const results = job.publication_results || {};
      return results[this.filterChannel]?.status === 'PUBLISHED';
    });
    this.publishedJobs.set(filtered);
    this.filteredJobs.set(filtered);
  }

  distinctChannels(): string[] {
    const channels = new Set<string>();
    for (const job of this.publishedJobsAll()) {
      const results = job.publication_results || {};
      for (const ch in results) {
        channels.add(ch);
      }
    }
    return Array.from(channels).sort();
  }

  channelLabel(channel: string): string {
    return this.channelLabels[channel] || channel.charAt(0).toUpperCase() + channel.slice(1);
  }

  publicationResults(job: Job): PublicationResult[] {
    const results: PublicationResult[] = [];
    const pub = job.publication_results || {};
    for (const channel in pub) {
      const result = pub[channel] as PublicationResult;
      results.push({
        channel,
        status: result?.status || 'unknown',
        url: result?.url,
        error: result?.error,
      });
    }
    return results;
  }

  retryChannel(job: Job, channel: string): void {
    const key = job.job_id + '-' + channel;
    this.retrying.set(key);
    this.api.publishJob(job.job_id, [channel]).subscribe({
      next: () => {
        this.toast.show(`Канал ${channel} повторно опубліковано`, 'success');
        this.retrying.set(null);
        this.loadPublished();
      },
      error: (err) => {
        this.toast.show(err.message || 'Помилка повторення публікації', 'error');
        this.retrying.set(null);
      },
    });
  }

  jobStatusLabel(status: string): string { return statusLabel(status); }
}
