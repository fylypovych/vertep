import { ChangeDetectorRef, Component, HostListener, OnDestroy, OnInit, computed, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { NavigationEnd, Router, RouterModule } from '@angular/router';
import { filter, Subscription } from 'rxjs';
import { TPipe } from '../../core/i18n/t.pipe';
import { LanguageSwitchComponent } from '../../shared/language-switch.component';
import { SidebarService } from '../../core/services/sidebar.service';
import { UiVersionService } from '../../core/services/ui-version.service';
import { AuthApiService } from '../../core/api/auth.api';
import { SystemApiService } from '../../core/api/system.api';
import { PolicyService, SystemMode } from '../../core/services/policy.service';
import { NavigationStateService } from '../../core/services/navigation-state.service';

const V3_PAGE_TITLES: Record<string, { title: string; subtitle: string }> = {
  '':           { title: 'Дашборд',                  subtitle: 'Огляд системи Vertep' },
  'jobs':       { title: 'Завдання і черга',          subtitle: 'Управління завданнями та чергою' },
  'queue':      { title: 'Завдання і черга',          subtitle: 'Управління завданнями та чергою' },
  'brands':     { title: 'Бренди',                   subtitle: 'Бренди, персонажі та канали' },
  'characters': { title: 'Персонажі',                subtitle: 'Профілі та конфігурація персонажів' },
  'published':  { title: 'Опубліковане',             subtitle: 'Опублікований контент' },
  'workers':    { title: 'Вузли',                    subtitle: 'Вузли та їх стан' },
  'monitor':    { title: 'Моніторинг',               subtitle: 'Стан системи, алерти та логи' },
  'health':     { title: 'Моніторинг',               subtitle: 'Стан системи, алерти та логи' },
  'alerts':     { title: 'Моніторинг',               subtitle: 'Стан системи, алерти та логи' },
  'logs':       { title: 'Моніторинг',               subtitle: 'Стан системи, алерти та логи' },
  'real-tests': { title: 'Моніторинг',               subtitle: 'Сценарії реального стенда' },
  'modules':    { title: 'Модулі та обслуговування', subtitle: 'Каталог модулів, оновлення та бекапи' },
  'pipeline':   { title: 'Конвеєр',                  subtitle: 'Шаблони обробки, моделі та движок відео' },
  'workflows':  { title: 'Шаблони обробки',          subtitle: 'Workflow обробки контенту' },
  'settings':   { title: 'Налаштування',             subtitle: 'Системні налаштування' },
  'lab':        { title: 'Лабораторія',              subtitle: 'Автономна лабораторія' },
  'profile':    { title: 'Профіль',                  subtitle: 'Профіль користувача' },
};

@Component({
  selector: 'app-v3-navbar',
  standalone: true,
  imports: [CommonModule, RouterModule, TPipe, LanguageSwitchComponent],
  template: `
    <header class="h-16 border-b flex items-center justify-between px-4 lg:px-6 flex-shrink-0"
            style="background-color: var(--v3-bg); border-color: var(--v3-border);">
      <!-- Left: menu toggle + title -->
      <div class="flex items-center gap-3 min-w-0">
        <button type="button" (click)="toggleSidebar()"
                class="p-2 rounded-lg transition-colors hover:bg-black/5"
                style="color: var(--v3-text-muted)"
                aria-label="{{ 'Перемикач меню' | t }}">
          <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16"/>
          </svg>
        </button>
        <div class="min-w-0">
          <h1 class="text-base font-semibold leading-tight truncate" style="color: var(--v3-text)" data-testid="v3-header-title">
            {{ title() | t }}
          </h1>
          <p class="hidden sm:block text-xs leading-tight truncate" style="color: var(--v3-text-muted)" data-testid="v3-header-subtitle">
            {{ subtitle() | t }}
          </p>
        </div>
      </div>

      <!-- Right: system state + language + v2 switcher + profile menu -->
      <div class="flex items-center gap-2 flex-shrink-0">
        <div class="hidden sm:flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium border"
             [style.background-color]="systemOk ? 'var(--v3-accent-soft)' : 'var(--v3-amber-soft)'"
             [style.color]="systemOk ? 'var(--v3-accent)' : 'var(--v3-amber)'"
             [style.border-color]="'transparent'">
          <span class="w-1.5 h-1.5 rounded-full"
                [style.background-color]="systemOk ? 'var(--v3-accent)' : 'var(--v3-amber)'"></span>
          {{ systemState | t }}
        </div>

        <app-language-switch />

        <button type="button"
                (click)="switchToV2()"
                data-testid="switch-to-v2"
                class="px-2.5 py-1.5 rounded-full border text-xs font-semibold transition-colors hover:bg-white"
                style="border-color: var(--v3-border-strong); color: var(--v3-text-muted); background-color: var(--v3-surface-subtle)"
                [title]="'Повернутися на v2' | t">
          v2
        </button>

        <div class="relative v3-profile-menu-container">
          <button type="button" (click)="toggleProfileMenu()"
                  class="flex items-center gap-2 px-2 py-1 rounded-full transition-colors hover:bg-white/80"
                  aria-label="{{ 'Меню користувача' | t }}"
                  [attr.aria-expanded]="showProfileMenu"
                  aria-haspopup="true">
            <div class="w-8 h-8 rounded-full flex items-center justify-center font-semibold text-xs select-none"
                 style="background-color: var(--v3-accent-soft); color: var(--v3-accent)">
              {{ userInitialSig() }}
            </div>
            <span class="hidden md:block text-sm font-medium" style="color: var(--v3-text)">{{ roleLabel | t }}</span>
            <svg class="w-4 h-4" style="color: var(--v3-text-muted)" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
            </svg>
          </button>

          @if (showProfileMenu) {
            <div class="v3-paper-card absolute right-0 mt-2 w-56 py-1 z-50">
              <div class="px-4 py-3 border-b" style="border-color: var(--v3-border)">
                <p class="text-sm font-semibold" style="color: var(--v3-text)">{{ roleFullLabel | t }}</p>
                <p class="text-xs truncate" style="color: var(--v3-text-muted)">{{ usernameSig() || userInitialSig() }}</p>
              </div>
              <a routerLink="/v3/profile" (click)="showProfileMenu = false"
                 class="flex items-center gap-3 px-4 py-2.5 text-sm hover:bg-[#faf9f6] transition-colors"
                 style="color: var(--v3-text)">
                {{ 'Профіль' | t }}
              </a>
              <a routerLink="/v3/profile" fragment="password" (click)="showProfileMenu = false"
                 class="flex items-center gap-3 px-4 py-2.5 text-sm hover:bg-[#faf9f6] transition-colors"
                 style="color: var(--v3-text)">
                {{ 'Змінити пароль' | t }}
              </a>
              <hr class="my-1" style="border-color: var(--v3-border)">
              <button type="button" (click)="logout()"
                      class="flex items-center gap-3 w-full px-4 py-2.5 text-sm text-left hover:bg-[#faf9f6] transition-colors"
                      style="color: var(--v3-danger)">
                {{ 'Вийти' | t }}
              </button>
            </div>
          }
        </div>
      </div>
    </header>
  `,
})
export class V3NavbarComponent implements OnInit, OnDestroy {
  private readonly currentUrl = signal('/v3');
  private readonly pageInfo = computed(() => {
    const cleaned = this.currentUrl().replace(/^\/v3\/?/, '').split('?')[0].split('#')[0];
    const segments = cleaned.split('/').filter(Boolean);
    const first = segments[0] || '';
    if (first === 'modules' && segments[1]) {
      const moduleId = decodeURIComponent(segments[1]);
      const navItem = this.navState.moduleItems().find(item => item.module_id === moduleId);
      return { title: navItem?.label || moduleId, subtitle: 'Розширення модуля' };
    }
    return V3_PAGE_TITLES[first] ?? V3_PAGE_TITLES[''];
  });

  readonly title = computed(() => this.pageInfo().title);
  readonly subtitle = computed(() => this.pageInfo().subtitle);
  readonly userInitialSig = signal('A');
  readonly usernameSig = signal('');
  systemState = 'Невизначений';
  systemOk = false;
  showProfileMenu = false;

  private subs = new Subscription();

  constructor(
    private router: Router,
    private sidebarService: SidebarService,
    private uiVersion: UiVersionService,
    private auth: AuthApiService,
    private system: SystemApiService,
    private policy: PolicyService,
    private navState: NavigationStateService,
    private cdr: ChangeDetectorRef,
  ) {}

  ngOnInit(): void {
    this.currentUrl.set(this.router.url);
    this.subs.add(
      this.router.events.pipe(filter(e => e instanceof NavigationEnd))
        .subscribe(e => this.currentUrl.set((e as NavigationEnd).urlAfterRedirects)),
    );
    this.loadSystemState();
    this.loadUserProfile();
    const id = setInterval(() => { this.loadSystemState(); this.policy.refresh(); }, 30_000);
    this.subs.add(new Subscription(() => clearInterval(id)));
  }

  ngOnDestroy(): void { this.subs.unsubscribe(); }

  @HostListener('document:click', ['$event'])
  onDocumentClick(event: MouseEvent): void {
    const target = event.target as HTMLElement;
    if (!target.closest('.v3-profile-menu-container')) {
      this.showProfileMenu = false;
    }
  }

  toggleSidebar(): void { this.sidebarService.toggle(); }
  toggleProfileMenu(): void { this.showProfileMenu = !this.showProfileMenu; }

  switchToV2(): void {
    this.uiVersion.setVersion('v2');
    const target = this.uiVersion.toV2Route(this.router.url);
    this.router.navigate([target.path], { queryParams: target.queryParams, fragment: target.fragment });
  }

  logout(): void {
    this.auth.deleteSession().subscribe({
      next: () => this.router.navigate(['/login']),
      error: () => this.router.navigate(['/login']),
    });
  }

  get roleLabel(): string { return this.policy.userRole() === 'admin' ? 'Адмін' : 'Модератор'; }
  get roleFullLabel(): string { return this.policy.userRole() === 'admin' ? 'Адміністратор' : 'Модератор'; }

  private loadSystemState(): void {
    this.system.status().subscribe({
      next: s => {
        const state = (s.system?.state?.toUpperCase() ?? 'UNAVAILABLE') as SystemMode;
        const labels: Record<string, string> = {
          NORMAL: 'Нормальний', OK: 'Працює', HEALTHY: 'Працює',
          MAINTENANCE: 'Обслуговування', UPDATING: 'Оновлення',
          EMERGENCY: 'Аварія', FAILED: 'Помилка', ERROR: 'Помилка',
          RECOVERING: 'Відновлення', READ_ONLY: 'Тільки читання',
          UNAVAILABLE: 'Недоступний',
        };
        this.systemState = labels[state] ?? 'Невідомий';
        this.systemOk = ['NORMAL', 'OK', 'HEALTHY'].includes(state);
        this.cdr.markForCheck();
      },
      error: () => {
        this.systemState = 'Недоступний';
        this.systemOk = false;
        this.cdr.markForCheck();
      },
    });
  }

  private loadUserProfile(): void {
    this.auth.getUserProfile().subscribe({
      next: p => {
        this.userInitialSig.set((p.user?.charAt(0) || 'A').toUpperCase());
        this.usernameSig.set(p.display_name || p.user || '');
        this.policy.userRole.set(p.role);
      },
      error: () => { this.userInitialSig.set('A'); },
    });
  }
}
