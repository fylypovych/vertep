import { ChangeDetectionStrategy, Component, OnDestroy, OnInit, computed, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ActivatedRoute, Router } from '@angular/router';
import { Subscription } from 'rxjs';
import { PolicyService } from '../../core/services/policy.service';
import { HealthComponent } from '../../health/health.component';
import { AlertsComponent } from '../../alerts/alerts.component';
import { LogsComponent } from '../../logs/logs.component';
import { IntegrationsSectionComponent } from '../../settings/sections/integrations.section';
import { RealTestsComponent } from '../../real-tests/real-tests.component';
import { TabbedWorkspaceComponent, WorkspaceTab } from '../ui/tabbed-workspace.component';

@Component({
  selector: 'app-v3-monitor-workspace',
  standalone: true,
  imports: [
    CommonModule,
    TabbedWorkspaceComponent,
    HealthComponent,
    AlertsComponent,
    LogsComponent,
    IntegrationsSectionComponent,
    RealTestsComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <app-v3-tabbed-workspace
      [tabs]="tabs()"
      [activeTab]="activeTab()"
      (tabChange)="selectTab($event)"
      testId="v3-monitor-workspace">
      @switch (activeTab()) {
        @case ('health') { <app-health /> }
        @case ('alerts') { <app-alerts /> }
        @case ('logs') { <app-logs /> }
        @case ('integrations') { <app-settings-integrations /> }
        @case ('real-tests') { <app-real-tests /> }
      }
    </app-v3-tabbed-workspace>
  `,
})
export class V3MonitorWorkspaceComponent implements OnInit, OnDestroy {
  activeTab = signal('health');
  private sub?: Subscription;

  tabs = computed<WorkspaceTab[]>(() => {
    const base: WorkspaceTab[] = [
      { id: 'health', label: 'Стан системи' },
      { id: 'alerts', label: 'Алерти' },
      { id: 'logs', label: 'Логи' },
    ];
    if (this.policy.userRole() === 'admin') {
      base.push({ id: 'integrations', label: 'Стан підключень' });
      base.push({ id: 'real-tests', label: 'Реальні тести' });
    }
    return base;
  });

  constructor(
    private route: ActivatedRoute,
    private router: Router,
    private policy: PolicyService,
  ) {}

  ngOnInit(): void {
    const fallback = (this.route.snapshot.data['defaultTab'] as string) || 'health';
    this.sub = this.route.queryParamMap.subscribe(params => {
      const requested = params.get('tab') || fallback;
      const allowed = this.tabs().some(t => t.id === requested);
      this.activeTab.set(allowed ? requested : 'health');
    });
  }

  ngOnDestroy(): void { this.sub?.unsubscribe(); }

  selectTab(tab: string): void {
    this.activeTab.set(tab);
    this.router.navigate(['/v3/monitor'], { queryParams: { tab }, replaceUrl: true });
  }
}
