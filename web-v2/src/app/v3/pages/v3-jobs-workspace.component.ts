import { AfterViewInit, ChangeDetectionStrategy, Component, OnDestroy, OnInit, ViewChild, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ActivatedRoute, Router } from '@angular/router';
import { Subscription } from 'rxjs';
import { JobsComponent } from '../../jobs/jobs.component';
import { TabbedWorkspaceComponent, WorkspaceTab } from '../ui/tabbed-workspace.component';

@Component({
  selector: 'app-v3-jobs-workspace',
  standalone: true,
  imports: [CommonModule, TabbedWorkspaceComponent, JobsComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <app-v3-tabbed-workspace
      [tabs]="tabs"
      [activeTab]="activeTab()"
      (tabChange)="selectTab($event)"
      testId="v3-jobs-workspace">
      <app-jobs [hideViewTabs]="true" />
    </app-v3-tabbed-workspace>
  `,
})
export class V3JobsWorkspaceComponent implements OnInit, AfterViewInit, OnDestroy {
  @ViewChild(JobsComponent) jobsComponent?: JobsComponent;

  readonly tabs: WorkspaceTab[] = [
    { id: 'list', label: 'Завдання' },
    { id: 'queue', label: 'Черга виконання' },
  ];

  activeTab = signal<'list' | 'queue'>('list');
  private sub?: Subscription;

  constructor(private route: ActivatedRoute, private router: Router) {}

  ngOnInit(): void {
    const fallback = this.route.snapshot.data['defaultTab'] === 'queue' ? 'queue' : 'list';
    this.sub = this.route.queryParamMap.subscribe(params => {
      const requested = params.get('tab') === 'queue' ? 'queue' : (params.get('tab') === 'list' ? 'list' : fallback);
      this.activeTab.set(requested);
      this.syncChild(requested);
    });
  }

  ngAfterViewInit(): void {
    this.syncChild(this.activeTab());
  }

  ngOnDestroy(): void { this.sub?.unsubscribe(); }

  selectTab(tab: string): void {
    const next: 'list' | 'queue' = tab === 'queue' ? 'queue' : 'list';
    this.activeTab.set(next);
    this.syncChild(next);
    this.router.navigate(['/v3/jobs'], { queryParams: { tab: next }, replaceUrl: true });
  }

  private syncChild(tab: 'list' | 'queue'): void {
    if (this.jobsComponent && this.jobsComponent.view() !== tab) {
      this.jobsComponent.applyView(tab);
    }
  }
}
