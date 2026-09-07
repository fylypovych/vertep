import { Component, OnInit } from '@angular/core';
import { RouterModule } from '@angular/router';
import { CommonModule } from '@angular/common';
import { SidebarService } from '../core/services/sidebar.service';
import { VertepApiService } from '../core/api.service';

interface NavItem {
  path: string;
  label: string;
  icon: string;
  exact?: boolean;
}

@Component({
  selector: 'app-sidebar',
  standalone: true,
  imports: [RouterModule, CommonModule],
  template: `
    <aside
      class="flex flex-col bg-white dark:bg-slate-800 border-r border-slate-200 dark:border-slate-700 h-full z-30 flex-shrink-0 transition-all duration-200"
      [class.w-64]="!collapsed"
      [class.w-16]="collapsed"
      [class.fixed]="isMobile"
      [class.relative]="!isMobile"
      [class.-translate-x-full]="isMobile && collapsed"
      [class.translate-x-0]="!collapsed">

      <!-- Logo -->
      <div class="flex items-center h-16 px-4 border-b border-slate-200 dark:border-slate-700 flex-shrink-0">
        <div class="flex items-center gap-3 overflow-hidden">
          <div class="w-8 h-8 bg-emerald-500 rounded-lg flex items-center justify-center text-white font-bold flex-shrink-0">V</div>
          <span class="text-lg font-semibold text-slate-800 dark:text-slate-100 whitespace-nowrap transition-opacity duration-200"
                [class.opacity-0]="collapsed"
                [class.w-0]="collapsed"
                [class.overflow-hidden]="collapsed">
            Vertep
          </span>
        </div>
      </div>

      <!-- Navigation -->
      <nav class="flex-1 overflow-y-auto overflow-x-hidden py-4 px-2 space-y-1">
        @for (item of navItems; track item.path) {
          <a [routerLink]="item.path"
             routerLinkActive="bg-emerald-50 dark:bg-emerald-900/30 text-emerald-700 dark:text-emerald-400"
             [routerLinkActiveOptions]="{ exact: item.exact ?? false }"
             class="flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-slate-700 dark:text-slate-300 rounded-lg hover:bg-slate-50 dark:hover:bg-slate-700 transition-colors"
             [title]="collapsed ? item.label : ''">
            <span class="flex-shrink-0 w-5 h-5" [innerHTML]="item.icon"></span>
            <span class="whitespace-nowrap transition-opacity duration-200"
                  [class.opacity-0]="collapsed"
                  [class.w-0]="collapsed"
                  [class.overflow-hidden]="collapsed">
              {{ item.label }}
            </span>
          </a>
        }
      </nav>

      <!-- Footer -->
      <div class="p-4 border-t border-slate-200 dark:border-slate-700 flex-shrink-0 overflow-hidden">
        <div class="text-xs text-slate-500 whitespace-nowrap transition-opacity duration-200"
             [class.opacity-0]="collapsed"
             [class.hidden]="collapsed">
          Vertep v{{ runtimeVersion || '...' }}
        </div>
      </div>
    </aside>
  `,
})
export class SidebarComponent implements OnInit {
  navItems: NavItem[] = [
    {
      path: '/',
      label: 'Дашборд',
      exact: true,
      icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2V6zM14 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2zM14 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z"/></svg>`,
    },
    {
      path: '/jobs',
      label: 'Завдання',
      icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2"/></svg>`,
    },
    {
      path: '/workers',
      label: 'Воркери',
      icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z"/></svg>`,
    },
    {
      path: '/characters',
      label: 'Персонажі',
      icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14.828 14.828a4 4 0 01-5.656 0M9 10h.01M15 10h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>`,
    },
    {
      path: '/settings',
      label: 'Налаштування',
      icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/></svg>`,
    },
  ];

  get collapsed(): boolean {
    return this.sidebarService.collapsed();
  }

  get isMobile(): boolean {
    return window.innerWidth < 1024;
  }

  runtimeVersion: string | null = null;

  constructor(private sidebarService: SidebarService, private api: VertepApiService) {}

  ngOnInit(): void {
    this.api.getStatus().subscribe({
      next: (s) => { this.runtimeVersion = s.version || null; },
      error: () => { this.runtimeVersion = null; },
    });
  }
}
