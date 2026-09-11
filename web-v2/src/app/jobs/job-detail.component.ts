import { Component, OnInit, signal, OnDestroy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, Router, RouterModule } from '@angular/router';
import { VertepApiService } from '../core/api.service';
import { ToastService } from '../core/services/toast.service';
import { ConfirmService } from '../core/services/confirm.service';
import { VertepDatePipe } from '../shared/vertep-date.pipe';
import { Subscription } from 'rxjs';
import { Job, JobUpdate, Character, Brand, Workflow, StageRecord, SceneRecord, AttemptRecord, PublicationResult, Channel } from '../core/models';
import { inStatusGroup, jobActionAllowed, statusLabel, workerStatusLabel, taskTypeLabel, channelLabel } from '../core/presentation';

@Component({
  selector: 'app-job-detail',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterModule, VertepDatePipe],
  template: `
    <div class="space-y-6" data-testid="job-detail-page">
      @if (loading()) {
        <div class="flex items-center justify-center py-12">
          <div class="w-10 h-10 border-4 border-emerald-500 border-t-transparent rounded-full animate-spin"></div>
        </div>
      } @else if (error()) {
        <div class="bg-red-50 border border-red-200 rounded-xl p-6">
          <p class="text-red-700">{{ error() }}</p>
          <button (click)="goBack()" class="mt-3 text-sm text-red-600 hover:text-red-700 font-medium">
            ← Повернутися до списку
          </button>
        </div>
      } @else if (job()) {
        <div class="bg-white rounded-xl border border-slate-200 p-6">
          <div class="flex items-start justify-between mb-6">
            <div>
              <div class="flex items-center gap-3 mb-2">
                <h2 class="text-xl font-semibold text-slate-900">{{ job()!.topic }}</h2>
                <span class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium"
                  [class.bg-emerald-50]="isActiveStatus(job()!.status)"
                  [class.text-emerald-700]="isActiveStatus(job()!.status)"
                  [class.bg-slate-100]="!isActiveStatus(job()!.status)"
                  [class.text-slate-600]="!isActiveStatus(job()!.status)">
                  {{ jobStatusLabel(job()!.status) }}
                </span>
              </div>
              <p class="text-sm text-slate-500">ID: {{ job()!.job_id }}</p>
            </div>
            <div class="flex gap-2 flex-wrap justify-end">
              @if (canPause()) {
                <button (click)="pauseJob()" [disabled]="actionLoading() === 'pause'" class="px-3 py-2 bg-amber-600 text-white rounded-lg hover:bg-amber-700 text-sm font-medium disabled:opacity-50">Пауза</button>
              }
              @if (canResume()) {
                <button (click)="resumeJob()" [disabled]="actionLoading() === 'resume'" class="px-3 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 text-sm font-medium disabled:opacity-50">Відновити</button>
              }
              @if (canRetry()) {
                <button (click)="retryJob()" [disabled]="actionLoading() === 'retry'" class="px-3 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm font-medium disabled:opacity-50">Повторити</button>
              }
              @if (canRegenerate()) {
                <button (click)="confirmRegenerate()" [disabled]="actionLoading() === 'regenerate'" class="px-3 py-2 bg-purple-600 text-white rounded-lg hover:bg-purple-700 text-sm font-medium disabled:opacity-50">Регенерувати</button>
              }
              @if (canCancel()) {
                <button (click)="confirmCancel()" [disabled]="actionLoading() === 'cancel'" class="px-3 py-2 bg-slate-600 text-white rounded-lg hover:bg-slate-700 text-sm font-medium disabled:opacity-50">Скасувати</button>
              }
              @if (canApprove()) {
                <button (click)="approveJob()" [disabled]="actionLoading() === 'approve'" class="px-3 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 text-sm font-medium disabled:opacity-50">Схвалити</button>
              }
              @if (canReviewApproval()) {
                <button (click)="requestRevision()" [disabled]="!!actionLoading()" data-testid="revision-job-button" class="px-3 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm font-medium disabled:opacity-50">Запросити правки</button>
                <button (click)="rejectApproval()" [disabled]="!!actionLoading()" data-testid="reject-job-button" class="px-3 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700 text-sm font-medium disabled:opacity-50">Відхилити</button>
              }
              @if (canPublish()) {
                <button (click)="publishJob()" [disabled]="actionLoading() === 'publish'" class="px-3 py-2 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 text-sm font-medium disabled:opacity-50">Опублікувати</button>
              }
              @if (!editing()) {
                <button (click)="startEditing()" data-testid="edit-job-button"
                  class="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm font-medium">
                  Редагувати
                </button>
              }
              @if (canDelete()) { <button (click)="confirmDelete()" data-testid="delete-job-button"
                class="px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700 text-sm font-medium">
                Видалити
              </button> }
              <button (click)="goBack()" data-testid="back-to-list-button"
                class="px-4 py-2 border border-slate-200 text-slate-600 rounded-lg hover:bg-slate-50 text-sm font-medium">
                ← Список
              </button>
            </div>
          </div>

          @if (actionLoading()) {
            <div class="bg-blue-50 border border-blue-200 rounded-lg p-4 mb-6">
              <div class="flex items-center gap-3">
                <div class="w-5 h-5 border-2 border-blue-500 border-t-transparent rounded-full animate-spin"></div>
                <p class="text-sm text-blue-800">Виконується дія: {{ actionLabel(actionLoading()!) }}...</p>
              </div>
            </div>
          } @else if (actionError()) {
            <div class="bg-red-50 border border-red-200 rounded-lg p-4 mb-6">
              <p class="text-sm text-red-700">{{ actionError() }}</p>
              <button (click)="actionError.set(null)" class="mt-2 text-sm text-red-600 hover:text-red-700 font-medium">Сховати</button>
            </div>
          }

          @if (regenerateWarning()) {
            <div class="bg-amber-50 border border-amber-200 rounded-lg p-4 mb-6">
              <h4 class="text-sm font-medium text-amber-900 mb-2">Увага: Регенерація завдання</h4>
              <p class="text-sm text-amber-800">Будуть видалені: сценарій, сцени та всі артефакти. Завдання повернеться до стану NEW і потребуватиме повторного проходження всіх етапів.</p>
            </div>
          }

          @if (conflict()) {
            <div class="bg-red-50 border border-red-200 rounded-lg p-4 mb-6">
              <h4 class="text-sm font-medium text-red-900 mb-2">Конфлікт версії</h4>
              <p class="text-sm text-red-700">Завдання було змінено іншим користувачем. Перезавантажте актуальний стан перед редагуванням.</p>
              <button (click)="reloadJob()" class="mt-2 text-sm text-red-600 hover:text-red-700 font-medium">Перезавантажити</button>
            </div>
          }

          @if (editing()) {
            <div class="bg-blue-50 border border-blue-200 rounded-lg p-4 mb-6">
              <h4 class="text-sm font-medium text-blue-900 mb-3">Редагування завдання</h4>
                <div class="space-y-4">
                  <div>
                    <label class="block text-sm font-medium text-slate-700 mb-1">Тема</label>
                    <input [(ngModel)]="editForm.topic" data-testid="edit-topic-input"
                      class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500">
                  </div>
                  <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div>
                      <label class="block text-sm font-medium text-slate-700 mb-1">Персонаж</label>
                      <select [(ngModel)]="editForm.character_id" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500">
                        @for (character of characters(); track character.id) {
                          <option [value]="character.id">{{ character.name }} ({{ character.id }})</option>
                        }
                      </select>
                    </div>
                    <div>
                      <label class="block text-sm font-medium text-slate-700 mb-1">Бренд</label>
                      <select [(ngModel)]="editForm.brand_id" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500">
                        <option value="">За замовчуванням</option>
                        @for (brand of brands(); track brand.id) {
                          <option [value]="brand.id">{{ brand.name }} ({{ brand.id }})</option>
                        }
                      </select>
                    </div>
                  </div>
                  <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div>
                      <label class="block text-sm font-medium text-slate-700 mb-1">Workflow</label>
                      <select [(ngModel)]="editForm.workflow" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500">
                        <option value="">Без workflow</option>
                        @for (workflow of workflows(); track workflow.name) {
                          <option [value]="workflow.name">{{ workflow.kind }}/{{ workflow.name }}</option>
                        }
                      </select>
                    </div>
                    <div>
                      <label class="block text-sm font-medium text-slate-700 mb-1">Пріоритет (1-10)</label>
                      <input type="number" [(ngModel)]="editForm.priority" min="1" max="10" data-testid="edit-priority-input"
                        class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500">
                    </div>
                  </div>
                  <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div>
                      <label class="block text-sm font-medium text-slate-700 mb-1">Тип завдання</label>
                      <select [(ngModel)]="editForm.task_type" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500">
                        <option value="image">Зображення</option>
                        <option value="video">Відео</option>
                      </select>
                    </div>
                    <div>
                      <label class="block text-sm font-medium text-slate-700 mb-1">Співвідношення сторін</label>
                      <select [(ngModel)]="editForm.aspect_ratio" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500">
                        <option value="16:9">16:9</option>
                        <option value="9:16">9:16</option>
                      </select>
                    </div>
                  </div>
                  <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div>
                      <label class="block text-sm font-medium text-slate-700 mb-1">Вихідний preset</label>
                      <select [(ngModel)]="editForm.output_preset" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500">
                        <option value="youtube">YouTube</option>
                        <option value="tiktok">TikTok</option>
                        <option value="instagram">Instagram</option>
                        <option value="facebook">Facebook</option>
                      </select>
                    </div>
                    <div class="flex items-center gap-2">
                      <input type="checkbox" [(ngModel)]="editForm.scheduled" id="edit-scheduled">
                      <label for="edit-scheduled" class="text-sm text-slate-700">Запланувати</label>
                    </div>
                  </div>
                  @if (editForm.scheduled) {
                    <div>
                      <label class="block text-sm font-medium text-slate-700 mb-1">Дата та час</label>
                      <input type="datetime-local" [(ngModel)]="editForm.scheduled_for" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500">
                    </div>
                  }
                  <div>
                    <label class="block text-sm font-medium text-slate-700 mb-1">Script (JSON)</label>
                    <textarea [(ngModel)]="editForm.scriptJson" rows="4" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 font-mono text-xs"></textarea>
                  </div>
                  <div>
                    <label class="block text-sm font-medium text-slate-700 mb-1">Prompt першої сцени</label>
                    <textarea [(ngModel)]="editForm.prompt" rows="3" data-testid="edit-prompt-input" class="w-full px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"></textarea>
                  </div>
                <div class="flex gap-2 pt-2">
                  <button (click)="saveChanges()" [disabled]="saving()"
                    class="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm font-medium disabled:opacity-50">
                    {{ saving() ? 'Збереження...' : 'Зберегти' }}
                  </button>
                  <button (click)="cancelEditing()"
                    class="px-4 py-2 border border-slate-200 text-slate-600 rounded-lg hover:bg-slate-50 text-sm font-medium">
                    Скасувати
                  </button>
                </div>
              </div>
            </div>
          }

          <div class="grid grid-cols-2 md:grid-cols-3 gap-4">
            <div class="bg-slate-50 rounded-lg p-4">
              <p class="text-xs text-slate-500 mb-1">Створено</p>
              <p class="text-sm font-medium text-slate-900">{{ job()!.created_at | vertepDate }}</p>
            </div>
            <div class="bg-slate-50 rounded-lg p-4">
              <p class="text-xs text-slate-500 mb-1">Оновлено</p>
              <p class="text-sm font-medium text-slate-900">{{ job()!.created_at | vertepDate }}</p>
            </div>
            <div class="bg-slate-50 rounded-lg p-4">
              <p class="text-xs text-slate-500 mb-1">Персонаж</p>
              <p class="text-sm font-medium text-slate-900">{{ job()!.character_id }}</p>
            </div>
            <div class="bg-slate-50 rounded-lg p-4">
              <p class="text-xs text-slate-500 mb-1">Пріоритет</p>
              <p class="text-sm font-medium text-slate-900">{{ job()!.priority }}</p>
            </div>
            <div class="bg-slate-50 rounded-lg p-4">
              <p class="text-xs text-slate-500 mb-1">Тип завдання</p>
              <p class="text-sm font-medium text-slate-900">{{ taskTypeLabel(job()!.task_type) }}</p>
            </div>
            <div class="bg-slate-50 rounded-lg p-4">
              <p class="text-xs text-slate-500 mb-1">Співвідношення сторін</p>
              <p class="text-sm font-medium text-slate-900">{{ job()!.aspect_ratio }}</p>
            </div>
            <div class="bg-slate-50 rounded-lg p-4">
              <p class="text-xs text-slate-500 mb-1">Формат публікації</p>
              <p class="text-sm font-medium text-slate-900">{{ job()!.output_preset }}</p>
            </div>
            <div class="bg-slate-50 rounded-lg p-4">
              <p class="text-xs text-slate-500 mb-1">Робочий вузол</p>
              <p class="text-sm font-medium text-slate-900">{{ job()!.assigned_worker || '—' }}</p>
            </div>
            <div class="bg-slate-50 rounded-lg p-4">
              <p class="text-xs text-slate-500 mb-1">Публікації</p>
              <p class="text-sm font-medium text-slate-900">{{ job()!.published_to.length ? job()!.published_to.join(', ') : '—' }}</p>
            </div>
            @if (job()!.workflow) {
              <div class="bg-slate-50 rounded-lg p-4 col-span-2">
                <p class="text-xs text-slate-500 mb-1">Workflow</p>
                <p class="text-sm font-medium text-slate-900 font-mono">{{ job()!.workflow }}</p>
              </div>
            }
            @if (job()!.scheduled_for) {
              <div class="bg-slate-50 rounded-lg p-4 col-span-2">
                <p class="text-xs text-slate-500 mb-1">Заплановано на</p>
                <p class="text-sm font-medium text-slate-900">{{ job()!.scheduled_for | vertepDate }}</p>
              </div>
            }
          </div>

          @if (job()!.status === 'SCRIPT_PENDING_APPROVAL' || job()!.status === 'SCRIPT_REVISION_REQUESTED' || job()!.status === 'SCRIPT_FAILED') {
            <div class="mt-6 bg-amber-50 border border-amber-200 rounded-lg p-4" data-testid="job-script">
              <h4 class="font-medium text-amber-900">Сценарій — {{ jobStatusLabel(job()!.status) }}</h4>
              @if (job()!.script; as script) {
                <p class="text-sm text-amber-900 mt-1 font-medium">{{ script['title'] }}</p>
                <p class="text-sm text-amber-800">{{ script['description'] || '' }}</p>
                @if (scriptScenes().length) {
                  <div class="mt-3 space-y-2">
                    @for (scene of scriptScenes(); track $index) {
                      <div class="bg-white rounded p-3 text-sm"><strong>Сцена {{ $index + 1 }}</strong><p class="text-xs text-slate-500">{{ scene['prompt'] }}</p><p>{{ scene['voiceover'] || '' }}</p></div>
                    }
                  </div>
                }
              } @else {
                <p class="text-sm text-amber-700 mt-1">Сценарій у черзі генерації...</p>
              }
              <div class="mt-3 flex gap-2 flex-wrap">
                @if (canApprove() && canReviewScript()) {
                  <button (click)="approveJob()" data-testid="approve-script-button" class="px-3 py-1.5 bg-emerald-600 text-white rounded text-sm">Затвердити сценарій</button>
                  <button (click)="requestRevision()" data-testid="revision-script-button" class="px-3 py-1.5 bg-amber-600 text-white rounded text-sm">Запитати правки</button>
                }
                @if (job()!.status === 'SCRIPT_FAILED') {
                  <button (click)="regenerateScriptAction()" data-testid="regenerate-script-button" class="px-3 py-1.5 bg-blue-600 text-white rounded text-sm">Перегенерувати</button>
                }
              </div>
            </div>
          }

          @if (activeStoryboard(); as storyboard) {
            <div class="mt-6 bg-violet-50 border border-violet-200 rounded-lg p-4" data-testid="job-storyboard">
              <h4 class="font-medium text-violet-900">Розкадровка, версія {{ storyboard.version }} · превʼю v{{ storyboard.image_version }} ({{ storyboard.image_status }})</h4>
              <p class="text-sm text-violet-800">{{ storyboard.title }}</p>
              <div class="mt-3 grid grid-cols-1 md:grid-cols-2 gap-3">
                @for (scene of storyboard.scenes; track scene.index) {
                  <div class="bg-white rounded p-3 text-sm">
                    <strong>Сцена {{ scene.index }} · {{ scene.duration }} с</strong>
                    <p>{{ scene.voiceover }}</p>
                    <p class="text-xs text-slate-500">{{ scene.prompt }}</p>
                    @if (scene.image_artifact_id) {
                      <p class="text-xs text-emerald-600 mt-1">Превʼю: {{ scene.image_artifact_id }} (v{{ scene.image_version }})</p>
                      <a [href]="'/api/jobs/' + job()!.job_id + '/artifacts/' + scene.image_artifact_id + '/download'" class="text-xs text-emerald-600 hover:underline">Відкрити превʼю</a>
                    } @else {
                      <p class="text-xs text-amber-600 mt-1">Превʼю генерується... ({{ storyboard.image_status }})</p>
                    }
                    <div class="mt-2 flex gap-2">
                      <button (click)="regenerateImageScene(scene.index)" class="text-xs px-2 py-1 border rounded">Перегенерувати сцену</button>
                    </div>
                  </div>
                }
              </div>
              <div class="mt-3 flex gap-2 flex-wrap">
                @if (storyboard.image_status === 'ready') {
                  <button (click)="approveImageStoryboard()" class="px-3 py-1.5 bg-emerald-600 text-white rounded text-sm">Затвердити превʼю розкадровки</button>
                }
                <button (click)="regenerateImageStoryboardAll()" class="px-3 py-1.5 bg-blue-600 text-white rounded text-sm">Перегенерувати всі превʼю</button>
                <button (click)="revisionImageStoryboard()" class="px-3 py-1.5 bg-amber-600 text-white rounded text-sm">Правки до превʼю</button>
              </div>
            </div>
          }

          @if (job()!.scenes && job()!.scenes.length > 0) {
            <div data-testid="job-scenes" class="mt-6">
              <h4 class="text-sm font-medium text-slate-900 mb-3">Сцени</h4>
              <div class="space-y-2">
                @for (scene of job()!.scenes; track scene.scene_id; let i = $index) {
                  <div class="bg-slate-50 rounded-lg p-3 flex items-center justify-between">
                    <div>
                      <span class="text-xs text-slate-500">Сцена {{ i + 1 }}</span>
                      <p class="text-sm text-slate-900">{{ scene.prompt.substring(0, 100) || 'Без опису' }}...</p>
                    </div>
                    <span class="px-2 py-1 rounded-full text-xs font-medium"
                      [class.bg-emerald-50]="scene.status === 'RUNNING'"
                      [class.text-emerald-700]="scene.status === 'RUNNING'"
                      [class.bg-blue-50]="scene.status === 'READY'"
                      [class.text-blue-700]="scene.status === 'READY'"
                      [class.bg-red-50]="scene.status === 'FAILED'"
                      [class.text-red-700]="scene.status === 'FAILED'"
                      [class.bg-slate-100]="!['RUNNING', 'READY', 'FAILED'].includes(scene.status)"
                      [class.text-slate-600]="!['RUNNING', 'READY', 'FAILED'].includes(scene.status)">
                      {{ jobStatusLabel(scene.status) }}
                    </span>
                  </div>
                }
              </div>
            </div>
          }

          @if (job()!.artifacts && job()!.artifacts.length > 0) {
            <div data-testid="job-artifacts" class="mt-6">
              <h4 class="text-sm font-medium text-slate-900 mb-3">Артефакти</h4>
              <div class="space-y-2">
                @for (artifact of job()!.artifacts; track artifact.artifact_id) {
                  <div class="bg-slate-50 rounded-lg p-3 flex items-center justify-between">
                    <div>
                      <span class="text-xs text-slate-500">{{ artifact.kind }}</span>
                      <p class="text-sm text-slate-900">{{ artifact.filename }}</p>
                      <span class="text-xs px-2 py-0.5 rounded-full"
                            [class.bg-emerald-50]="artifact.valid"
                            [class.text-emerald-700]="artifact.valid"
                            [class.bg-red-50]="!artifact.valid"
                            [class.text-red-700]="!artifact.valid">
                        {{ artifact.valid ? 'Валідний' : 'Невалідний' }}
                      </span>
                    </div>
                    <div class="flex gap-2">
                      <button (click)="verifyArtifact(artifact.artifact_id)" [disabled]="verifying() === artifact.artifact_id" class="text-xs px-2 py-1 border border-slate-200 rounded hover:bg-slate-100 disabled:opacity-50">
                        {{ verifying() === artifact.artifact_id ? 'Перевірка...' : 'Перевірити' }}
                      </button>
                      @if (artifact.path) {
                        <a [href]="'/api/jobs/' + job()!.job_id + '/artifacts/' + artifact.artifact_id + '/download'"
                           class="text-xs text-emerald-600 hover:text-emerald-700 font-medium px-2 py-1">Завантажити</a>
                      }
                    </div>
                  </div>
                }
              </div>
              <div class="mt-3 flex gap-2">
                <label class="text-xs px-3 py-2 border border-slate-200 rounded-lg cursor-pointer hover:bg-slate-50">
                  Завантажити reference
                  <input type="file" class="hidden" (change)="uploadArtifact($event, 'references')">
                </label>
                <label class="text-xs px-3 py-2 border border-slate-200 rounded-lg cursor-pointer hover:bg-slate-50">
                  Завантажити audio
                  <input type="file" class="hidden" (change)="uploadArtifact($event, 'audio')">
                </label>
                <button (click)="exportJob()" class="text-xs px-3 py-2 border border-slate-200 rounded-lg hover:bg-slate-50">Експорт</button>
                <label class="text-xs px-3 py-2 border border-slate-200 rounded-lg cursor-pointer hover:bg-slate-50">
                  Імпорт
                  <input type="file" class="hidden" (change)="importProject($event)">
                </label>
              </div>
            </div>
          }

           @if (canPublish()) {
             <div class="mt-6 bg-slate-50 rounded-lg p-4">
               <h4 class="text-sm font-medium text-slate-900 mb-3">Публікація</h4>
               <div class="space-y-3">
                 <div>
                   <label class="block text-sm font-medium text-slate-700 mb-1">Канали</label>
                   <div class="flex flex-wrap gap-2">
                     @for (channel of availablePublishChannels(); track channel) {
                       <label class="flex items-center gap-1.5 text-sm">
                         <input type="checkbox" [value]="channel" (change)="onChannelToggle(channel, $event)">
                         {{ channelLabel(channel) }}
                       </label>
                     }
                   </div>
                 </div>
                 <div class="flex items-center gap-2">
                   <input type="checkbox" id="approve-before-publish" [(ngModel)]="approveBeforePublish">
                   <label for="approve-before-publish" class="text-sm text-slate-700">Схвалювати перед публікацією</label>
                 </div>
                 <button (click)="publishJob()" [disabled]="actionLoading() === 'publish'" class="px-4 py-2 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 text-sm font-medium disabled:opacity-50">
                   {{ actionLoading() === 'publish' ? 'Публікація...' : 'Опублікувати' }}
                 </button>
               </div>
             </div>
           }

           @if (hasPublicationResults()) {
             <div class="mt-6">
               <h4 class="text-sm font-medium text-slate-900 mb-3">Результати публікації</h4>
               <div class="space-y-2">
                 @for (result of publicationResults(); track result.channel) {
                   <div class="bg-slate-50 rounded-lg p-3 flex items-center justify-between">
                     <div>
                       <span class="text-sm font-medium text-slate-900">{{ channelLabel(result.channel) }}</span>
                       @if (result.url) {
                         <a [href]="result.url" target="_blank" class="text-xs text-emerald-600 hover:text-emerald-700 ml-2">{{ result.url }}</a>
                       }
                       @if (result.error) {
                         <p class="text-xs text-red-600 mt-0.5">{{ result.error }}</p>
                       }
                     </div>
                     <div class="flex items-center gap-2">
                       <span class="px-2 py-1 rounded-full text-xs font-medium"
                             [class.bg-emerald-50]="result.status === 'PUBLISHED'"
                             [class.text-emerald-700]="result.status === 'PUBLISHED'"
                             [class.bg-red-50]="result.status === 'FAILED' || result.status === 'NOT_CONFIGURED'"
                             [class.text-red-700]="result.status === 'FAILED' || result.status === 'NOT_CONFIGURED'"
                             [class.bg-amber-50]="result.status !== 'PUBLISHED' && result.status !== 'FAILED' && result.status !== 'NOT_CONFIGURED'"
                             [class.text-amber-700]="result.status !== 'PUBLISHED' && result.status !== 'FAILED' && result.status !== 'NOT_CONFIGURED'">
                         {{ result.status }}
                       </span>
                       @if (result.status === 'FAILED') {
                         <button (click)="retryFailedChannel(result.channel)" [disabled]="actionLoading() === 'retry-' + result.channel" class="text-xs px-2 py-1 bg-amber-600 text-white rounded hover:bg-amber-700 disabled:opacity-50">
                           {{ actionLoading() === 'retry-' + result.channel ? 'Повтор...' : 'Повторити' }}
                         </button>
                       }
                     </div>
                   </div>
                 }
               </div>
             </div>
           }

          @if (hasTimeline()) {
            <div class="mt-6">
              <h4 class="text-sm font-medium text-slate-900 mb-3">Timeline</h4>
              <div class="bg-slate-50 rounded-lg p-4 space-y-4">
                @for (stageEntry of timelineStages(); track stageEntry.name) {
                  <div class="flex gap-3">
                    <div class="flex flex-col items-center">
                      <div class="w-3 h-3 rounded-full"
                           [class.bg-emerald-500]="stageEntry.status === 'READY'"
                           [class.bg-blue-500]="stageEntry.status === 'RUNNING'"
                           [class.bg-red-500]="stageEntry.status === 'FAILED'"
                           [class.bg-slate-400]="!['READY', 'RUNNING', 'FAILED'].includes(stageEntry.status)"></div>
                      @if (hasNextStage(stageEntry)) {
                        <div class="w-0.5 h-8 bg-slate-200"></div>
                      }
                    </div>
                    <div class="flex-1 pb-4">
                      <div class="flex items-center gap-2">
                        <span class="text-sm font-medium text-slate-900">{{ stageLabel(stageEntry.name) }}</span>
                        <span class="px-2 py-0.5 rounded-full text-xs font-medium"
                              [class.bg-emerald-50]="stageEntry.status === 'READY'"
                              [class.text-emerald-700]="stageEntry.status === 'READY'"
                              [class.bg-blue-50]="stageEntry.status === 'RUNNING'"
                              [class.text-blue-700]="stageEntry.status === 'RUNNING'"
                              [class.bg-red-50]="stageEntry.status === 'FAILED'"
                              [class.text-red-700]="stageEntry.status === 'FAILED'"
                              [class.bg-slate-100]="!['READY', 'RUNNING', 'FAILED'].includes(stageEntry.status)"
                              [class.text-slate-600]="!['READY', 'RUNNING', 'FAILED'].includes(stageEntry.status)">
                          {{ stageEntry.status }}
                        </span>
                      </div>
                      @if (stageEntry.started_at) {
                        <p class="text-xs text-slate-500 mt-0.5">Старт: {{ stageEntry.started_at | vertepDate }}</p>
                      }
                      @if (stageEntry.completed_at) {
                        <p class="text-xs text-slate-500">Завершено: {{ stageEntry.completed_at | vertepDate }}</p>
                      }
                      @if (stageEntry.attempts.length) {
                        <div class="mt-2 space-y-1">
                          @for (attempt of stageEntry.attempts; track attempt.attempt) {
                            <div class="text-xs text-slate-600">
                              Спроба #{{ attempt.attempt }}: {{ attempt.status }} {{ attempt.node_name ? 'на ' + attempt.node_name : '' }} {{ attempt.error ? '— ' + attempt.error : '' }}
                            </div>
                          }
                        </div>
                      }
                      @if (stageScenes(stageEntry.name).length) {
                        <div class="mt-2 space-y-1">
                          @for (scene of stageScenes(stageEntry.name); track scene.scene_id) {
                            <div class="text-xs text-slate-600 bg-white rounded p-2">
                              <span class="font-medium">Сцена {{ scene.index }}:</span> {{ scene.prompt.substring(0, 80) || 'Без опису' }}...
                              @if (scene.assigned_worker) {
                                <span class="text-slate-400">({{ scene.assigned_worker }})</span>
                              }
                            </div>
                          }
                        </div>
                      }
                    </div>
                  </div>
                }
              </div>
            </div>
          }

          @if (job()!.events && job()!.events.length > 0) {
            <div data-testid="job-events" class="mt-6">
              <h4 class="text-sm font-medium text-slate-900 mb-3">Історія подій</h4>
              <div class="bg-slate-50 rounded-lg p-3 max-h-48 overflow-y-auto">
                @for (event of structuredEvents(); track event.key) {
                  <div class="text-xs text-slate-600 font-mono py-0.5">
                    @if (event.timestamp) {
                      <span class="text-slate-400">{{ event.timestamp | vertepDate }}</span>
                    }
                    {{ event.message }}
                  </div>
                }
              </div>
            </div>
          }
        </div>
      }
    </div>
  `,
})
export class JobDetailComponent implements OnInit, OnDestroy {
  job = signal<Job | null>(null);
  scriptScenes(): Record<string, unknown>[] {
    const scenes = this.job()?.script?.['scenes'];
    return Array.isArray(scenes)
      ? scenes.filter((scene): scene is Record<string, unknown> =>
          typeof scene === 'object' && scene !== null && !Array.isArray(scene))
      : [];
  }
  loading = signal(true);
  error = signal<string | null>(null);
  editing = signal(false);
  saving = signal(false);
  actionLoading = signal<string | null>(null);
  actionError = signal<string | null>(null);
  regenerateWarning = signal(false);
  conflict = signal(false);
  verifying = signal<string | null>(null);
  characters = signal<Character[]>([]);
  brands = signal<Brand[]>([]);
  workflows = signal<Workflow[]>([]);
  channelTypes = signal<string[]>([]);
  jobChannels = signal<Channel[]>([]);
  editForm: Partial<JobUpdate> & { scriptJson?: string; brand_id?: string; aspect_ratio?: string; output_preset?: string; task_type?: string; scheduled?: boolean; scheduled_for?: string } = { topic: '', priority: 5, workflow: '', character_id: '', prompt: '', brand_id: '', aspect_ratio: '16:9', output_preset: 'youtube', task_type: 'image', scheduled: false, scheduled_for: '' };
  private subs = new Subscription();

  constructor(
    private route: ActivatedRoute,
    private router: Router,
    private api: VertepApiService,
    private toast: ToastService,
    private confirm: ConfirmService,
  ) {}

  ngOnInit(): void {
    const jobId = this.route.snapshot.paramMap.get('id');
    if (jobId) {
      this.loadJob(jobId);
    } else {
      this.error.set('ID завдання не вказано');
      this.loading.set(false);
    }
    this.loadCharacters();
    this.loadBrands();
    this.loadWorkflows();
    this.loadChannelTypes();
  }

  ngOnDestroy(): void {
    this.subs.unsubscribe();
  }

  loadJob(jobId: string): void {
    this.loading.set(true);
    this.error.set(null);
    this.conflict.set(false);
    this.subs.add(
      this.api.getJob(jobId).subscribe({
        next: (job) => {
          this.job.set(job as Job);
          this.loadJobChannels(job as Job);
          this.loading.set(false);
        },
        error: (err) => {
          this.error.set(err.message || 'Не вдалося завантажити завдання');
          this.loading.set(false);
        },
      }),
    );
  }

  reloadJob(): void {
    const jobId = this.route.snapshot.paramMap.get('id');
    if (jobId) {
      this.loadJob(jobId);
    }
  }

  loadCharacters(): void {
    this.api.getCharacters().subscribe({
      next: (characters) => this.characters.set(characters),
      error: () => {},
    });
  }

  loadBrands(): void {
    this.api.getBrands().subscribe({
      next: (brands) => this.brands.set(brands),
      error: () => {},
    });
  }

  loadWorkflows(): void {
    this.api.getWorkflows().subscribe({
      next: (workflows) => this.workflows.set(workflows),
      error: () => {},
    });
  }

  loadChannelTypes(): void {
    this.api.getChannelTypes().subscribe({
      next: (types) => this.channelTypes.set(types),
      error: () => this.channelTypes.set(['youtube', 'tiktok', 'instagram', 'facebook', 'threads']),
    });
  }

  loadJobChannels(job: Job): void {
    if (job.brand_id) {
      this.api.getChannels(job.brand_id).subscribe({
        next: (channels) => this.jobChannels.set(channels),
        error: () => this.jobChannels.set([]),
      });
    }
  }

  isActiveStatus(status: string): boolean {
    return inStatusGroup(status, 'active');
  }

  jobStatusLabel(status: string): string { return statusLabel(status); }
  taskTypeLabel(value: string): string { return taskTypeLabel(value); }
  channelLabel(value: string): string { return channelLabel(value); }

  startEditing(): void {
    const j = this.job();
    if (!j) return;
    this.editForm = {
      topic: j.topic,
      priority: j.priority,
      workflow: j.workflow || '',
      character_id: j.character_id,
      brand_id: j.brand_id || '',
      aspect_ratio: j.aspect_ratio || '16:9',
      output_preset: j.output_preset || 'youtube',
      task_type: j.task_type || 'image',
      scheduled: !!j.scheduled_for,
      scheduled_for: j.scheduled_for ? j.scheduled_for.slice(0, 16) : '',
      scriptJson: j.script ? JSON.stringify(j.script, null, 2) : '',
      prompt: String((j.script?.['scenes'] as Array<Record<string, unknown>> | undefined)?.[0]?.['prompt'] || ''),
    };
    this.editing.set(true);
    this.conflict.set(false);
  }

  cancelEditing(): void {
    this.editing.set(false);
  }

  saveChanges(): void {
    const j = this.job();
    if (!j) return;
    this.saving.set(true);
    this.conflict.set(false);
    const payload: JobUpdate = {
      expected_version: j.version,
      topic: this.editForm.topic,
      priority: this.editForm.priority,
      character_id: this.editForm.character_id || j.character_id,
      prompt: this.editForm.prompt || undefined,
    };
    if (this.editForm.workflow) {
      payload.workflow = this.editForm.workflow;
    }
    if (this.editForm.scriptJson) {
      try {
        payload.script = JSON.parse(this.editForm.scriptJson);
      } catch {
        this.toast.show('Невірний JSON у полі script', 'error');
        this.saving.set(false);
        return;
      }
    }
    this.subs.add(
      this.api.updateJob(j.job_id, payload).subscribe({
        next: (updated: Job) => {
          this.job.set(updated);
          this.editing.set(false);
          this.saving.set(false);
          this.toast.show('Завдання оновлено', 'success');
        },
        error: (err: { status?: number; message?: string }) => {
          if (err.status === 409) {
            this.conflict.set(true);
          } else {
            this.toast.show(err.message || 'Помилка оновлення', 'error');
          }
          this.saving.set(false);
        },
      }),
    );
  }

  confirmDelete(): void {
    const j = this.job();
    if (!j) return;
    this.confirm.confirm({
      title: 'Видалити завдання',
      message: `Ви впевнені, що хочете видалити "${j.topic}"?`,
    }).subscribe((ok) => {
      if (!ok) return;
      this.subs.add(
        this.api.deleteJob(j.job_id).subscribe({
          next: () => {
            this.toast.show('Завдання видалено', 'success');
            this.router.navigate(['/jobs']);
          },
          error: (err) => {
            this.toast.show(err.message || 'Помилка видалення', 'error');
          },
        }),
      );
    });
  }

  goBack(): void {
    this.router.navigate(['/jobs']);
  }

  canPause(): boolean {
    return jobActionAllowed('pause', this.job()?.status);
  }

  canResume(): boolean {
    return jobActionAllowed('resume', this.job()?.status);
  }

  canRetry(): boolean {
    return jobActionAllowed('retry', this.job()?.status);
  }

  canRegenerate(): boolean {
    return jobActionAllowed('regenerate', this.job()?.status);
  }

  canCancel(): boolean {
    return jobActionAllowed('cancel', this.job()?.status);
  }

  canApprove(): boolean {
    const j = this.job();
    if (!j) return false;
    if (this.canReviewScript() || this.canReviewStoryboard()) return true;
    return (j.status === 'READY' || j.status === 'PENDING_APPROVAL') && !j.approved;
  }

  canReviewApproval(): boolean {
    const s = this.job()?.status;
    return s === 'PENDING_APPROVAL' || this.canReviewScript() || this.canReviewStoryboard();
  }

  canReviewStoryboard(): boolean {
    const j = this.job();
    return !!j && j.status === 'STORYBOARD_PENDING_APPROVAL' && !!j.active_storyboard_version;
  }

  canReviewScript(): boolean {
    const s = this.job()?.status;
    return s === 'SCRIPT_PENDING_APPROVAL' || s === 'SCRIPT_REVISION_REQUESTED';
  }

  canDelete(): boolean { return jobActionAllowed('delete', this.job()?.status); }
  activeStoryboard() {
    const j = this.job();
    return j?.storyboards?.find(item => item.version === j.active_storyboard_version) || null;
  }

  approveImageStoryboard(): void {
    const j = this.job(); const sb = this.activeStoryboard(); if (!j || !sb) return;
    this.runAction('approve', () => this.api.approveImageStoryboard(j.job_id, sb.version));
  }
  regenerateImageScene(index: number): void {
    const j = this.job(); const sb = this.activeStoryboard(); if (!j || !sb) return;
    this.runAction('approve', () => this.api.revisionImageStoryboard(j.job_id, sb.version, { scene_indexes: [index] }));
  }
  regenerateImageStoryboardAll(): void {
    const j = this.job(); const sb = this.activeStoryboard(); if (!j || !sb) return;
    this.runAction('approve', () => this.api.regenerateImageStoryboard(j.job_id, sb.version));
  }
  revisionImageStoryboard(): void {
    const rev = window.prompt('Опишіть правки до превʼю (prompt для сцени):');
    if (rev === null) return;
    const j = this.job(); const sb = this.activeStoryboard(); if (!j || !sb) return;
    this.runAction('approve', () => this.api.revisionImageStoryboard(j.job_id, sb.version, { revision: rev }));
  }

  actionLabel(action: string): string {
    const labels: Record<string, string> = {
      pause: 'Пауза',
      resume: 'Відновлення',
      retry: 'Повтор',
      regenerate: 'Регенерація',
      cancel: 'Скасування',
      approve: 'Схвалення',
      revision: 'Правки',
      reject: 'Відхилення',
    };
    return labels[action] || action;
  }

  pauseJob(): void { this.runAction('pause', () => this.api.pauseJob(this.job()!.job_id)); }
  resumeJob(): void { this.runAction('resume', () => this.api.resumeJob(this.job()!.job_id)); }
  approveJob(): void {
    const j = this.job()!;
    if (this.canReviewScript()) {
      this.runAction('approve', () => this.api.approveScript(j.job_id));
      return;
    }
    this.runAction('approve', () => this.canReviewStoryboard()
      ? this.api.approveStoryboard(j.job_id, j.active_storyboard_version!)
      : this.api.approveJob(j.job_id));
  }

  requestScriptRevision(): void {
    const revision = window.prompt('Опишіть потрібні зміни до сценарію:');
    if (revision?.trim()) this.runAction('revision', () => this.api.requestScriptRevision(this.job()!.job_id, revision.trim()));
  }

  regenerateScriptAction(): void {
    const revision = window.prompt('Залиште коментар для регенерації (необовʼязково):') || undefined;
    this.runAction('regenerate', () => this.api.regenerateScript(this.job()!.job_id, revision));
  }

  rejectStoryboard(): void {
    const j = this.job();
    if (!j?.active_storyboard_version) return;
    this.confirm.confirm({ title: 'Відхилити розкадровку', message: 'Відхилити поточну версію розкадровки?' }).subscribe(ok => {
      if (ok) this.runAction('reject', () => this.api.rejectStoryboard(j.job_id, j.active_storyboard_version!));
    });
  }

  requestStoryboardRevision(): void {
    const j = this.job();
    if (!j?.active_storyboard_version) return;
    const revision = window.prompt('Опишіть потрібні зміни до розкадровки:');
    if (revision?.trim()) this.runAction('revision', () => this.api.regenerateStoryboard(j.job_id, j.active_storyboard_version!, revision.trim()));
  }

  rejectApproval(): void {
    if (this.canReviewScript()) {
      this.confirm.confirm({ title: 'Відхилити сценарій', message: 'Скасувати завдання зі сценарієм?' }).subscribe(ok => {
        if (ok) this.runAction('reject', () => this.api.cancelJob(this.job()!.job_id));
      });
      return;
    }
    if (this.canReviewStoryboard()) {
      this.rejectStoryboard();
      return;
    }
    this.confirm.confirm({ title: 'Відхилити завдання', message: 'Відхилити це завдання?' }).subscribe(ok => {
      if (ok) this.runAction('reject', () => this.api.cancelJob(this.job()!.job_id));
    });
  }

  requestRevision(): void {
    if (this.canReviewScript()) {
      this.requestScriptRevision();
      return;
    }
    if (this.canReviewStoryboard()) {
      this.requestStoryboardRevision();
      return;
    }
    this.confirm.confirm({ title: 'Запросити правки', message: 'Повернути завдання на повторну генерацію?' }).subscribe(ok => {
      if (ok) this.runAction('revision', () => this.api.regenerateJob(this.job()!.job_id));
    });
  }

  retryJob(): void {
    this.confirm.confirm({ title: 'Повторити завдання', message: 'Спробувати виконати завдання ще раз?' }).subscribe((ok) => {
      if (!ok) return;
      this.runAction('retry', () => this.api.retryJob(this.job()!.job_id));
    });
  }

  confirmRegenerate(): void {
    this.regenerateWarning.set(true);
    this.confirm.confirm({
      title: 'Регенерувати завдання',
      message: 'Будуть видалені: сценарій, сцени та всі артефакти. Завдання повернеться до стану NEW. Продовжити?',
    }).subscribe((ok) => {
      this.regenerateWarning.set(false);
      if (!ok) return;
      this.runAction('regenerate', () => this.api.regenerateJob(this.job()!.job_id));
    });
  }

  confirmCancel(): void {
    this.confirm.confirm({ title: 'Скасувати завдання', message: 'Ви впевнені, що хочете скасувати це завдання?' }).subscribe((ok) => {
      if (!ok) return;
      this.runAction('cancel', () => this.api.cancelJob(this.job()!.job_id));
    });
  }

  private runAction(action: string, fn: () => any): void {
    const j = this.job();
    if (!j) return;
    this.actionLoading.set(action);
    this.actionError.set(null);
    this.subs.add(
      fn().subscribe({
        next: (updated: Job) => {
          this.job.set(updated);
          this.actionLoading.set(null);
          this.toast.show(`Дію "${this.actionLabel(action)}" застосовано`, 'success');
        },
        error: (err: { status?: number; message?: string }) => {
          if (err.status === 409) {
            this.conflict.set(true);
          }
          this.actionError.set(err.message || `Помилка виконання дії "${this.actionLabel(action)}"`);
          this.actionLoading.set(null);
          this.toast.show(err.message || 'Помилка виконання дії', 'error');
        },
      }),
    );
  }

  hasTimeline(): boolean {
    const j = this.job();
    if (!j) return false;
    return !!j.stages || j.scenes.length > 0 || j.events.length > 0;
  }

  timelineStages(): Array<{ name: string; status: string; attempts: AttemptRecord[]; started_at?: string; completed_at?: string }> {
    const j = this.job();
    if (!j) return [];
    const order = ['SCRIPT', 'ASSETS', 'TTS', 'ASSEMBLY', 'PUBLISH'];
    const stages = Object.values(j.stages || {});
    return order
      .filter(name => stages.some(s => s.name === name))
      .map(name => {
        const stage = stages.find(s => s.name === name)!;
        return {
          name: stage.name,
          status: stage.status,
          attempts: stage.attempts || [],
          started_at: stage.started_at,
          completed_at: stage.completed_at,
        };
      });
  }

  stageScenes(stageName: string): SceneRecord[] {
    const j = this.job();
    if (!j) return [];
    return j.scenes.filter(s => {
      if (stageName === 'SCRIPT') return s.status === 'PENDING' || s.status === 'RUNNING' || s.status === 'READY';
      if (stageName === 'ASSETS') return s.status === 'ASSET_GENERATION' || s.status === 'ASSETS_READY';
      if (stageName === 'TTS') return s.status === 'TTS';
      if (stageName === 'ASSEMBLY') return s.status === 'ASSEMBLY';
      if (stageName === 'PUBLISH') return s.status === 'PUBLISHING' || s.status === 'PUBLISHED';
      return false;
    });
  }

  hasNextStage(current: { name: string }): boolean {
    const stages = this.timelineStages();
    const idx = stages.findIndex(s => s.name === current.name);
    return idx >= 0 && idx < stages.length - 1;
  }

  stageLabel(name: string): string {
    const labels: Record<string, string> = {
      SCRIPT: 'Сценарій',
      ASSETS: 'Активи',
      TTS: 'Голос',
      ASSEMBLY: 'Монтаж',
      PUBLISH: 'Публікація',
    };
    return labels[name] || name;
  }

  structuredEvents(): Array<{ key: string; message: string; timestamp?: string }> {
    const j = this.job();
    if (!j) return [];
    return j.events.map((event, idx) => {
      const timestampMatch = event.match(/^(\d{4}-\d{2}-\d{2}T[\d:]+Z?)\s+/);
      if (timestampMatch) {
        return {
          key: `${idx}-${timestampMatch[1]}`,
          timestamp: timestampMatch[1],
          message: event.slice(timestampMatch[0].length),
        };
      }
      return {
        key: `${idx}-${event}`,
        message: event,
      };
    });
  }

  verifyArtifact(artifactId: string): void {
    const j = this.job();
    if (!j) return;
    this.verifying.set(artifactId);
    this.api.verifyArtifacts(j.job_id).subscribe({
      next: (result) => {
        const updated = { ...j, artifacts: j.artifacts.map(a => {
          const v = result.results.find(r => r.artifact_id === a.artifact_id);
          return { ...a, valid: v ? v.valid : a.valid };
        })};
        this.job.set(updated as Job);
        this.verifying.set(null);
        this.toast.show('Інтегральність перевірено', 'success');
      },
      error: (err) => {
        this.toast.show(err.message || 'Помилка перевірки', 'error');
        this.verifying.set(null);
      },
    });
  }

  uploadArtifact(event: Event, folder: 'references' | 'audio'): void {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    const j = this.job();
    if (!j) return;
    this.api.uploadArtifact(j.job_id, folder, file.name, file).subscribe({
      next: (artifact) => {
        const updated = { ...j, artifacts: [...j.artifacts, artifact]};
        this.job.set(updated as Job);
        this.toast.show('Файл завантажено', 'success');
      },
      error: (err) => this.toast.show(err.message || 'Помилка завантаження', 'error'),
    });
  }

  exportJob(): void {
    const j = this.job();
    if (!j) return;
    this.api.exportJob(j.job_id).subscribe({
      next: (blob) => {
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `job-${j.job_id}.zip`;
        a.click();
        URL.revokeObjectURL(url);
      },
      error: (err) => this.toast.show(err.message || 'Помилка експорту', 'error'),
    });
  }

  importProject(event: Event): void {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    this.api.importProject(file).subscribe({
      next: () => {
        this.toast.show('Проект імпортовано', 'success');
        this.loadJob(this.route.snapshot.paramMap.get('id') || '');
      },
      error: (err) => this.toast.show(err.message || 'Помилка імпорту', 'error'),
    });
  }

  canPublish(): boolean {
    const j = this.job();
    return !!j && jobActionAllowed('publish', j.status);
  }

  canRetryPublish(): boolean {
    const j = this.job();
    if (!j || j.status !== 'FAILED') return false;
    return !!j.publication_results && Object.keys(j.publication_results).length > 0;
  }

  approveBeforePublish = signal(false);
  selectedChannels = signal<string[]>([]);

  onChannelToggle(channel: string, event: Event): void {
    const checked = (event.target as HTMLInputElement).checked;
    this.toggleChannel(channel, checked);
  }

  toggleChannel(channel: string, checked: boolean): void {
    if (checked) {
      this.selectedChannels.update(list => [...list, channel]);
    } else {
      this.selectedChannels.update(list => list.filter(c => c !== channel));
    }
  }

  availablePublishChannels(): string[] {
    const types = this.channelTypes();
    if (types.length) return types;
    const configured = this.jobChannels().map(c => c.channel_type);
    return configured.length ? Array.from(new Set(configured)) : ['youtube', 'tiktok', 'instagram', 'facebook', 'threads'];
  }

  publishJob(): void {
    const j = this.job();
    if (!j) return;
    this.actionLoading.set('publish');
    const channels = this.selectedChannels().length ? this.selectedChannels() : undefined;
    this.api.publishJob(j.job_id, channels).subscribe({
      next: (updated) => {
        this.job.set(updated);
        this.actionLoading.set(null);
        this.selectedChannels.set([]);
        this.toast.show('Публікацію запущено', 'success');
      },
      error: (err) => {
        this.actionError.set(err.message || 'Помилка публікації');
        this.actionLoading.set(null);
        this.toast.show(err.message || 'Помилка публікації', 'error');
      },
    });
  }

  retryFailedChannel(channel: string): void {
    const j = this.job();
    if (!j) return;
    this.actionLoading.set('retry-' + channel);
    this.api.publishJob(j.job_id, [channel]).subscribe({
      next: (updated) => {
        this.job.set(updated);
        this.actionLoading.set(null);
        this.toast.show(`Канал ${channel} повторно опубліковано`, 'success');
      },
      error: (err) => {
        this.actionError.set(err.message || 'Помилка повторення публікації');
        this.actionLoading.set(null);
        this.toast.show(err.message || 'Помилка повторення публікації', 'error');
      },
    });
  }

  publicationResults(): PublicationResult[] {
    const j = this.job();
    if (!j) return [];
    const results: PublicationResult[] = [];
    for (const channel in j.publication_results || {}) {
      const result = (j.publication_results || {})[channel] as PublicationResult;
      results.push({
        channel,
        status: result?.status || 'unknown',
        url: result?.url,
        error: result?.error,
      });
    }
    return results;
  }

  hasPublicationResults(): boolean {
    const j = this.job();
    if (!j || !j.publication_results) return false;
    return Object.keys(j.publication_results).length > 0;
  }
}
