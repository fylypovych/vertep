import { inject, Injectable } from '@angular/core';
import { I18nService } from '../i18n/i18n.service';
import { Observable, Subject } from 'rxjs';

export interface ConfirmOptions {
  title: string;
  message: string;
  confirmText?: string;
  cancelText?: string;
}

@Injectable({ providedIn: 'root' })
export class ConfirmService {
  private pending: { resolve: (value: boolean) => void } | null = null;
  private openSubject = new Subject<ConfirmOptions>();
  open$ = this.openSubject.asObservable();
  private i18n = inject(I18nService);

  confirm(options: ConfirmOptions): Observable<boolean> {
    return new Observable<boolean>((subscriber) => {
      this.pending = {
        resolve: (value: boolean) => {
          subscriber.next(value);
          subscriber.complete();
        },
      };
      const t = (text?: string) => (text ? this.i18n.t(text) : text);
      this.openSubject.next({ ...options, title: t(options.title)!, message: t(options.message)!,
                              confirmText: t(options.confirmText), cancelText: t(options.cancelText) });
    });
  }

  resolve(value: boolean): void {
    if (this.pending) {
      this.pending.resolve(value);
      this.pending = null;
    }
  }
}
