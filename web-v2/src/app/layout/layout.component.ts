import { Component } from '@angular/core';
import { RouterOutlet } from '@angular/router';
import { SidebarComponent } from './sidebar.component';
import { HeaderComponent } from './header.component';
import { ToastContainerComponent } from '../shared/toast-container.component';
import { ConfirmDialogComponent } from '../shared/confirm-dialog.component';
import { SidebarService } from '../core/services/sidebar.service';

@Component({
  selector: 'app-layout',
  standalone: true,
  imports: [RouterOutlet, SidebarComponent, HeaderComponent, ToastContainerComponent, ConfirmDialogComponent],
  template: `
    <div class="flex h-screen overflow-hidden bg-slate-50 dark:bg-slate-900">
      <app-sidebar />

      <!-- Mobile backdrop -->
      <div class="fixed inset-0 z-20 bg-black/50 lg:hidden"
           [class.hidden]="sidebarCollapsed"
           (click)="closeSidebar()"></div>

      <!-- Main area -->
      <div class="flex flex-1 flex-col min-w-0 overflow-hidden">
        <app-header />
        <main class="flex-1 overflow-y-auto p-4 lg:p-6">
          <router-outlet></router-outlet>
        </main>
      </div>

      <app-toast-container />
      <app-confirm-dialog />
    </div>
  `,
})
export class LayoutComponent {
  get sidebarCollapsed(): boolean {
    return this.sidebarService.collapsed();
  }

  constructor(private sidebarService: SidebarService) {}

  closeSidebar(): void {
    this.sidebarService.close();
  }
}
