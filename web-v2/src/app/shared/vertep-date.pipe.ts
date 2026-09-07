import { Pipe, PipeTransform } from '@angular/core';

@Pipe({ name: 'vertepDate', standalone: true })
export class VertepDatePipe implements PipeTransform {
  transform(value: string | Date | null | undefined): string {
    if (!value) return '—';
    try {
      const date = typeof value === 'string' ? new Date(value) : value;
      if (isNaN(date.getTime())) return '—';
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
