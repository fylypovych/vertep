import { Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';

@Component({
  selector: 'app-json-viewer',
  standalone: true,
  imports: [CommonModule],
  template: `
    <details class="mt-2" data-testid="json-viewer">
      <summary class="text-sm text-slate-500 hover:text-slate-700 cursor-pointer select-none">Технічні деталі</summary>
      <pre class="mt-2 text-xs text-slate-600 bg-slate-50 p-4 rounded-lg overflow-auto max-h-64">{{ value | json }}</pre>
    </details>
  `,
})
export class JsonViewerComponent {
  @Input() value: unknown = {};
}
