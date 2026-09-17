import { Component, OnInit, computed, inject } from '@angular/core';
import { RouterModule } from '@angular/router';
import { CommonModule } from '@angular/common';
import { SidebarService } from '../core/services/sidebar.service';
import { VertepApiService } from '../core/api.service';
import { PolicyService } from '../core/services/policy.service';

interface NavItem {
  id: string;
  path: string;
  label: string;
  icon: string;
  exact?: boolean;
  queryParams?: Record<string, string>;
  adminOnly?: boolean;
}

interface SidebarGroup {
  label: string;
  items: NavItem[];
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
      [class.translate-x-0]="!collapsed"
      role="navigation"
      aria-label="Навігація">

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
      <nav class="flex-1 overflow-y-auto overflow-x-hidden py-4 px-2 space-y-1" data-testid="sidebar-nav">
        @for (group of visibleGroups(); track group.label) {
          <div class="px-3 py-1.5 text-xs font-medium text-slate-500 uppercase transition-all duration-200"
               [class.opacity-0]="collapsed"
               [class.w-0]="collapsed"
               [class.overflow-hidden]="collapsed"
               [class.invisible]="collapsed">
            {{ group.label }}
          </div>
          @for (item of group.items; track item.id) {
            <a [routerLink]="item.path"
               [queryParams]="item.queryParams"
               routerLinkActive="bg-emerald-50 dark:bg-emerald-900/30 text-emerald-700 dark:text-emerald-400"
               ariaCurrentWhenActive="page"
               [routerLinkActiveOptions]="{ exact: item.exact ?? false }"
               class="flex items-center gap-3 px-3 py-2.5 text-sm font-medium text-slate-700 dark:text-slate-300 rounded-lg hover:bg-slate-50 dark:hover:bg-slate-700 transition-colors"
               [attr.aria-label]="collapsed ? item.label : null"
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
  navGroups: SidebarGroup[] = [
    {
      label: 'Контент',
      items: [
        {
          id: 'dashboard',
          path: '/',
          label: 'Дашборд',
          exact: true,
          icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2V6zM14 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2zM14 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z"/></svg>`,
        },
        {
          id: 'jobs',
          path: '/jobs',
          label: 'Завдання',
          icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2"/></svg>`,
        },
        {
          id: 'queue',
          path: '/jobs',
          label: 'Виконання',
          icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10"/></svg>`,
          queryParams: { tab: 'queue' },
        },
        {
          id: 'published',
          path: '/published',
          label: 'Опубліковане',
          icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z"/></svg>`,
        },
        {
          id: 'characters',
          path: '/characters',
          label: 'Персонажі',
          icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14.828 14.828a4 4 0 01-5.656 0M9 10h.01M15 10h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>`,
        },
        {
          id: 'workflows',
          path: '/workflows',
          label: 'Сценарії',
          icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6.25v11.5M6 12h12M6 12l5 5m0 0l5-5"/></svg>`,
        },
        {
          id: 'brands',
          path: '/brands',
          label: 'Бренди',
          icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v14m14 0v2a2 2 0 002 2h2M5 19h14M5 19l1.5-1.5M5 19l-1.5 1.5M12 12a3 3 0 100-6 3 3 0 000 6z"/></svg>`,
        },
      ],
    },
    {
      label: 'Система',
      items: [
        {
          id: 'workers',
          path: '/workers',
          label: 'Вузли',
          icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z"/></svg>`,
        },
        {
          id: 'alerts',
          path: '/alerts',
          label: 'Алерти',
          icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"/></svg>`,
        },
      ],
    },
    {
      label: 'Адміністрування',
      items: [
        {
          id: 'settings',
          path: '/settings',
          label: 'Налаштування',
          queryParams: { tab: 'system' },
          icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 2.296 2.572-1.065 1.543.94 3.31-.826 2.37-2.37.94-1.543-.826-3.31-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37a1.724 1.724 0 002.573-1.066z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/></svg>`,
          adminOnly: true,
        },
        {
          id: 'settings-system',
          path: '/settings',
          label: 'Система',
          queryParams: { tab: 'system' },
          icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37.996.608 2.296.07 2.572-1.065z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/></svg>`,
          adminOnly: true,
        },
        { id: 'settings-telegram', path: '/settings', label: 'Telegram', queryParams: { tab: 'telegram' }, icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 0-8 0-8s0 0 0 0c0-4.418 4.03-8 9-8s9 3.582 9 8z"/></svg>`, adminOnly: true },
        { id: 'settings-secrets', path: '/settings', label: 'Секрети', queryParams: { tab: 'secrets' }, icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z"/></svg>`, adminOnly: true },
        { id: 'settings-models', path: '/settings', label: 'Моделі', queryParams: { tab: 'models' }, icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19.428 15.428a2 2 0 00-1.022-.547l-2.387-.477a6 6 0 00-3.86.517l-.318.158a6 6 0 01-3.86.517L6.05 15.21a2 2 0 00-1.806.547M8 4h8l-1 1v5.172a2 2 0 00.586 1.414l5 5c1.26 1.26.367 3.414-1.415 3.414H4.828c-1.782 0-2.674-2.154-1.414-3.414l5-5A2 2 0 009 10.172V5L8 4z"/></svg>`, adminOnly: true },
        { id: 'settings-update', path: '/settings', label: 'Оновлення', queryParams: { tab: 'update' }, icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>`, adminOnly: true },
        { id: 'settings-backup', path: '/settings', label: 'Бекапи', queryParams: { tab: 'backup' }, icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 8h14M5 8a2 2 0 110-4h14a2 2 0 110 4M5 8v10a2 2 0 002 2h10a2 2 0 002-2V8m-9 4h4"/></svg>`, adminOnly: true },
        { id: 'settings-security', path: '/settings', label: 'Безпека', queryParams: { tab: 'security' }, icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z"/></svg>`, adminOnly: true },
        { id: 'settings-roles', path: '/settings', label: 'Ролі', queryParams: { tab: 'roles' }, icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z"/></svg>`, adminOnly: true },
        { id: 'settings-branding', path: '/settings', label: 'Брендинг', queryParams: { tab: 'branding' }, icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12z"/></svg>`, adminOnly: true },
        { id: 'settings-integrations', path: '/settings', label: 'Інтеграції', queryParams: { tab: 'integrations' }, icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 10-5.656-5.656l-1.1 1.1"/></svg>`, adminOnly: true },
      ],
    },
  ];

  visibleGroups = computed(() => {
    const isAdmin = this.policy.userRole() === 'admin';
    return this.navGroups
      .map(group => ({
        label: group.label,
        items: group.items.filter(item => !item.adminOnly || isAdmin),
      }))
      .filter(group => group.items.length > 0);
  });

  get collapsed(): boolean {
    return this.sidebarService.collapsed();
  }

  get isMobile(): boolean {
    return window.innerWidth < 1024;
  }

  runtimeVersion: string | null = null;

  constructor(private sidebarService: SidebarService, private api: VertepApiService, private policy: PolicyService) {}

  ngOnInit(): void {
    this.api.getStatus().subscribe({
      next: (s) => { this.runtimeVersion = s.version || null; },
      error: () => { this.runtimeVersion = null; },
    });
  }
}
