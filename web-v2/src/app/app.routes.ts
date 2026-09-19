import { Routes } from '@angular/router';
import { LayoutComponent } from './layout/layout.component';
import { AuthGuard } from './core/auth.guard';
import { AdminGuard } from './core/admin.guard';
import { UnsavedGuardService } from './core/services/unsaved-guard.service';
import { RouteMetadata } from './core/models';

export const routes: Routes = [
  {
    path: 'login',
    loadComponent: () => import('./login/login.component').then(m => m.LoginComponent),
    data: { metadata: { title: 'Увійти', icon: 'login-icon' } }
  },
  {
    path: 'setup',
    loadComponent: () => import('./setup/setup.component').then(m => m.SetupComponent),
    data: { metadata: { title: 'Налаштування', icon: 'setup-icon' } }
  },
  {
    path: '',
    component: LayoutComponent,
    canActivate: [AuthGuard],
    children: [
      { path: '', title: 'Дашборд', data: { metadata: { title: 'Дашборд', icon: 'dashboard-icon' } } },
      { path: 'jobs', title: 'Завдання', data: { metadata: { title: 'Завдання', icon: 'jobs-icon' } } },
      { path: 'queue', title: 'Черга', data: { metadata: { title: 'Черга', icon: 'queue-icon', queryParams: { tab: 'queue' } } } },
      { path: 'jobs/:id', title: 'Завдання', data: { metadata: { title: 'Завдання' } } },
      { path: 'published', title: 'Опубліковане', data: { metadata: { title: 'Опубліковане', icon: 'published-icon' } } },
      { path: 'workers', title: 'Вузли', data: { metadata: { title: 'Вузли', icon: 'workers-icon' } } },
      { path: 'workers/:id', title: 'Вузол', data: { metadata: { title: 'Вузол' } } },
      { path: 'characters', title: 'Персонажі', data: { metadata: { title: 'Персонажі', icon: 'characters-icon' } } },
      { path: 'characters/:id', title: 'Персонаж', data: { metadata: { title: 'Персонаж' } }, canDeactivate: [UnsavedGuardService] },
      { path: 'workflows', title: 'Сценарії', data: { metadata: { title: 'Сценарії', icon: 'workflows-icon' } } },
      { path: 'brands', title: 'Бренди', data: { metadata: { title: 'Бренди', icon: 'brands-icon' } } },
      { path: 'operations', title: 'Операції', data: { metadata: { title: 'Операції', icon: 'operations-icon' } } },
      { path: 'alerts', title: 'Алерти', data: { metadata: { title: 'Алерти', icon: 'alerts-icon' } } },
      { path: 'logs', title: 'Логи', data: { metadata: { title: 'Логи', icon: 'logs-icon' } } },
      { path: 'health', title: 'Стан системи', data: { metadata: { title: 'Стан системи', icon: 'health-icon' } } },
      { path: 'settings', title: 'Налаштування', data: { metadata: { title: 'Налаштування', icon: 'settings-icon', adminOnly: true } }, canActivate: [AdminGuard] },
      { path: 'profile', title: 'Профіль', data: { metadata: { title: 'Профіль', icon: 'profile-icon' } } },
    ],
  },
  { path: '**', redirectTo: '' },
];
