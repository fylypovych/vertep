import { ChangeDetectionStrategy, Component, OnDestroy, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ActivatedRoute, Router } from '@angular/router';
import { Subscription } from 'rxjs';
import { SystemInfoSectionComponent } from '../../settings/sections/system-info.section';
import { BrandingSectionComponent } from '../../settings/sections/branding.section';
import { UsersSectionComponent } from '../../settings/sections/users.section';
import { PublishingSectionComponent } from '../../settings/sections/publishing.section';
import { TelegramSectionComponent } from '../../settings/sections/telegram.section';
import { SecretsSectionComponent } from '../../settings/sections/secrets.section';
import { SecuritySectionComponent } from '../../settings/sections/security.section';
import { TabbedWorkspaceComponent, WorkspaceTab } from '../ui/tabbed-workspace.component';

@Component({
  selector: 'app-v3-settings-workspace',
  standalone: true,
  imports: [
    CommonModule,
    TabbedWorkspaceComponent,
    SystemInfoSectionComponent,
    BrandingSectionComponent,
    UsersSectionComponent,
    PublishingSectionComponent,
    TelegramSectionComponent,
    SecretsSectionComponent,
    SecuritySectionComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <app-v3-tabbed-workspace
      [tabs]="tabs"
      [activeTab]="activeTab()"
      (tabChange)="selectTab($event)"
      testId="v3-settings-workspace">
      @switch (activeTab()) {
        @case ('general') {
          <div class="space-y-5">
            <app-settings-system-info />
            <app-settings-branding />
          </div>
        }
        @case ('users') {
          <app-settings-users />
        }
        @case ('integrations') {
          <div class="space-y-5">
            <app-settings-publishing />
            <app-settings-telegram />
            <app-settings-secrets />
          </div>
        }
        @case ('security') {
          <app-settings-security />
        }
      }
    </app-v3-tabbed-workspace>
  `,
})
export class V3SettingsWorkspaceComponent implements OnInit, OnDestroy {
  readonly tabs: WorkspaceTab[] = [
    { id: 'general', label: 'Загальне і брендинг' },
    { id: 'users', label: 'Користувачі та доступ' },
    { id: 'integrations', label: 'Інтеграції' },
    { id: 'security', label: 'Безпека' },
  ];

  activeTab = signal('general');
  private sub?: Subscription;

  constructor(private route: ActivatedRoute, private router: Router) {}

  ngOnInit(): void {
    this.sub = this.route.queryParamMap.subscribe(params => {
      const raw = params.get('tab') || 'general';
      // Support legacy v2 /v3/settings?tab=... deep links cleanly.
      if (raw === 'update' || raw === 'backup') {
        this.router.navigate(['/v3/modules'], { queryParams: { tab: raw }, replaceUrl: true });
        return;
      }
      if (raw === 'models' || raw === 'video-engine' || raw === 'workflow-mode') {
        this.router.navigate(['/v3/pipeline'], { queryParams: { tab: raw }, replaceUrl: true });
        return;
      }
      if (raw === 'publishing' || raw === 'telegram' || raw === 'secrets') {
        this.activeTab.set('integrations');
        return;
      }
      if (raw === 'system' || raw === 'branding') {
        this.activeTab.set('general');
        return;
      }
      this.activeTab.set(this.tabs.some(t => t.id === raw) ? raw : 'general');
    });
  }

  ngOnDestroy(): void { this.sub?.unsubscribe(); }

  selectTab(tab: string): void {
    this.activeTab.set(tab);
    this.router.navigate(['/v3/settings'], { queryParams: { tab }, replaceUrl: true });
  }
}
