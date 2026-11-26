import { ChangeDetectionStrategy, Component, OnDestroy, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ActivatedRoute, Router } from '@angular/router';
import { Subscription } from 'rxjs';
import { WorkflowsComponent } from '../../workflows/workflows.component';
import { ModelsSectionComponent } from '../../settings/sections/models.section';
import { VideoEngineSectionComponent } from '../../settings/sections/video-engine.section';
import { WorkflowModeSectionComponent } from '../../settings/sections/workflow-mode.section';
import { TabbedWorkspaceComponent, WorkspaceTab } from '../ui/tabbed-workspace.component';

@Component({
  selector: 'app-v3-pipeline-workspace',
  standalone: true,
  imports: [
    CommonModule,
    TabbedWorkspaceComponent,
    WorkflowsComponent,
    ModelsSectionComponent,
    VideoEngineSectionComponent,
    WorkflowModeSectionComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <app-v3-tabbed-workspace
      [tabs]="tabs"
      [activeTab]="activeTab()"
      (tabChange)="selectTab($event)"
      testId="v3-pipeline-workspace">
      @switch (activeTab()) {
        @case ('workflows') { <app-workflows /> }
        @case ('models') { <app-settings-models /> }
        @case ('video-engine') { <app-settings-video-engine /> }
        @case ('workflow-mode') { <app-settings-workflow-mode /> }
      }
    </app-v3-tabbed-workspace>
  `,
})
export class V3PipelineWorkspaceComponent implements OnInit, OnDestroy {
  readonly tabs: WorkspaceTab[] = [
    { id: 'workflows', label: 'Шаблони обробки' },
    { id: 'models', label: 'Моделі' },
    { id: 'video-engine', label: 'Движок відео' },
    { id: 'workflow-mode', label: 'Режим workflow' },
  ];

  activeTab = signal('workflows');
  private sub?: Subscription;

  constructor(private route: ActivatedRoute, private router: Router) {}

  ngOnInit(): void {
    this.sub = this.route.queryParamMap.subscribe(params => {
      const requested = params.get('tab') || 'workflows';
      this.activeTab.set(this.tabs.some(t => t.id === requested) ? requested : 'workflows');
    });
  }

  ngOnDestroy(): void { this.sub?.unsubscribe(); }

  selectTab(tab: string): void {
    this.activeTab.set(tab);
    this.router.navigate(['/v3/pipeline'], { queryParams: { tab }, replaceUrl: true });
  }
}
