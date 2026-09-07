import { Injectable, signal } from '@angular/core';

@Injectable({ providedIn: 'root' })
export class SidebarService {
  collapsed = signal<boolean>(false);

  toggle(): void {
    this.collapsed.update(v => !v);
  }

  open(): void {
    this.collapsed.set(false);
  }

  close(): void {
    this.collapsed.set(true);
  }
}
