import { Component, inject } from '@angular/core';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { OwnerTokenService } from './owner-token.service';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [RouterOutlet, RouterLink, RouterLinkActive],
  template: `
    <nav class="lab-nav">
      <span class="brand">Vertep Lab</span>
      <a data-testid="nav-dashboard" routerLink="/dashboard" routerLinkActive="active">Dashboard</a>
      <a data-testid="nav-issues" routerLink="/issues" routerLinkActive="active">Issues</a>
      <a data-testid="nav-batch-ir" routerLink="/batch-ir" routerLinkActive="active">Пакетна перевірка ir</a>
      <a data-testid="nav-approvals" routerLink="/approvals" routerLinkActive="active">Погодження</a>
      <a data-testid="nav-gpu" routerLink="/gpu" routerLinkActive="active">GPU</a>
      <a data-testid="nav-history" routerLink="/history" routerLinkActive="active">Історія</a>
      <a data-testid="nav-settings" routerLink="/settings" routerLinkActive="active">Налаштування</a>
      <a data-testid="nav-reports" routerLink="/reports" routerLinkActive="active">Звіти</a>
      <span style="flex: 1"></span>
      @if (owner.identity()) {
        <span data-testid="vertep-signed-in">Вхід через Vertep</span>
      } @else {
        <a data-testid="vertep-login" href="/api/lab/login">Увійти через Vertep</a>
      }
      <label>
        Owner token
        <input type="password" data-testid="owner-token"
               [value]="owner.token()" (input)="onToken($event)"
               placeholder="службовий вхід" size="14">
      </label>
    </nav>
    <main>
      <router-outlet></router-outlet>
    </main>
  `,
})
export class AppComponent {
  readonly owner = inject(OwnerTokenService);

  onToken(event: Event): void {
    this.owner.set((event.target as HTMLInputElement).value);
  }
}
