import { ChangeDetectionStrategy, Component, OnDestroy, OnInit, computed, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ActivatedRoute, Router } from '@angular/router';
import { Subscription } from 'rxjs';
import { PolicyService } from '../../core/services/policy.service';
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
      [tabs]="tabs()"
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
  tabs = computed<WorkspaceTab[]>(() => {
    const list: WorkspaceTab[] = [{ id: 'workflows', label: 'Шаблони обробки' }];
    if (this.policy.userRole() === 'admin') {
      list.push(
        { id: 'models', label: 'Моделі' },
        { id: 'video-engine', label: 'Движок відео' },
        { id: 'workflow-mode', label: 'Режим workflow' },
      );
    }
    return list;
  });

  activeTab = signal('workflows');
  private sub?: Subscription;

  constructor(
    private route: ActivatedRoute,
    private router: Router,
    private policy: PolicyService,
  ) {}

  ngOnInit(): void {
    this.sub = this.route.queryParamMap.subscribe(params => {
      const requested = params.get('tab') || 'workflows';
      const allowed = this.tabs().some(t => t.id === requested);
      if (!allowed) {
        this.activeTab.set('workflows');
        this.router.navigate(['/v3/pipeline'], { queryParams: { tab: 'workflows' }, replaceUrl: true });
        return;
      }
      this.activeTab.set(requested);
    });
  }

  ngOnDestroy(): void { this.sub?.unsubscribe(); }

  selectTab(tab: string): void {
    if (!this.tabs().some(t => t.id === tab)) return;
    this.activeTab.set(tab);
    this.router.navigate(['/v3/pipeline'], { queryParams: { tab }, replaceUrl: true });
  }
}
