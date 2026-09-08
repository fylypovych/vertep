import { Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';

@Component({
  selector: 'app-action-toolbar',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="flex items-center justify-between mb-4" data-testid="action-toolbar">
      <h3 class="text-lg font-semibold text-slate-900">{{ title }}</h3>
      <div class="flex gap-2">
        <ng-content></ng-content>
      </div>
    </div>
  `,
})
export class ActionToolbarComponent {
  @Input() title: string = '';
}
