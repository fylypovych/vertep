import { Routes } from '@angular/router';
import { LayoutComponent } from './layout/layout.component';
import { DashboardComponent } from './dashboard/dashboard.component';
import { JobsComponent } from './jobs/jobs.component';
import { WorkersComponent } from './workers/workers.component';
import { CharactersComponent } from './characters/characters.component';
import { SettingsComponent } from './settings/settings.component';

export const routes: Routes = [
  {
    path: '',
    component: LayoutComponent,
    children: [
      { path: '', component: DashboardComponent },
      { path: 'jobs', component: JobsComponent },
      { path: 'workers', component: WorkersComponent },
      { path: 'characters', component: CharactersComponent },
      { path: 'settings', component: SettingsComponent },
    ],
  },
];
