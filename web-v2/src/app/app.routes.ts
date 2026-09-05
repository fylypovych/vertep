import { Routes } from '@angular/router';
import { LayoutComponent } from './layout/layout.component';
import { DashboardComponent } from './dashboard/dashboard.component';
import { JobsComponent } from './jobs/jobs.component';
import { WorkersComponent } from './workers/workers.component';
import { CharactersComponent } from './characters/characters.component';
import { SettingsComponent } from './settings/settings.component';
import { LoginComponent } from './login/login.component';
import { AuthGuard } from './core/auth.guard';

export const routes: Routes = [
  { path: 'login', component: LoginComponent },
  {
    path: '',
    component: LayoutComponent,
    canActivate: [AuthGuard],
    children: [
      { path: '', component: DashboardComponent, title: 'Дашборд' },
      { path: 'jobs', component: JobsComponent, title: 'Завдання' },
      { path: 'workers', component: WorkersComponent, title: 'Воркери' },
      { path: 'characters', component: CharactersComponent, title: 'Персонажі' },
      { path: 'settings', component: SettingsComponent, title: 'Налаштування' },
    ],
  },
];
