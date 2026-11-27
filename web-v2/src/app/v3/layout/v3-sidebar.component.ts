import { Component, OnDestroy, OnInit, computed, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { NavigationEnd, Router, RouterModule } from '@angular/router';
import { Subscription } from 'rxjs';
import { filter } from 'rxjs/operators';
import { TPipe } from '../../core/i18n/t.pipe';
import { SidebarService } from '../../core/services/sidebar.service';
import { SystemApiService } from '../../core/api/system.api';
import { PolicyService } from '../../core/services/policy.service';
import { NavigationStateService } from '../../core/services/navigation-state.service';

const MODULE_ICON = `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4"/></svg>`;

const PAGE_ICONS: Record<string, string> = {
  '/brands': `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4"/></svg>`,
  '/characters': `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"/></svg>`,
  '/published': `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z"/></svg>`,
};

const MODULE_GROUPS: Record<string, string> = {
  content: 'Студія',
  system: 'Інфраструктура',
};

const CONTENT_ORDER = ['/brands', '/characters', '/published'];

interface V3NavItem {
  id: string;
  path: string;
  label: string;
  icon: string;
  exact?: boolean;
  /** Additional v3 route prefixes (aliases) that should highlight this nav item (#554). */
  matchPaths?: string[];
  adminOnly?: boolean;
  /** Shown only to non-admin roles (e.g. moderator Workflows entry without admin Pipeline tabs). */
  moderatorOnly?: boolean;
}

interface V3SidebarGroup {
  label: string;
  items: V3NavItem[];
}

const PANEL_API_MAJOR = 1;

@Component({
  selector: 'app-v3-sidebar',
  standalone: true,
  imports: [CommonModule, RouterModule, TPipe],
  template: `
    <aside
      class="flex flex-col h-full z-30 flex-shrink-0 border-r transition-all duration-200"
      style="background-color: var(--v3-surface); border-color: var(--v3-border);"
      [class.w-64]="!collapsed"
      [class.w-16]="collapsed"
      [class.fixed]="isMobile"
      [class.relative]="!isMobile"
      [class.-translate-x-full]="isMobile && collapsed"
      [class.translate-x-0]="!collapsed"
      role="navigation"
      aria-label="{{ 'Навігація' | t }}">

      <!-- Brand header -->
      <div class="flex items-center h-16 px-4 border-b flex-shrink-0" style="border-color: var(--v3-border)">
        <a routerLink="/v3" class="flex items-center gap-3 overflow-hidden">
          <div class="w-9 h-9 rounded-xl flex items-center justify-center text-white font-bold text-base flex-shrink-0 shadow-xs"
               style="background: linear-gradient(135deg, var(--v3-accent), #0b6e50)">V</div>
          <div class="whitespace-nowrap transition-opacity duration-200"
               [class.opacity-0]="collapsed"
               [class.w-0]="collapsed"
               [class.overflow-hidden]="collapsed">
            <div class="text-sm font-bold tracking-wide uppercase" style="color: var(--v3-text)">Vertep</div>
            <div class="text-[11px] leading-tight" style="color: var(--v3-text-subtle)">Paper Workspace v3</div>
          </div>
        </a>
      </div>

      <!-- Reorganized compact navigation -->
      <nav class="flex-1 overflow-y-auto overflow-x-hidden py-4 px-2.5 space-y-4" data-testid="v3-sidebar-nav">
        @for (group of visibleGroups(); track group.label) {
          <div class="space-y-1">
            @if (group.label) {
              <div [attr.data-testid]="'v3-nav-group-' + group.label"
                   class="px-3 pt-1 pb-1 text-[11px] font-semibold uppercase tracking-wider transition-all duration-200"
                   style="color: var(--v3-text-subtle)"
                   [class.opacity-0]="collapsed"
                   [class.w-0]="collapsed"
                   [class.overflow-hidden]="collapsed"
                   [class.invisible]="collapsed">
                {{ group.label | t }}
              </div>
            }
            @for (item of group.items; track item.id) {
              <a [routerLink]="item.path"
                 [class.v3-nav-active]="isItemActive(item)"
                 [attr.aria-current]="isItemActive(item) ? 'page' : null"
                 class="group flex items-center gap-3 px-3 py-2.5 text-sm font-medium rounded-xl transition-colors"
                 style="color: var(--v3-text-muted)"
                 [attr.data-testid]="'v3-nav-' + item.id"
                 [attr.aria-label]="collapsed ? (item.label | t) : null"
                 [title]="collapsed ? (item.label | t) : ''">
                <span class="flex-shrink-0 w-5 h-5" [innerHTML]="item.icon"></span>
                <span class="whitespace-nowrap truncate transition-opacity duration-200"
                      [class.opacity-0]="collapsed"
                      [class.w-0]="collapsed"
                      [class.overflow-hidden]="collapsed">
                  {{ item.label | t }}
                </span>
              </a>
            }
          </div>
        }
      </nav>

      <!-- Version footer -->
      <div class="p-4 border-t flex-shrink-0 overflow-hidden" style="border-color: var(--v3-border)">
        <div class="text-xs whitespace-nowrap transition-opacity duration-200"
             style="color: var(--v3-text-subtle)"
             [class.opacity-0]="collapsed"
             [class.hidden]="collapsed">
          Vertep v{{ runtimeVersion() || '...' }}
          @if (panelVersion() && panelVersion() !== runtimeVersion()) {
            <div data-testid="v3-panel-version" [attr.title]="panelCommit() || null">
              {{ 'Панель' | t }} v{{ panelVersion() }}@if (panelCommit()) { · {{ panelCommit()!.slice(0, 12) }} }
            </div>
          }
          @if (apiIncompatible()) {
            <div class="mt-1 whitespace-normal font-medium" style="color: var(--v3-amber)" data-testid="v3-panel-incompatible">
              {{ 'Панель несумісна з цією версією CORE. Оновіть сторінку або панель.' | t }}
            </div>
          }
        </div>
      </div>
    </aside>
  `,
  styles: [`
    .v3-nav-active {
      background-color: var(--v3-accent-soft) !important;
      color: var(--v3-accent) !important;
      font-weight: 600;
    }
    a:hover:not(.v3-nav-active) {
      background-color: var(--v3-surface-subtle);
      color: var(--v3-text) !important;
    }
  `],
})
export class V3SidebarComponent implements OnInit, OnDestroy {
  private readonly baseGroups: V3SidebarGroup[] = [
    {
      label: '',
      items: [
        {
          id: 'dashboard',
          path: '/v3',
          label: 'Дашборд',
          exact: true,
          icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M4 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2V6zM14 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2zM14 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z"/></svg>`,
        },
      ],
    },
    {
      label: 'Студія',
      items: [
        {
          id: 'jobs',
          path: '/v3/jobs',
          matchPaths: ['/v3/jobs', '/v3/queue'],
          label: 'Завдання і черга',
          icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2"/></svg>`,
        },
      ],
    },
    {
      label: 'Інфраструктура',
      items: [
        {
          id: 'workers',
          path: '/v3/workers',
          label: 'Вузли',
          icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M5 12h14M5 12a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v4a2 2 0 01-2 2M5 12a2 2 0 00-2 2v4a2 2 0 002 2h14a2 2 0 002-2v-4a2 2 0 00-2-2"/></svg>`,
        },
        {
          id: 'monitor',
          path: '/v3/monitor',
          matchPaths: ['/v3/monitor', '/v3/health', '/v3/alerts', '/v3/logs', '/v3/real-tests'],
          label: 'Моніторинг',
          icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"/></svg>`,
        },
        {
          id: 'modules',
          path: '/v3/modules',
          label: 'Модулі та обслуговування',
          exact: true,
          adminOnly: true,
          icon: MODULE_ICON,
        },
      ],
    },
    {
      label: 'Керування',
      items: [
        {
          id: 'pipeline',
          path: '/v3/pipeline',
          matchPaths: ['/v3/pipeline', '/v3/workflows'],
          label: 'Конвеєр',
          adminOnly: true,
          icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M12 6V4m0 2a2 2 0 100 4m0-4a2 2 0 110 4m-6 8a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4m6 6v10m6-2a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4"/></svg>`,
        },
        {
          id: 'settings',
          path: '/v3/settings',
          label: 'Налаштування',
          adminOnly: true,
          icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/></svg>`,
        },
      ],
    },
  ];

  private readonly moderatorWorkflowsItem: V3NavItem = {
    id: 'workflows',
    path: '/v3/workflows',
    matchPaths: ['/v3/workflows', '/v3/pipeline'],
    label: 'Шаблони обробки',
    moderatorOnly: true,
    icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M12 6.25v11.5M6 12h12M6 12l5 5m0 0l5-5"/></svg>`,
  };

  private readonly baseLabels = new Set(
    this.baseGroups.flatMap(group => group.items.map(item => item.label)),
  );

  currentPath = signal('');
  runtimeVersion = signal<string | null>(null);
  panelVersion = signal<string | null>(null);
  panelCommit = signal<string | null>(null);
  apiIncompatible = signal(false);
  private statusTimer?: ReturnType<typeof setInterval>;
  private routerSub?: Subscription;

  moduleItems = computed<(V3NavItem & { group: string; rawPath: string })[]>(() =>
    this.navState.moduleItems().map(item => {
      const isExtension = !MODULE_GROUPS[item.group];
      const label = isExtension && this.baseLabels.has(item.label)
        ? `${item.label} (модуль)`
        : item.label;
      return {
        id: 'module-' + item.module_id + '-' + item.id,
        rawPath: item.path,
        path: item.path.startsWith('/v3') ? item.path : `/v3${item.path.startsWith('/') ? '' : '/'}${item.path}`,
        label,
        icon: PAGE_ICONS[item.path] || MODULE_ICON,
        adminOnly: item.admin_only,
        group: item.group,
      };
    }),
  );

  visibleGroups = computed(() => {
    const isAdmin = this.policy.userRole() === 'admin';
    const modules = this.moduleItems();
    const labItem: V3NavItem = { id: 'lab', path: '/v3/lab', label: 'Лабораторія', icon: MODULE_ICON, adminOnly: true };

    const groupsWithModules = this.baseGroups.map(group => {
      const rank = (rawPath: string) => {
        const idx = CONTENT_ORDER.indexOf(rawPath);
        return idx < 0 ? 99 : idx;
      };
      const own = modules
        .filter(item => MODULE_GROUPS[item.group] === group.label)
        .sort((a, b) => rank(a.rawPath) - rank(b.rawPath));
      const items = [...group.items, ...own];
      if (group.label === 'Студія' && !isAdmin) {
        items.push(this.moderatorWorkflowsItem);
      }
      if (group.label === 'Інфраструктура' && this.navState.labConfigured()) {
        items.push(labItem);
      }
      return { label: group.label, items };
    });

    const extensions = modules.filter(item => !MODULE_GROUPS[item.group]);
    const allGroups = extensions.length
      ? [...groupsWithModules, { label: 'Розширення модулів', items: extensions }]
      : groupsWithModules;

    return allGroups
      .map(group => ({
        label: group.label,
        items: group.items.filter(item => (!item.adminOnly || isAdmin) && (!item.moderatorOnly || !isAdmin)),
      }))
      .filter(group => group.items.length > 0);
  });

  get collapsed(): boolean { return this.sidebarService.collapsed(); }
  get isMobile(): boolean { return window.innerWidth < 1024; }

  constructor(
    private sidebarService: SidebarService,
    private systemApi: SystemApiService,
    private policy: PolicyService,
    private navState: NavigationStateService,
    private router: Router,
  ) {}

  ngOnInit(): void {
    this.currentPath.set(this.normalizePath(this.router.url));
    this.routerSub = this.router.events
      .pipe(filter(event => event instanceof NavigationEnd))
      .subscribe(event => this.currentPath.set(this.normalizePath((event as NavigationEnd).urlAfterRedirects)));

    fetch('/ui-version.json', { cache: 'no-store' })
      .then(response => (response.ok ? response.json() : null))
      .then(identity => {
        this.panelVersion.set(identity?.version || null);
        this.panelCommit.set(identity?.commit_sha || null);
      })
      .catch(() => { this.panelVersion.set(null); });

    this.checkCore();
    this.navState.refreshAll();
    this.statusTimer = setInterval(() => { this.checkCore(); this.navState.refreshAll(); }, 30_000);
  }

  ngOnDestroy(): void {
    if (this.statusTimer) clearInterval(this.statusTimer);
    this.routerSub?.unsubscribe();
  }

  isItemActive(item: V3NavItem): boolean {
    const path = this.currentPath();
    if (!path) return false;
    const targets = item.matchPaths?.length ? item.matchPaths : [item.path];
    if (item.exact) {
      return targets.includes(path);
    }
    return targets.some(prefix => path === prefix || path.startsWith(prefix + '/'));
  }

  private normalizePath(url: string): string {
    const withoutQuery = (url || '/').split('?')[0].split('#')[0];
    return withoutQuery.replace(/\/+$/, '') || '/';
  }

  private checkCore(): void {
    this.systemApi.status().subscribe({
      next: s => {
        this.runtimeVersion.set(s.version || null);
        this.apiIncompatible.set(s.api_major !== undefined && s.api_major !== PANEL_API_MAJOR);
      },
      error: () => { this.runtimeVersion.set(null); },
    });
  }
}
