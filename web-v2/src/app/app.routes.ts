import { Routes } from '@angular/router';
import { LayoutComponent } from './layout/layout.component';
import { DashboardComponent } from './dashboard/dashboard.component';
import { JobsComponent } from './jobs/jobs.component';
import { JobDetailComponent } from './jobs/job-detail.component';
import { WorkersComponent } from './workers/workers.component';
import { WorkerDetailComponent } from './worker-detail/worker-detail.component';
import { CharactersComponent } from './characters/characters.component';
import { CharacterDetailComponent } from './character-detail/character-detail.component';
import { WorkflowsComponent } from './workflows/workflows.component';
import { BrandsComponent } from './brands/brands.component';
import { SettingsComponent } from './settings/settings.component';
import { LoginComponent } from './login/login.component';
import { QueueComponent } from './queue/queue.component';
import { PublishedComponent } from './published/published.component';
import { AlertsComponent } from './alerts/alerts.component';
import { LogsComponent } from './logs/logs.component';
import { HealthComponent } from './health/health.component';
import { AuthGuard } from './core/auth.guard';

export const routes: Routes = [
  { path: 'login', component: LoginComponent },
  {
    path: '',
    component: LayoutComponent,
    canActivate: [AuthGuard],
    children: [
      { path: '',           component: DashboardComponent,  title: 'Дашборд' },
      { path: 'jobs',       component: JobsComponent,        title: 'Завдання' },
      { path: 'jobs/:id',   component: JobDetailComponent,  title: 'Завдання' },
      { path: 'queue',      component: QueueComponent,       title: 'Черга' },
      { path: 'published',  component: PublishedComponent,   title: 'Опубліковане' },
      { path: 'workers',    component: WorkersComponent,     title: 'Воркери' },
      { path: 'workers/:id', component: WorkerDetailComponent, title: 'Воркер' },
      { path: 'characters', component: CharactersComponent,  title: 'Персонажі' },
      { path: 'characters/:id', component: CharacterDetailComponent, title: 'Персонаж' },
      { path: 'workflows', component: WorkflowsComponent, title: 'Сценарії' },
      { path: 'brands', component: BrandsComponent, title: 'Бренди' },
      { path: 'operations', component: AlertsComponent,      title: 'Операції' },
      { path: 'alerts',     component: AlertsComponent,      title: 'Алерти' },
      { path: 'logs',       component: LogsComponent,        title: 'Логи' },
      { path: 'health',     component: HealthComponent,      title: 'Стан системи' },
      { path: 'settings',   component: SettingsComponent,   title: 'Налаштування' },
    ],
  },
  { path: '**', redirectTo: '' },
];
