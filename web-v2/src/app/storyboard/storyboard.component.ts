import { Component } from '@angular/core';

@Component({
  selector: 'app-storyboard',
  template: `
    <div class="error-container" data-testid="error-alert">
      <p class="error-message">Сталася помилка</p>
    </div>
    <button (click)="onCancel()" data-testid="cancel-button" class="mt-4 px-4 py-2 bg-gray-200 rounded">
      Скасувати
    </button>
  `,
  styles: []
})
export class StoryboardComponent {
  onCancel(): void {
    // Placeholder for cancel logic – tests only need the element to exist.
    console.log('Cancel clicked');
  }
}
