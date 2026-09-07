import { Injectable } from '@angular/core';
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

  confirm(options: ConfirmOptions): Observable<boolean> {
    return new Observable<boolean>((subscriber) => {
      this.pending = {
        resolve: (value: boolean) => {
          subscriber.next(value);
          subscriber.complete();
        },
      };
      this.openSubject.next(options);
    });
  }

  resolve(value: boolean): void {
    if (this.pending) {
      this.pending.resolve(value);
      this.pending = null;
    }
  }
}
