import { TPipe } from '../core/i18n/t.pipe';
import { Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';

@Component({
  selector: 'app-status-badge',
  standalone: true,
  imports: [TPipe, CommonModule],
  template: `
    <span class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium"
          [class.bg-emerald-50]="isActive"
          [class.text-emerald-700]="isActive"
          [class.bg-slate-100]="!isActive"
          [class.text-slate-600]="!isActive">
      <span class="w-1.5 h-1.5 rounded-full"
            [class.bg-emerald-500]="isActive"
            [class.bg-slate-400]="!isActive"></span>
      {{ (label || status) | t }}
    </span>
  `,
})
export class StatusBadgeComponent {
  @Input() status: string = '';
  @Input() label: string | null = null;
  @Input() activeStates: string[] = ['READY', 'ONLINE', 'FREE', 'RUNNING', 'HEALTHY', 'OK'];

  get isActive(): boolean {
    return this.activeStates.map(s => s.toUpperCase()).includes(this.status.toUpperCase());
  }
}
