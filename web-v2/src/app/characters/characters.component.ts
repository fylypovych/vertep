import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { VertepApiService } from '../core/vertep-api.service';
import { Character } from '../core/models';

@Component({
  selector: 'app-characters',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="bg-white rounded-xl border border-slate-200 p-5">
      <div class="flex items-center justify-between mb-4">
        <h3 class="text-lg font-semibold text-slate-900">Персонажі</h3>
        <button class="px-4 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 text-sm font-medium">
          Новий персонаж
        </button>
      </div>
      <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        @for (character of characters; track character.id) {
          <div class="border border-slate-200 rounded-lg p-4 hover:shadow-md transition-shadow">
            <div class="flex items-center justify-between mb-2">
              <h4 class="font-semibold text-slate-900">{{ character.name }}</h4>
              <span class="text-xs text-slate-500">{{ character.language }}</span>
            </div>
            <p class="text-sm text-slate-600 mb-3">{{ character.id }}</p>
            <div class="flex gap-2">
              <button class="text-sm text-emerald-600 hover:text-emerald-700 font-medium">Редагувати</button>
              <button class="text-sm text-slate-400 hover:text-slate-600 font-medium">Видалити</button>
            </div>
          </div>
        }
      </div>
    </div>
  `,
})
export class CharactersComponent implements OnInit {
  characters: Character[] = [];

  constructor(private api: VertepApiService) {}

  ngOnInit(): void {
    this.api.getCharacters().subscribe({
      next: (characters) => (this.characters = characters),
      error: () => {},
    });
  }
}
