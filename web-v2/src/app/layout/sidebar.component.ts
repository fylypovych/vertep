import { TPipe } from '../core/i18n/t.pipe';
import { Component, OnDestroy, OnInit, computed, inject, signal } from '@angular/core';
import { RouterModule } from '@angular/router';
import { HttpClient, HttpContext } from '@angular/common/http';
import { BACKGROUND_REQUEST } from '../core/interceptors/auth.interceptor';
import { CommonModule } from '@angular/common';
import { SidebarService } from '../core/services/sidebar.service';
import { SystemApiService } from '../core/api/system.api';
import { PolicyService } from '../core/services/policy.service';
import { ModulesApiService } from '../core/api/modules.api';

const MODULE_ICON = `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4"/></svg>`;

/** Icons of the panel pages that official modules declare in their menu entries. */
const PAGE_ICONS: Record<string, string> = {
  '/published': `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z"/></svg>`,
  '/characters': `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14.828 14.828a4 4 0 01-5.656 0M9 10h.01M15 10h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>`,
  '/brands': `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v14m14 0v2a2 2 0 002 2h2M5 19h14M5 19l1.5-1.5M5 19l-1.5 1.5M12 12a3 3 0 100-6 3 3 0 000 6z"/></svg>`,
};
/** Where a module's menu entry goes (`group` of its manifest entry). */
const MODULE_GROUPS: Record<string, string> = { content: 'Контент', system: 'Система' };

interface NavItem {
  id: string;
  path: string;
  label: string;
  icon: string;
  exact?: boolean;
  queryParams?: Record<string, string>;
  adminOnly?: boolean;
  /** A built-in page of a service without its own menu entry: shown only while that service answers. */
  module?: string;
}

interface SidebarGroup {
  label: string;
  items: NavItem[];
}

/** CORE API major this panel build is written for (#504 AC3). */
const PANEL_API_MAJOR = 1;

@Component({
  selector: 'app-sidebar',
  standalone: true,
  imports: [TPipe, RouterModule, CommonModule],
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
      aria-label="{{ 'Навігація' | t }}">

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
          @if (group.label) {
          <div [attr.data-testid]="'nav-group-' + group.label" class="px-3 py-1.5 text-xs font-medium text-slate-500 uppercase transition-all duration-200"
               [class.opacity-0]="collapsed"
               [class.w-0]="collapsed"
               [class.overflow-hidden]="collapsed"
               [class.invisible]="collapsed">
            {{ (group.label) | t }}
          </div>
          }
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
                {{ (item.label) | t }}
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
          Vertep v{{ runtimeVersion() || '...' }}
          @if (panelVersion() && panelVersion() !== runtimeVersion()) {
            <div data-testid="panel-version" [attr.title]="panelCommit() || null">{{ 'Панель' | t }} v{{ panelVersion() }}@if (panelCommit()) { · {{ panelCommit()!.slice(0, 12) }} }</div>
          }
          @if (apiIncompatible()) {
            <div class="text-amber-700 whitespace-normal" data-testid="panel-incompatible">{{ 'Панель несумісна з цією версією CORE. Оновіть сторінку або панель.' | t }}</div>
          }
        </div>
      </div>
    </aside>
  `,
})
export class SidebarComponent implements OnInit, OnDestroy {
  navGroups: SidebarGroup[] = [
    {
      // #518: the overview stands first, above every group (no group title).
      label: '',
      items: [
        {
          id: 'dashboard',
          path: '/',
          label: 'Дашборд',
          exact: true,
          icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2V6zM14 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2zM14 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z"/></svg>`,
        },
      ],
    },
    {
      label: 'Контент',
      items: [
        {
          id: 'jobs',
          path: '/jobs',
          label: 'Завдання',
          icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2"/></svg>`,
        },
        {
          id: 'queue',
          path: '/queue',
          label: 'Черга виконання',
          icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10"/></svg>`,
        },
        {
          id: 'workflows',
          path: '/workflows',
          label: 'Шаблони обробки',
          icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6.25v11.5M6 12h12M6 12l5 5m0 0l5-5"/></svg>`,
        },
      ],
    },
    {
      label: 'Система',
      items: [
        { id: 'health', path: '/health', label: 'Стан системи', icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>` },
        {
          id: 'alerts',
          path: '/alerts',
          label: 'Алерти',
          icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"/></svg>`,
        },
        { id: 'logs', path: '/logs', label: 'Логи', icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"/></svg>` },
        { id: 'settings-integrations', path: '/settings', label: 'Стан підключень', queryParams: { tab: 'integrations' }, icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 10-5.656-5.656l-1.1 1.1"/></svg>`, adminOnly: true },
        {
          id: 'workers',
          path: '/workers',
          label: 'Вузли',
          icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z"/></svg>`,
        },
        { id: 'modules', path: '/modules', label: 'Модулі', exact: true, icon: MODULE_ICON, adminOnly: true },
        { id: 'settings-update', path: '/settings', label: 'Оновлення', queryParams: { tab: 'update' }, icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>`, adminOnly: true },
        { id: 'settings-backup', path: '/settings', label: 'Бекапи', queryParams: { tab: 'backup' }, module: 'backup', icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 8h14M5 8a2 2 0 110-4h14a2 2 0 110 4M5 8v10a2 2 0 002 2h10a2 2 0 002-2V8m-9 4h4"/></svg>`, adminOnly: true },
        {
          id: 'real-tests',
          path: '/real-tests',
          label: 'Реальні тести',
          icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>`,
          adminOnly: true,
        },
      ],
    },
    {
      label: 'Налаштування',
      items: [
        {
          id: 'settings-system',
          path: '/settings',
          label: 'Загальне',
          queryParams: { tab: 'system' },
          icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37.996.608 2.296.07 2.572-1.065z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/></svg>`,
          adminOnly: true,
        },
        { id: 'settings-publishing', path: '/settings', label: 'Публікація', queryParams: { tab: 'publishing' }, icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 0-8 0-8s0 0 0 0c0-4.418 4.03-8 9-8s9 3.582 9 8z"/></svg>`, adminOnly: true },
        { id: 'settings-telegram', path: '/settings', label: 'Telegram-бот', queryParams: { tab: 'telegram' }, icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 0-8 0-8s0 0 0 0c0-4.418 4.03-8 9-8s9 3.582 9 8z"/></svg>`, adminOnly: true },
        { id: 'settings-models', path: '/settings', label: 'Моделі', queryParams: { tab: 'models' }, icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19.428 15.428a2 2 0 00-1.022-.547l-2.387-.477a6 6 0 00-3.86.517l-.318.158a6 6 0 01-3.86.517L6.05 15.21a2 2 0 00-1.806.547M8 4h8l-1 1v5.172a2 2 0 00.586 1.414l5 5c1.26 1.26.367 3.414-1.415 3.414H4.828c-1.782 0-2.674-2.154-1.414-3.414l5-5A2 2 0 009 10.172V5L8 4z"/></svg>`, adminOnly: true },
        { id: 'settings-video-engine', path: '/settings', label: 'Движок відео', queryParams: { tab: 'video-engine' }, icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19.428 15.428a2 2 0 00-1.022-.547l-2.387-.477a6 6 0 00-3.86.517l-.318.158a6 6 0 01-3.86.517L6.05 15.21a2 2 0 00-1.806.547M8 4h8l-1 1v5.172a2 2 0 00.586 1.414l5 5c1.26 1.26.367 3.414-1.415 3.414H4.828c-1.782 0-2.674-2.154-1.414-3.414l5-5A2 2 0 009 10.172V5L8 4z"/></svg>`, adminOnly: true },
        { id: 'settings-workflow-mode', path: '/settings', label: 'Режим workflow', queryParams: { tab: 'workflow-mode' }, icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19.428 15.428a2 2 0 00-1.022-.547l-2.387-.477a6 6 0 00-3.86.517l-.318.158a6 6 0 01-3.86.517L6.05 15.21a2 2 0 00-1.806.547M8 4h8l-1 1v5.172a2 2 0 00.586 1.414l5 5c1.26 1.26.367 3.414-1.415 3.414H4.828c-1.782 0-2.674-2.154-1.414-3.414l5-5A2 2 0 009 10.172V5L8 4z"/></svg>`, adminOnly: true },
        { id: 'settings-branding', path: '/settings', label: 'Брендинг', queryParams: { tab: 'branding' }, icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12z"/></svg>`, adminOnly: true },
        { id: 'settings-secrets', path: '/settings', label: 'Ключі інтеграцій', queryParams: { tab: 'secrets' }, icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z"/></svg>`, adminOnly: true },
        { id: 'settings-security', path: '/settings', label: 'Безпека', queryParams: { tab: 'security' }, icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z"/></svg>`, adminOnly: true },
        { id: 'settings-users', path: '/settings', label: 'Користувачі', queryParams: { tab: 'users' }, icon: `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a4 4 0 00-5-3.87M9 20H4v-2a4 4 0 015-3.87m8-4.13a4 4 0 11-8 0 4 4 0 018 0z"/></svg>`, adminOnly: true },
      ],
    },
  ];

  /** Menu entries READY modules declare; a module that is not READY has none (owner, 23.11.2026). */
  moduleItems = signal<(NavItem & { group: string })[]>([]);
  /** #491 L3: «Лабораторія» for an administrator, only when CORE has LAB_PANEL_URL. */
  labConfigured = signal(false);
  /** Built-in pages whose service answers (Бекапи): shown only then. */
  activeModules = signal<Set<string>>(new Set());

  visibleGroups = computed(() => {
    const isAdmin = this.policy.userRole() === 'admin';
    const active = this.activeModules();
    const modules = this.moduleItems();
    const lab: NavItem = { id: 'lab', path: '/lab', label: 'Лабораторія', icon: MODULE_ICON, adminOnly: true };
    const navGroups = this.labConfigured()
      ? this.navGroups.map(group => group.label === 'Система' ? { ...group, items: [...group.items, lab] } : group)
      : this.navGroups;
    const extensions = modules.filter(item => !MODULE_GROUPS[item.group]);
    const withModules = navGroups.map(group => {
      // #518 order of the official pages (PAGE_ICONS); other entries follow them.
      const rank = (path: string) => { const at = Object.keys(PAGE_ICONS).indexOf(path); return at < 0 ? 99 : at; };
      const own = modules.filter(item => MODULE_GROUPS[item.group] === group.label)
        .sort((a, b) => rank(a.path) - rank(b.path));
      if (!own.length) return group;
      // Module pages stand before the processing templates, where the built-in entries were.
      const at = group.items.findIndex(item => item.id === 'workflows');
      const items = [...group.items];
      items.splice(at < 0 ? items.length : at, 0, ...own);
      return { ...group, items };
    });
    const groups = extensions.length ? [...withModules, { label: 'Розширення модулів', items: extensions }] : withModules;
    return groups
      .map(group => ({
        label: group.label,
        items: group.items.filter(item => (!item.adminOnly || isAdmin) && (!item.module || active.has(item.module))),
      }))
      .filter(group => group.items.length > 0);
  });

  get collapsed(): boolean {
    return this.sidebarService.collapsed();
  }

  get isMobile(): boolean {
    return window.innerWidth < 1024;
  }

  runtimeVersion = signal<string | null>(null);
  /** #504: build of the separately deployed panel and its API compatibility. */
  panelVersion = signal<string | null>(null);
  apiIncompatible = signal(false);
  panelCommit = signal<string | null>(null);
  private statusTimer?: ReturnType<typeof setInterval>;

  constructor(private sidebarService: SidebarService, private systemApi: SystemApiService, private policy: PolicyService,
              private modulesApi: ModulesApiService, private http: HttpClient) {}

  ngOnInit(): void {
    fetch('/ui-version.json', { cache: 'no-store' })
      .then((response) => (response.ok ? response.json() : null))
      .then((identity) => { this.panelVersion.set(identity?.version || null); this.panelCommit.set(identity?.commit_sha || null); })
      .catch(() => { this.panelVersion.set(null); });
    this.checkCore();
    // #504 AC3: CORE may restart or be updated while the panel stays open; the
    // version and API compatibility are re-checked, so a reconnect clears the
    // state and an incompatible CORE is reported without reloading the page.
    // A module activated or deactivated meanwhile changes the menu without a reload.
    this.statusTimer = setInterval(() => { this.checkCore(); this.loadModuleMenu(); }, 30_000);
    // A background probe: a refusal (moderator, no session yet) never redirects or reports an error.
    this.http.get<{ url: string | null }>('/api/lab/panel',
        { context: new HttpContext().set(BACKGROUND_REQUEST, true) }).subscribe({
      next: ({ url }) => this.labConfigured.set(!!url),
      error: () => this.labConfigured.set(false),  // a moderator gets 403: no entry
    });
    this.loadModuleMenu();
  }

  private loadModuleMenu(): void {
    // «Бекапи» is shown while the backup service answers — deployed as a module or as a role.
    // Only the administrator has the entry; a refusal or an unreachable service hides it.
    if (this.policy.userRole() === 'admin') {
      this.http.get<{ available: boolean }>('/api/system/backups/available',
          { context: new HttpContext().set(BACKGROUND_REQUEST, true) }).subscribe({
        next: ({ available }) => this.activeModules.set(new Set(available ? ['backup'] : [])),
        error: () => this.activeModules.set(new Set()),
      });
    }
    this.modulesApi.navigation().subscribe(items => this.moduleItems.set(items.map(item => ({
      id: 'module-' + item.module_id + '-' + item.id,
      path: item.path,
      label: item.label,
      icon: PAGE_ICONS[item.path] || MODULE_ICON,
      adminOnly: item.admin_only,
      group: item.group,
    }))));
  }

  ngOnDestroy(): void {
    if (this.statusTimer) clearInterval(this.statusTimer);
  }

  private checkCore(): void {
    this.systemApi.status().subscribe({
      next: (s) => {
        this.runtimeVersion.set(s.version || null);
        this.apiIncompatible.set(s.api_major !== undefined && s.api_major !== PANEL_API_MAJOR);
      },
      error: () => { this.runtimeVersion.set(null); },
    });
  }
}
