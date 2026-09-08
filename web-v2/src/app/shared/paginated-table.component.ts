import { Component, Input, Output, EventEmitter, signal, computed } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';

@Component({
  selector: 'app-paginated-table',
  standalone: true,
  imports: [CommonModule, FormsModule],
  template: `
    <div data-testid="paginated-table">
      <div class="mb-4">
        <input [(ngModel)]="searchTerm" placeholder="Пошук..." class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 text-sm">
      </div>
      <div class="overflow-x-auto">
        <table class="w-full text-sm text-left">
          <thead class="text-xs text-slate-500 uppercase bg-slate-50">
            <tr>
              @for (col of columns; track col.key) {
                <th class="px-4 py-3">{{ col.label }}</th>
              }
            </tr>
          </thead>
          <tbody>
            @for (row of pagedRows(); track trackByFn(row)) {
              <tr class="border-t border-slate-100">
                @for (col of columns; track col.key) {
                  <td class="px-4 py-3">
                    {{ col.template ? col.template(row) : (row[col.key] ?? '—') }}
                  </td>
                }
              </tr>
            } @empty {
              <tr><td [attr.colspan]="columns.length" class="px-4 py-6 text-center text-slate-500">Немає даних</td></tr>
            }
          </tbody>
        </table>
      </div>
      @if (pages() > 1) {
        <div class="flex items-center justify-between mt-4">
          <button (click)="prev()" [disabled]="page() === 1" class="px-3 py-1.5 text-sm border border-slate-200 rounded-lg disabled:opacity-50">Назад</button>
          <span class="text-sm text-slate-600">Сторінка {{ page() }} з {{ pages() }}</span>
          <button (click)="next()" [disabled]="page() === pages()" class="px-3 py-1.5 text-sm border border-slate-200 rounded-lg disabled:opacity-50">Вперед</button>
        </div>
      }
    </div>
  `,
})
export class PaginatedTableComponent {
  @Input() rows: any[] = [];
  @Input() columns: Array<{ key: string; label: string; template?: (row: any) => string }> = [];
  @Input() pageSize = 10;
  @Input() trackByFn: (row: any) => string | number = (row) => row.id || row.job_id || JSON.stringify(row);
  @Output() pageChange = new EventEmitter<{ page: number; pageSize: number }>();

  searchTerm = '';
  page = signal(1);

  filteredRows = computed(() => {
    if (!this.searchTerm.trim()) return this.rows;
    const term = this.searchTerm.toLowerCase();
    return this.rows.filter(row => this.columns.some(col => String(row[col.key] ?? '').toLowerCase().includes(term)));
  });

  pagedRows = computed(() => {
    const start = (this.page() - 1) * this.pageSize;
    return this.filteredRows().slice(start, start + this.pageSize);
  });

  pages = computed(() => Math.max(1, Math.ceil(this.filteredRows().length / this.pageSize)));

  prev(): void { if (this.page() > 1) this.page.update(p => p - 1); }
  next(): void { if (this.page() < this.pages()) this.page.update(p => p + 1); }
}
