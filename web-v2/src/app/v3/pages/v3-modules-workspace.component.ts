import { ChangeDetectionStrategy, Component, OnDestroy, OnInit, computed, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ActivatedRoute, Router } from '@angular/router';
import { Subscription } from 'rxjs';
import { NavigationStateService } from '../../core/services/navigation-state.service';
import { ModuleManagementComponent } from '../../modules/module-management.component';
import { UpdateSectionComponent } from '../../settings/sections/update.section';
import { BackupSectionComponent } from '../../settings/sections/backup.section';
import { TabbedWorkspaceComponent, WorkspaceTab } from '../ui/tabbed-workspace.component';

@Component({
  selector: 'app-v3-modules-workspace',
  standalone: true,
  imports: [
    CommonModule,
    TabbedWorkspaceComponent,
    ModuleManagementComponent,
    UpdateSectionComponent,
    BackupSectionComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <app-v3-tabbed-workspace
      [tabs]="tabs()"
      [activeTab]="activeTab()"
      (tabChange)="selectTab($event)"
      testId="v3-modules-workspace">
      @switch (activeTab()) {
        @case ('catalog') { <app-module-management /> }
        @case ('update') { <app-settings-update /> }
        @case ('backup') { <app-settings-backup /> }
      }
    </app-v3-tabbed-workspace>
  `,
})
export class V3ModulesWorkspaceComponent implements OnInit, OnDestroy {
  activeTab = signal('catalog');
  backupAvailable = computed(() => this.navState.backupAvailable());
  private sub?: Subscription;

  tabs = computed<WorkspaceTab[]>(() => {
    const list: WorkspaceTab[] = [
      { id: 'catalog', label: 'Каталог модулів' },
      { id: 'update', label: 'Оновлення' },
    ];
    if (this.backupAvailable() || this.activeTab() === 'backup') {
      list.push({ id: 'backup', label: 'Бекапи' });
    }
    return list;
  });

  constructor(
    private route: ActivatedRoute,
    private router: Router,
    private navState: NavigationStateService,
  ) {}

  ngOnInit(): void {
    this.navState.refreshBackup();

    this.sub = this.route.queryParamMap.subscribe(params => {
      const requested = params.get('tab') || 'catalog';
      const valid = ['catalog', 'update', 'backup'].includes(requested);
      this.activeTab.set(valid ? requested : 'catalog');
    });
  }

  ngOnDestroy(): void { this.sub?.unsubscribe(); }

  selectTab(tab: string): void {
    this.activeTab.set(tab);
    this.router.navigate(['/v3/modules'], { queryParams: { tab }, replaceUrl: true });
  }
}
