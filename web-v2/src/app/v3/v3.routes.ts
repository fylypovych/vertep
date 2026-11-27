import { Routes } from '@angular/router';
import { V3LayoutComponent } from './layout/v3-layout.component';
import { AdminGuard } from '../core/admin.guard';
import { modulePageGuard } from '../core/module-page.guard';
import { UnsavedGuardService } from '../core/services/unsaved-guard.service';

export const V3_ROUTES: Routes = [
  {
    path: '',
    component: V3LayoutComponent,
    children: [
      {
        path: '',
        loadComponent: () => import('./pages/v3-dashboard.component').then(m => m.V3DashboardComponent),
        title: 'Дашборд',
      },
      {
        path: 'jobs',
        loadComponent: () => import('./pages/v3-jobs-workspace.component').then(m => m.V3JobsWorkspaceComponent),
        title: 'Завдання і черга',
      },
      {
        path: 'queue',
        loadComponent: () => import('./pages/v3-jobs-workspace.component').then(m => m.V3JobsWorkspaceComponent),
        title: 'Черга виконання',
        data: { defaultTab: 'queue' },
      },
      {
        path: 'jobs/:id',
        loadComponent: () => import('../jobs/job-detail.component').then(m => m.JobDetailComponent),
        title: 'Завдання',
      },
      {
        path: 'brands',
        canActivate: [modulePageGuard],
        loadComponent: () => import('./pages/v3-brands.component').then(m => m.V3BrandsComponent),
        title: 'Бренди',
      },
      {
        path: 'characters',
        canActivate: [modulePageGuard],
        loadComponent: () => import('./pages/v3-characters.component').then(m => m.V3CharactersComponent),
        title: 'Персонажі',
      },
      {
        path: 'characters/:id',
        canActivate: [modulePageGuard],
        canDeactivate: [UnsavedGuardService],
        loadComponent: () => import('./pages/v3-character-detail.component').then(m => m.V3CharacterDetailComponent),
        title: 'Персонаж',
      },
      {
        path: 'published',
        canActivate: [modulePageGuard],
        loadComponent: () => import('../published/published.component').then(m => m.PublishedComponent),
        title: 'Опубліковане',
      },
      {
        path: 'workers',
        loadComponent: () => import('../workers/workers.component').then(m => m.WorkersComponent),
        title: 'Вузли',
      },
      {
        path: 'workers/:id',
        loadComponent: () => import('../worker-detail/worker-detail.component').then(m => m.WorkerDetailComponent),
        title: 'Вузол',
      },
      {
        path: 'monitor',
        loadComponent: () => import('./pages/v3-monitor-workspace.component').then(m => m.V3MonitorWorkspaceComponent),
        title: 'Моніторинг',
      },
      {
        path: 'health',
        loadComponent: () => import('./pages/v3-monitor-workspace.component').then(m => m.V3MonitorWorkspaceComponent),
        title: 'Стан системи',
        data: { defaultTab: 'health' },
      },
      {
        path: 'alerts',
        loadComponent: () => import('./pages/v3-monitor-workspace.component').then(m => m.V3MonitorWorkspaceComponent),
        title: 'Алерти',
        data: { defaultTab: 'alerts' },
      },
      {
        path: 'logs',
        loadComponent: () => import('./pages/v3-monitor-workspace.component').then(m => m.V3MonitorWorkspaceComponent),
        title: 'Логи',
        data: { defaultTab: 'logs' },
      },
      {
        path: 'real-tests',
        canActivate: [AdminGuard],
        loadComponent: () => import('./pages/v3-monitor-workspace.component').then(m => m.V3MonitorWorkspaceComponent),
        title: 'Реальні тести',
        data: { defaultTab: 'real-tests' },
      },
      {
        path: 'modules',
        canActivate: [AdminGuard],
        loadComponent: () => import('./pages/v3-modules-workspace.component').then(m => m.V3ModulesWorkspaceComponent),
        title: 'Модулі та обслуговування',
      },
      {
        path: 'modules/:moduleId',
        loadComponent: () => import('../modules/module-extension.component').then(m => m.ModuleExtensionComponent),
        title: 'Модуль',
      },
      {
        path: 'pipeline',
        loadComponent: () => import('./pages/v3-pipeline-workspace.component').then(m => m.V3PipelineWorkspaceComponent),
        title: 'Конвеєр',
      },
      {
        path: 'workflows',
        loadComponent: () => import('../workflows/workflows.component').then(m => m.WorkflowsComponent),
        title: 'Шаблони обробки',
      },
      {
        path: 'settings',
        canActivate: [AdminGuard],
        loadComponent: () => import('./pages/v3-settings-workspace.component').then(m => m.V3SettingsWorkspaceComponent),
        title: 'Налаштування',
      },
      {
        path: 'lab',
        canActivate: [AdminGuard],
        loadComponent: () => import('../lab/lab-entry.component').then(m => m.LabEntryComponent),
        title: 'Лабораторія',
      },
      {
        path: 'profile',
        loadComponent: () => import('./pages/v3-profile.component').then(m => m.V3ProfileComponent),
        title: 'Профіль',
      },
      { path: '**', redirectTo: '' },
    ],
  },
];
