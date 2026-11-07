import { TPipe } from '../core/i18n/t.pipe';
import { Component, inject } from '@angular/core';
import { I18nService, Language } from '../core/i18n/i18n.service';

@Component({
  imports: [TPipe],
  selector: 'app-language-switch',
  standalone: true,
  template: `
    <div class="inline-flex rounded-lg border border-slate-200 dark:border-slate-700 text-xs overflow-hidden" role="group" aria-label="Мова / Language">
      @for (option of options; track option.id) {
        <button type="button" class="px-2 py-1"
                [class.bg-emerald-600]="i18n.language() === option.id"
                [class.text-white]="i18n.language() === option.id"
                [attr.aria-pressed]="i18n.language() === option.id"
                [attr.data-testid]="'language-' + option.id"
                (click)="i18n.set(option.id)">{{ (option.label) | t }}</button>
      }
    </div>
  `,
})
export class LanguageSwitchComponent {
  readonly i18n = inject(I18nService);
  readonly options: { id: Language; label: string }[] = [{ id: 'uk', label: 'УК' }, { id: 'en', label: 'EN' }];
}
