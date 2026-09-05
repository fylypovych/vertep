import { Injectable } from '@angular/core';
import { Observable, Subject } from 'rxjs';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog.component';

export interface ConfirmOptions {
  title: string;
  message: string;
  confirmText?: string;
  cancelText?: string;
}

@Injectable({ providedIn: 'root' })
export class ConfirmService {
  private subject = new Subject<boolean>();

  confirm(options: ConfirmOptions): Observable<boolean> {
    const result = confirm(`${options.title}\n\n${options.message}`);
    const confirmed = result === true;
    this.subject.next(confirmed);
    this.subject.complete();
    this.subject = new Subject<boolean>();
    return this.subject.asObservable();
  }
}
