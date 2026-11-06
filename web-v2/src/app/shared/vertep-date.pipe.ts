import { inject, Pipe, PipeTransform } from '@angular/core';
import { I18nService } from '../core/i18n/i18n.service';

@Pipe({ name: 'vertepDate', standalone: true, pure: false })
export class VertepDatePipe implements PipeTransform {
  private i18n = inject(I18nService);

  transform(value: string | Date | null | undefined): string {
    if (!value) return '—';
    try {
      const date = typeof value === 'string' ? new Date(value) : value;
      if (isNaN(date.getTime())) return '—';
      if (this.i18n.language() === 'en') {
        return date.toLocaleString('en-US', { dateStyle: 'short', timeStyle: 'short' });
      }
      const day = date.getDate().toString().padStart(2, '0');
      const month = (date.getMonth() + 1).toString().padStart(2, '0');
      const year = date.getFullYear();
      const hours = date.getHours().toString().padStart(2, '0');
      const minutes = date.getMinutes().toString().padStart(2, '0');
      return `${day}.${month}.${year} ${hours}:${minutes}`;
    } catch {
      return '—';
    }
  }
}
