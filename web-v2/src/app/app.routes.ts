import { Routes } from '@angular/router';
import { LayoutComponent } from './layout/layout.component';
import { AuthGuard } from './core/auth.guard';
import { AdminGuard } from './core/admin.guard';
import { UnsavedGuardService } from './core/services/unsaved-guard.service';

export const routes: Routes = [
  {
    path: 'login',
    loadComponent: () => import('./login/login.component').then(m => m.LoginComponent),
  },
  {
    path: 'setup',
    loadComponent: () => import('./setup/setup.component').then(m => m.SetupComponent),
  },
  {
    path: '',
    component: LayoutComponent,
    canActivate: [AuthGuard],
    children: [
      { path: '', loadComponent: () => import('./dashboard/dashboard.component').then(m => m.DashboardComponent), title: 'Дашборд' },
      { path: 'jobs', loadComponent: () => import('./jobs/jobs.component').then(m => m.JobsComponent), title: 'Завдання' },
      { path: 'queue', loadComponent: () => import('./jobs/jobs.component').then(m => m.JobsComponent), title: 'Черга' },
      { path: 'jobs/:id', loadComponent: () => import('./jobs/job-detail.component').then(m => m.JobDetailComponent), title: 'Завдання' },
      { path: 'published', loadComponent: () => import('./published/published.component').then(m => m.PublishedComponent), title: 'Опубліковане' },
      { path: 'workers', loadComponent: () => import('./workers/workers.component').then(m => m.WorkersComponent), title: 'Вузли' },
      { path: 'workers/:id', loadComponent: () => import('./worker-detail/worker-detail.component').then(m => m.WorkerDetailComponent), title: 'Вузол' },
      { path: 'characters', loadComponent: () => import('./characters/characters.component').then(m => m.CharactersComponent), title: 'Персонажі' },
      { path: 'characters/:id', loadComponent: () => import('./character-detail/character-detail.component').then(m => m.CharacterDetailComponent), title: 'Персонаж', canDeactivate: [UnsavedGuardService] },
      { path: 'workflows', loadComponent: () => import('./workflows/workflows.component').then(m => m.WorkflowsComponent), title: 'Сценарії' },
      { path: 'brands', loadComponent: () => import('./brands/brands.component').then(m => m.BrandsComponent), title: 'Бренди' },
      { path: 'operations', loadComponent: () => import('./alerts/alerts.component').then(m => m.AlertsComponent), title: 'Операції' },
      { path: 'alerts', loadComponent: () => import('./alerts/alerts.component').then(m => m.AlertsComponent), title: 'Алерти' },
      { path: 'logs', loadComponent: () => import('./logs/logs.component').then(m => m.LogsComponent), title: 'Логи' },
      { path: 'health', loadComponent: () => import('./health/health.component').then(m => m.HealthComponent), title: 'Стан системи' },
      { path: 'settings', loadComponent: () => import('./settings/settings.component').then(m => m.SettingsComponent), title: 'Налаштування', canActivate: [AdminGuard] },
      { path: 'profile', loadComponent: () => import('./profile/profile.component').then(m => m.ProfileComponent), title: 'Профіль' },
    ],
  },
  { path: '**', redirectTo: '' },
];
