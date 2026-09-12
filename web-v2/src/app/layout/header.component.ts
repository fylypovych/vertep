import { Component, OnInit, OnDestroy, HostListener } from '@angular/core';
import { Router, NavigationEnd, RouterModule } from '@angular/router';
import { CommonModule } from '@angular/common';
import { filter, Subscription } from 'rxjs';
import { SidebarService } from '../core/services/sidebar.service';
import { ThemeService } from '../core/services/theme.service';
import { VertepApiService } from '../core/api.service';
import { PolicyService, UserRole, SystemMode } from '../core/services/policy.service';

const PAGE_TITLES: Record<string, { title: string; subtitle: string }> = {
  '':           { title: 'Дашборд',      subtitle: 'Огляд системи Vertep' },
  'jobs':       { title: 'Завдання',      subtitle: 'Управління завданнями' },
  'workers':    { title: 'Воркери',       subtitle: 'Вузли та їх стан' },
  'characters': { title: 'Персонажі',     subtitle: 'Персонажі контенту' },
  'settings':   { title: 'Налаштування',  subtitle: 'Системні налаштування' },
  'profile':    { title: 'Профіль',       subtitle: 'Профіль користувача' },
};

@Component({
  selector: 'app-header',
  standalone: true,
  imports: [CommonModule, RouterModule],
  templateUrl: './header.component.html',
})
export class HeaderComponent implements OnInit, OnDestroy {
  title = 'Дашборд';
  subtitle = 'Огляд системи Vertep';
  systemState = 'Нормальний';
  systemOk = true;
  isDark = false;
  userInitial = 'A';
  userRole: UserRole = 'admin';
  showProfileMenu = false;
  systemReason: string | null = null;

  private subs = new Subscription();

  constructor(
    private router: Router,
    private sidebarService: SidebarService,
    private themeService: ThemeService,
    private api: VertepApiService,
    private policy: PolicyService,
  ) {}

  ngOnInit(): void {
    this.isDark = this.themeService.isDark;
    this.subs.add(this.themeService.dark$.subscribe(d => { this.isDark = d; }));
    this.updateTitle(this.router.url);
    this.subs.add(
      this.router.events.pipe(filter(e => e instanceof NavigationEnd))
        .subscribe(e => this.updateTitle((e as NavigationEnd).urlAfterRedirects))
    );
    this.loadSystemState();
    this.loadUserProfile();
    const id = setInterval(() => this.loadSystemState(), 30_000);
    this.subs.add(new Subscription(() => clearInterval(id)));
  }

  ngOnDestroy(): void { this.subs.unsubscribe(); }

  @HostListener('document:click', ['$event'])
  onDocumentClick(event: MouseEvent): void {
    const target = event.target as HTMLElement;
    if (!target.closest('.profile-menu-container')) {
      this.showProfileMenu = false;
    }
  }

  toggleSidebar(): void { this.sidebarService.toggle(); }
  toggleTheme(): void   { this.themeService.toggle(); }

  switchToV1(): void {
    document.cookie = 'vertep_ui=v1; path=/; max-age=31536000; SameSite=Lax';
    window.location.href = '/v1/';
  }

  toggleProfileMenu(): void {
    this.showProfileMenu = !this.showProfileMenu;
  }

  logout(): void {
    this.api.deleteSession().subscribe({
      next:  () => this.router.navigate(['/login']),
      error: () => this.router.navigate(['/login']),
    });
  }

  private updateTitle(url: string): void {
    const segment = url.replace(/^\//, '').split('?')[0].split('#')[0];
    const info = PAGE_TITLES[segment] ?? PAGE_TITLES[''];
    this.title    = info.title;
    this.subtitle = info.subtitle;
  }

  private loadSystemState(): void {
    this.api.getStatus().subscribe({
      next: (s) => {
        const state = (s.system?.state?.toUpperCase() ?? 'NORMAL') as SystemMode;
        const labels: Record<string, string> = {
          NORMAL: 'Нормальний', OK: 'Працює', HEALTHY: 'Працює',
          MAINTENANCE: 'Обслуговування', UPDATING: 'Оновлення',
          EMERGENCY: 'Аварія', FAILED: 'Помилка', ERROR: 'Помилка',
          RECOVERING: 'Відновлення', READ_ONLY: 'Тільки читання',
        };
        this.systemState = labels[state] ?? state;
        this.systemOk = ['NORMAL', 'OK', 'HEALTHY'].includes(state);
        this.systemReason = s.system?.reason || null;
      },
      error: () => { this.systemState = 'Недоступний'; this.systemOk = false; },
    });
  }

  private loadUserProfile(): void {
    this.api.getUserProfile().subscribe({
      next: (p) => {
        this.userInitial = (p.user?.charAt(0) || 'A').toUpperCase();
        this.userRole = p.role;
        this.policy.userRole.set(p.role);
      },
      error: () => { this.userInitial = 'A'; this.userRole = 'admin'; },
    });
  }

  getSystemStateReason(): string | null {
    return this.systemReason;
  }
}
