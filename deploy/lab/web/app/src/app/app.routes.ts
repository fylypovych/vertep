import { Routes } from '@angular/router';
import { ApprovalsComponent } from './approvals/approvals.component';
import { BatchIrComponent } from './batch-ir/batch-ir.component';
import { DashboardComponent } from './dashboard/dashboard.component';
import { GpuComponent } from './gpu/gpu.component';
import { HistoryComponent } from './history/history.component';
import { IssuesComponent } from './issues/issues.component';
import { ReportsComponent } from './reports/reports.component';
import { SettingsComponent } from './settings/settings.component';

export const routes: Routes = [
  { path: '', pathMatch: 'full', redirectTo: 'dashboard' },
  { path: 'dashboard', component: DashboardComponent },
  { path: 'issues', component: IssuesComponent },
  { path: 'batch-ir', component: BatchIrComponent },
  { path: 'approvals', component: ApprovalsComponent },
  { path: 'gpu', component: GpuComponent },
  { path: 'history', component: HistoryComponent },
  { path: 'settings', component: SettingsComponent },
  { path: 'reports', component: ReportsComponent },
  { path: '**', redirectTo: 'dashboard' },
];
