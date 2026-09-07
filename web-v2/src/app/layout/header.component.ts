import { Component, OnInit, OnDestroy } from '@angular/core';
import { Router, NavigationEnd, RouterModule } from '@angular/router';
import { CommonModule } from '@angular/common';
import { filter, Subscription } from 'rxjs';
import { SidebarService } from '../core/services/sidebar.service';
import { ThemeService } from '../core/services/theme.service';
import { VertepApiService } from '../core/api.service';

const PAGE_TITLES: Record<string, { title: string; subtitle: string }> = {
  '':           { title: 'Дашборд',      subtitle: 'Огляд системи Vertep' },
  'jobs':       { title: 'Завдання',      subtitle: 'Управління завданнями' },
  'workers':    { title: 'Воркери',       subtitle: 'Вузли та їх стан' },
  'characters': { title: 'Персонажі',     subtitle: 'Персонажі контенту' },
  'settings':   { title: 'Налаштування',  subtitle: 'Системні налаштування' },
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

  private subs = new Subscription();

  constructor(
    private router: Router,
    private sidebarService: SidebarService,
    private themeService: ThemeService,
    private api: VertepApiService,
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
    const id = setInterval(() => this.loadSystemState(), 30_000);
    this.subs.add(new Subscription(() => clearInterval(id)));
  }

  ngOnDestroy(): void { this.subs.unsubscribe(); }

  toggleSidebar(): void { this.sidebarService.toggle(); }
  toggleTheme(): void   { this.themeService.toggle(); }

  switchToV1(): void {
    document.cookie = 'vertep_ui=v1; path=/; max-age=31536000; SameSite=Lax';
    window.location.href = '/v1/';
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
        const state = s.system?.state?.toUpperCase() ?? 'NORMAL';
        const labels: Record<string, string> = {
          NORMAL: 'Нормальний', OK: 'Працює', HEALTHY: 'Працює',
          MAINTENANCE: 'Обслуговування', UPDATING: 'Оновлення',
          EMERGENCY: 'Аварія', FAILED: 'Помилка', ERROR: 'Помилка',
        };
        this.systemState = labels[state] ?? state;
        this.systemOk = ['NORMAL', 'OK', 'HEALTHY'].includes(state);
      },
      error: () => { this.systemState = 'Недоступний'; this.systemOk = false; },
    });
  }
}
