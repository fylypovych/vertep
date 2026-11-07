import { inject, Pipe, PipeTransform } from '@angular/core';
import { I18nService } from './i18n.service';

/** Impure so a language change redraws the text without a reload, keeping unsaved forms. */
@Pipe({ name: 't', standalone: true, pure: false })
export class TPipe implements PipeTransform {
  private i18n = inject(I18nService);
  transform(text: unknown): string { return text === null || text === undefined ? '' : this.i18n.t(String(text)); }
}
