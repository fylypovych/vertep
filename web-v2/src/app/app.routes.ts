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
      { path: '', loadComponent: () => import('./dashboard/dashboard.component').then(m => m.DashboardComponent), title: 'Дашборд', data: { metadata: { title: 'Дашборд', icon: 'dashboard-icon' } } },
      { path: 'jobs', loadComponent: () => import('./jobs/jobs.component').then(m => m.JobsComponent), title: 'Завдання', data: { metadata: { title: 'Завдання', icon: 'jobs-icon' } } },
      { path: 'queue', loadComponent: () => import('./jobs/jobs.component').then(m => m.JobsComponent), title: 'Черга', data: { metadata: { title: 'Черга', icon: 'queue-icon', queryParams: { tab: 'queue' } } } },
      { path: 'jobs/:id', loadComponent: () => import('./jobs/job-detail.component').then(m => m.JobDetailComponent), title: 'Завдання', data: { metadata: { title: 'Завдання' } } },
      { path: 'published', loadComponent: () => import('./published/published.component').then(m => m.PublishedComponent), title: 'Опубліковане', data: { metadata: { title: 'Опубліковане', icon: 'published-icon' } } },
      { path: 'workers', loadComponent: () => import('./workers/workers.component').then(m => m.WorkersComponent), title: 'Вузли', data: { metadata: { title: 'Вузли', icon: 'workers-icon' } } },
      { path: 'workers/:id', loadComponent: () => import('./worker-detail/worker-detail.component').then(m => m.WorkerDetailComponent), title: 'Вузол', data: { metadata: { title: 'Вузол' } } },
      { path: 'characters', loadComponent: () => import('./characters/characters.component').then(m => m.CharactersComponent), title: 'Персонажі', data: { metadata: { title: 'Персонажі', icon: 'characters-icon' } } },
      { path: 'characters/:id', loadComponent: () => import('./character-detail/character-detail.component').then(m => m.CharacterDetailComponent), title: 'Персонаж', data: { metadata: { title: 'Персонаж' } }, canDeactivate: [UnsavedGuardService] },
      { path: 'workflows', loadComponent: () => import('./workflows/workflows.component').then(m => m.WorkflowsComponent), title: 'Сценарії', data: { metadata: { title: 'Сценарії', icon: 'workflows-icon' } } },
      { path: 'brands', loadComponent: () => import('./brands/brands.component').then(m => m.BrandsComponent), title: 'Бренди', data: { metadata: { title: 'Бренди', icon: 'brands-icon' } } },
      { path: 'operations', redirectTo: 'alerts', pathMatch: 'full' },
      { path: 'alerts', loadComponent: () => import('./alerts/alerts.component').then(m => m.AlertsComponent), title: 'Алерти', data: { metadata: { title: 'Алерти', icon: 'alerts-icon' } } },
      { path: 'logs', loadComponent: () => import('./logs/logs.component').then(m => m.LogsComponent), title: 'Логи', data: { metadata: { title: 'Логи', icon: 'logs-icon' } } },
      { path: 'health', loadComponent: () => import('./health/health.component').then(m => m.HealthComponent), title: 'Стан системи', data: { metadata: { title: 'Стан системи', icon: 'health-icon' } } },
      { path: 'settings', loadComponent: () => import('./settings/settings.component').then(m => m.SettingsComponent), title: 'Налаштування', data: { metadata: { title: 'Налаштування', icon: 'settings-icon', adminOnly: true } }, canActivate: [AdminGuard] },
      { path: 'profile', loadComponent: () => import('./profile/profile.component').then(m => m.ProfileComponent), title: 'Профіль', data: { metadata: { title: 'Профіль', icon: 'profile-icon' } } },
    ],
  },
  { path: '**', redirectTo: '' },
];
