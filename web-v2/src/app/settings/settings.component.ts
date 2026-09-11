import { Component } from '@angular/core';
import { CommonModule } from '@angular/common';
import { SystemInfoSectionComponent } from './sections/system-info.section';
import { TelegramSectionComponent } from './sections/telegram.section';
import { SecretsSectionComponent } from './sections/secrets.section';
import { ModelsSectionComponent } from './sections/models.section';
import { UpdateSectionComponent } from './sections/update.section';
import { BackupSectionComponent } from './sections/backup.section';
import { SecuritySectionComponent } from './sections/security.section';
import { RolesSectionComponent } from './sections/roles.section';
import { BrandingSectionComponent } from './sections/branding.section';
import { IntegrationsSectionComponent } from './sections/integrations.section';

@Component({
  selector: 'app-settings',
  standalone: true,
  imports: [
    CommonModule,
    SystemInfoSectionComponent,
    TelegramSectionComponent,
    SecretsSectionComponent,
    ModelsSectionComponent,
    UpdateSectionComponent,
    BackupSectionComponent,
    SecuritySectionComponent,
    RolesSectionComponent,
    BrandingSectionComponent,
    IntegrationsSectionComponent,
  ],
  template: `
    <div class="space-y-6" data-testid="settings-page">
      <div class="flex flex-col lg:flex-row gap-6">
        <nav class="lg:w-56 flex-shrink-0">
          <div class="bg-white rounded-xl border border-slate-200 p-2 flex lg:flex-col gap-1 overflow-x-auto">
            @for (tab of tabs; track tab.id) {
              <button (click)="activeTab = tab.id"
                class="flex items-center gap-2 px-3 py-2 text-sm font-medium rounded-lg whitespace-nowrap transition-colors text-left w-full"
                [class.bg-emerald-50]="activeTab === tab.id"
                [class.text-emerald-700]="activeTab === tab.id"
                [class.text-slate-600]="activeTab !== tab.id"
                [class.hover:bg-slate-50]="activeTab !== tab.id">
                <span [innerHTML]="tab.icon" class="w-4 h-4 flex-shrink-0"></span>
                <span>{{ tab.label }}</span>
              </button>
            }
          </div>
        </nav>
        <div class="flex-1 min-w-0">
          @switch (activeTab) {
            @case ('system') { <app-settings-system-info /> }
            @case ('telegram') { <app-settings-telegram /> }
            @case ('secrets') { <app-settings-secrets /> }
            @case ('models') { <app-settings-models /> }
            @case ('update') { <app-settings-update /> }
            @case ('backup') { <app-settings-backup /> }
            @case ('security') { <app-settings-security /> }
            @case ('roles') { <app-settings-roles /> }
            @case ('branding') { <app-settings-branding /> }
            @case ('integrations') { <app-settings-integrations /> }
          }
        </div>
      </div>
    </div>
  `,
})
export class SettingsComponent {
  activeTab = 'system';
  readonly tabs = [
    { id: 'system', label: 'Система', icon: '<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-4 h-4"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 12h14M5 12a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v4a2 2 0 01-2 2M5 12a2 2 0 00-2 2v4a2 2 0 002 2h14a2 2 0 002-2v-4a2 2 0 00-2-2"/></svg>' },
    { id: 'telegram', label: 'Telegram', icon: '<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-4 h-4"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z"/></svg>' },
    { id: 'secrets', label: 'Секрети', icon: '<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-4 h-4"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z"/></svg>' },
    { id: 'models', label: 'Моделі', icon: '<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-4 h-4"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19.428 15.428a2 2 0 00-1.022-.547l-2.387-.477a6 6 0 00-3.86.517l-.318.158a6 6 0 01-3.86.517L6.05 15.21a2 2 0 00-1.806.547M8 4h8l-1 1v5.172a2 2 0 00.586 1.414l5 5c1.26 1.26.367 3.414-1.415 3.414H4.828c-1.782 0-2.674-2.154-1.414-3.414l5-5A2 2 0 009 10.172V5L8 4z"/></svg>' },
    { id: 'update', label: 'Оновлення', icon: '<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-4 h-4"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>' },
    { id: 'backup', label: 'Бекапи', icon: '<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-4 h-4"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 8h14M5 8a2 2 0 110-4h14a2 2 0 110 4M5 8v10a2 2 0 002 2h10a2 2 0 002-2V8m-9 4h4"/></svg>' },
    { id: 'security', label: 'Безпека', icon: '<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-4 h-4"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z"/></svg>' },
    { id: 'roles', label: 'Ролі', icon: '<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-4 h-4"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z"/></svg>' },
    { id: 'branding', label: 'Брендинг', icon: '<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-4 h-4"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z"/></svg>' },
    { id: 'integrations', label: 'Інтеграції', icon: '<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="w-4 h-4"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1"/></svg>' },
  ];
}
