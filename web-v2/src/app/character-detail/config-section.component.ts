import { Component, Input, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';

@Component({
  selector: 'app-config-section',
  standalone: true,
  imports: [CommonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="bg-white rounded-xl border border-slate-200 p-5">
      <h3 class="text-sm font-medium text-slate-500 mb-3">{{ title }}</h3>
      <pre class="text-xs text-slate-700 bg-slate-50 p-3 rounded overflow-x-auto">{{ json }}</pre>
    </div>
  `,
})
export class ConfigSection {
  @Input() title = '';
  @Input() data: Record<string, unknown> = {};

  get json(): string {
    try {
      return JSON.stringify(this.data, null, 2);
    } catch {
      return String(this.data);
    }
  }
}
