import { inject, Injectable } from '@angular/core';
import { I18nService } from '../i18n/i18n.service';
import { BehaviorSubject } from 'rxjs';

export interface Toast {
  id: number;
  message: string;
  type: 'success' | 'error' | 'info';
}

@Injectable({ providedIn: 'root' })
export class ToastService {
  private toasts = new BehaviorSubject<Toast[]>([]);
  toasts$ = this.toasts.asObservable();
  private counter = 0;
  private i18n = inject(I18nService);

  show(message: string, type: 'success' | 'error' | 'info' = 'info') {
    const id = ++this.counter;
    const current = this.toasts.value;
    this.toasts.next([...current, { id, message: this.i18n.t(message), type }]);
    setTimeout(() => this.dismiss(id), 4000);
  }

  dismiss(id: number) {
    this.toasts.next(this.toasts.value.filter(t => t.id !== id));
  }
}
