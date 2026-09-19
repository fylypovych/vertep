import { Component } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterOutlet } from '@angular/router';
import { SidebarComponent } from './sidebar.component';
import { HeaderComponent } from './header.component';
import { ToastContainerComponent } from '../shared/toast-container.component';
import { ConfirmDialogComponent } from '../shared/confirm-dialog.component';
import { SidebarService } from '../core/services/sidebar.service';
import { LoadingService } from '../core/services/loading.service';
import { LoadingStateComponent } from '../shared/loading-state.component';

@Component({
  selector: 'app-layout',
  standalone: true,
  imports: [
    CommonModule,
    RouterOutlet,
    SidebarComponent,
    HeaderComponent,
    ToastContainerComponent,
    ConfirmDialogComponent,
    LoadingStateComponent
  ],
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
        <main class="flex-1 overflow-y-auto p-4 lg:p-6" role="main" aria-label="Основний вміст">
          <router-outlet></router-outlet>
        </main>
      </div>

      <app-toast-container />
      <app-confirm-dialog />

      <!-- Global loader -->
      <div class="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
           *ngIf="loadingService.loading()">
        <app-loading-state size="w-16 h-16" />
      </div>
    </div>
  `,
})
export class LayoutComponent {
  get sidebarCollapsed(): boolean {
    return this.sidebarService.collapsed();
  }

  constructor(
    private sidebarService: SidebarService,
    public loadingService: LoadingService
  ) {}

  closeSidebar(): void {
    this.sidebarService.close();
  }
}
