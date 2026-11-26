import { Component } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterOutlet } from '@angular/router';
import { TPipe } from '../../core/i18n/t.pipe';
import { SidebarService } from '../../core/services/sidebar.service';
import { LoadingService } from '../../core/services/loading.service';
import { ToastContainerComponent } from '../../shared/toast-container.component';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog.component';
import { PostSetupDialogComponent } from '../../shared/post-setup-dialog.component';
import { LoadingStateComponent } from '../../shared/loading-state.component';
import { V3SidebarComponent } from './v3-sidebar.component';
import { V3NavbarComponent } from './v3-navbar.component';

@Component({
  selector: 'app-v3-layout',
  standalone: true,
  imports: [
    CommonModule,
    RouterOutlet,
    TPipe,
    V3SidebarComponent,
    V3NavbarComponent,
    ToastContainerComponent,
    ConfirmDialogComponent,
    PostSetupDialogComponent,
    LoadingStateComponent,
  ],
  template: `
    <div class="v3-paper-theme flex h-screen overflow-hidden" data-testid="v3-layout">
      <app-v3-sidebar />

      <!-- Mobile backdrop -->
      <div class="fixed inset-0 z-20 bg-black/40 lg:hidden"
           [class.hidden]="sidebarCollapsed"
           (click)="closeSidebar()"></div>

      <!-- Main content area -->
      <div class="flex flex-1 flex-col min-w-0 overflow-hidden">
        <app-v3-navbar />
        <main class="flex-1 overflow-y-auto p-4 lg:p-6" role="main" aria-label="{{ 'Основний вміст' | t }}">
          <div class="max-w-7xl mx-auto">
            <router-outlet></router-outlet>
          </div>
        </main>
      </div>

      <app-toast-container />
      <app-confirm-dialog />
      <app-post-setup-dialog />

      <div class="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
           data-testid="global-loading-overlay"
           *ngIf="loadingService.loading()">
        <app-loading-state size="w-16 h-16" />
      </div>
    </div>
  `,
})
export class V3LayoutComponent {
  get sidebarCollapsed(): boolean { return this.sidebarService.collapsed(); }

  constructor(
    private sidebarService: SidebarService,
    public loadingService: LoadingService,
  ) {}

  closeSidebar(): void { this.sidebarService.close(); }
}
