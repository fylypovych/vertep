import { TPipe } from '../core/i18n/t.pipe';
import { Component, OnInit, signal, OnDestroy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, Router, RouterModule } from '@angular/router';
import { JobsApiService } from '../core/api/jobs.api';
import { ResourcesApiService } from '../core/api/resources.api';
import { WorkersApiService } from '../core/api/workers.api';
import { ToastService } from '../core/services/toast.service';
import { ConfirmService } from '../core/services/confirm.service';
import { VertepDatePipe } from '../shared/vertep-date.pipe';
import { Subscription } from 'rxjs';
import { Job, JobUpdate, Character, Brand, Workflow, StageRecord, SceneRecord, AttemptRecord, PublicationResult, Channel } from '../core/models';
import { PolicyService } from '../core/services/policy.service';
import { inStatusGroup, jobActionAllowed, statusLabel, workerStatusLabel, taskTypeLabel, channelLabel } from '../core/presentation';

@Component({
  selector: 'app-job-detail',
  standalone: true,
  imports: [TPipe, CommonModule, FormsModule, RouterModule, VertepDatePipe],
  templateUrl: './job-detail.component.html',
})
export class JobDetailComponent implements OnInit, OnDestroy {
  job = signal<Job | null>(null);
  videoArtifactUrl = signal<string | null>(null);
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
  conflictLocalVersion = signal<number | null>(null);
  conflictActualVersion = signal<number | null>(null);
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
    private jobsApi: JobsApiService,
    private resources: ResourcesApiService,
    private workersApi: WorkersApiService,
    private toast: ToastService,
    private confirm: ConfirmService,
    readonly policy: PolicyService,
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
    this.conflictLocalVersion.set(null);
    this.conflictActualVersion.set(null);
    this.subs.add(
      this.jobsApi.get(jobId).subscribe({
        next: (job) => {
          this.job.set(job as Job);
          if (this.route.snapshot.queryParamMap.get('edit') === '1' && !this.editing()
              && this.policy.can('update_job').allowed) this.startEditing();
          this.loadJobChannels(job as Job);
          if ((job as Job).output_path) {
            this.videoArtifactUrl.set(`/api/jobs/${encodeURIComponent((job as Job).job_id)}/final/video.mp4`);
          }
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

  reapplyAfterConflict(): void {
    const jobId = this.route.snapshot.paramMap.get('id');
    if (!jobId) return;
    this.conflict.set(false);
    this.conflictLocalVersion.set(null);
    this.conflictActualVersion.set(null);
    this.jobsApi.get(jobId).subscribe({
      next: (fresh) => {
        this.job.set(fresh as Job);
        this.loadJobChannels(fresh as Job);
        if (this.editing()) {
          // Re-submit the still-open form against the fresh version.
          this.saveChanges();
        }
      },
      error: (err) => this.toast.show(err.message || 'Не вдалося перезавантажити завдання', 'error'),
    });
  }

  loadCharacters(): void {
    this.resources.characters().subscribe({
      next: (characters) => this.characters.set(characters),
      error: () => {},
    });
  }

  loadBrands(): void {
    this.resources.brands().subscribe({
      next: (brands) => this.brands.set(brands),
      error: () => {},
    });
  }

  loadWorkflows(): void {
    this.resources.workflows().subscribe({
      next: (workflows) => this.workflows.set(workflows),
      error: () => {},
    });
  }

  loadChannelTypes(): void {
    this.resources.channelTypes().subscribe({
      next: (types) => this.channelTypes.set(types),
      error: () => this.channelTypes.set(['youtube', 'tiktok', 'instagram', 'facebook', 'threads']),
    });
  }

  loadJobChannels(job: Job): void {
    if (job.brand_id) {
      this.resources.channels(job.brand_id).subscribe({
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
      this.jobsApi.update(j.job_id, payload).subscribe({
        next: (updated: Job) => {
          this.job.set(updated);
          this.editing.set(false);
          this.saving.set(false);
          this.toast.show('Завдання оновлено', 'success');
        },
        error: (err: { status?: number; message?: string }) => {
          if (err.status === 409) {
            this.conflict.set(true);
            this.conflictLocalVersion.set(j.version);
            const match = (err.message || '').match(/current version is (\d+)/);
            this.conflictActualVersion.set(match ? Number(match[1]) : null);
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
        this.jobsApi.delete(j.job_id).subscribe({
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
    return !!j && (j.status === 'STORYBOARD_PENDING_APPROVAL' || j.status === 'STORYBOARD_READY')
      && !!j.active_storyboard_version;
  }

   canReviewScript(): boolean {
    const s = this.job()?.status;
    return s === 'SCRIPT_PENDING_APPROVAL' || s === 'SCRIPT_REVISION_REQUESTED';
  }

  videoUrl(): string | null {
    return this.videoArtifactUrl();
  }

  canReviewVideo(): boolean {
    const j = this.job();
    if (!j) return false;
    return j.status === 'VIDEO_PENDING_APPROVAL' || j.status === 'VIDEO_REVISION_REQUESTED';
  }

  canApproveVideo(): boolean {
    return this.job()?.status === 'VIDEO_PENDING_APPROVAL';
  }

  canRegenerateVideo(): boolean {
    return this.job()?.status === 'VIDEO_REVISION_REQUESTED';
  }

  approveVideo(): void {
    const j = this.job();
    if (!j) return;
    // Issue #81 R3: approval names the reviewed version and its hash so a
    // stale tab cannot approve a newer, unreviewed video.
    const active = j.active_video_version;
    if (!active) return;
    const reviewed = (j.video_versions ?? []).find(v => v.version === active);
    this.runAction('video_approve', () => this.jobsApi.approveVideo(j.job_id, active, reviewed?.sha256));
  }

  requestVideoRevision(): void {
    const j = this.job();
    if (!j) return;
    const revision = window.prompt('Опишіть потрібні зміни до відео:');
    if (revision === null) return;
    const text = revision.trim();
    if (!text) return;
    this.runAction('video_revision', () => this.jobsApi.requestVideoRevision(j.job_id, text));
  }

  regenerateVideo(): void {
    const j = this.job();
    if (!j) return;
    this.confirm.confirm({
      title: 'Регенерувати відео',
      message: 'Збудувати відео повторно з поточними кадрами та налаштуваннями?',
    }).subscribe((ok) => {
      if (!ok) return;
      this.runAction('video_regenerate', () => this.jobsApi.regenerateVideo(j.job_id));
    });
  }

  canDelete(): boolean { return jobActionAllowed('delete', this.job()?.status); }
  selectedStoryboardVersion = signal<number | null>(null);

  activeStoryboard() {
    const j = this.job();
    if (!j?.storyboards) return null;
    const selected = this.selectedStoryboardVersion();
    if (selected !== null) {
      return j.storyboards.find(item => item.version === selected) || null;
    }
    return j.storyboards.find(item => item.version === j.active_storyboard_version) || null;
  }

  allStoryboardVersions() {
    const j = this.job();
    return j?.storyboards || [];
  }

  selectStoryboardVersion(version: number | null) {
    const active = this.job()?.active_storyboard_version ?? null;
    this.selectedStoryboardVersion.set(version === active ? null : version);
  }

  canRegenerateImage(): boolean {
    const j = this.job();
    const sb = this.activeStoryboard();
    return !!j && !!sb && (j.status === 'STORYBOARD_READY' || j.status === 'STORYBOARD_PENDING_APPROVAL'
      || j.status === 'STORYBOARD_FAILED' || j.status === 'VIDEO_REVISION_REQUESTED');
  }

  canRevisionImage(): boolean {
    const j = this.job();
    const sb = this.activeStoryboard();
    return !!j && !!sb && (j.status === 'STORYBOARD_READY' || j.status === 'STORYBOARD_PENDING_APPROVAL');
  }

  previewImageUrl(artifactId: string): string {
    const j = this.job();
    return j ? `/api/jobs/${encodeURIComponent(j.job_id)}/artifacts/${encodeURIComponent(artifactId)}/download` : '';
  }

  approveImageStoryboard(): void {
    const j = this.job(); const sb = this.activeStoryboard(); if (!j || !sb) return;
    this.runAction('approve', () => this.jobsApi.approveImageStoryboard(j.job_id, sb.version, sb.image_version));
  }
  regenerateImageScene(index: number): void {
    const j = this.job(); const sb = this.activeStoryboard(); if (!j || !sb) return;
    this.runAction('approve', () => this.jobsApi.revisionImageStoryboard(j.job_id, sb.version, { scene_indexes: [index], image_version: sb.image_version }));
  }
  regenerateImageStoryboardAll(): void {
    const j = this.job(); const sb = this.activeStoryboard(); if (!j || !sb) return;
    this.runAction('approve', () => this.jobsApi.regenerateImageStoryboard(j.job_id, sb.version, undefined, sb.image_version));
  }
  revisionImageStoryboard(): void {
    const rev = window.prompt('Опишіть правки до превʼю (prompt для сцени):');
    if (rev === null) return;
    const j = this.job(); const sb = this.activeStoryboard(); if (!j || !sb) return;
    this.runAction('approve', () => this.jobsApi.revisionImageStoryboard(j.job_id, sb.version, { revision: rev, image_version: sb.image_version }));
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
      video_approve: 'Схвалення відео',
      video_revision: 'Правки до відео',
      video_regenerate: 'Регенерація відео',
    };
    return labels[action] || action;
  }

  pauseJob(): void { this.runAction('pause', () => this.jobsApi.pause(this.job()!.job_id)); }
  resumeJob(): void { this.runAction('resume', () => this.jobsApi.resume(this.job()!.job_id)); }
  approveJob(): void {
    const j = this.job()!;
    if (this.canReviewScript()) {
      this.runAction('approve', () => this.jobsApi.approveScript(j.job_id));
      return;
    }
    this.runAction('approve', () => this.canReviewStoryboard()
      ? this.jobsApi.approveStoryboard(j.job_id, j.active_storyboard_version!, j.active_image_version)
      : this.jobsApi.approve(j.job_id));
  }

  requestScriptRevision(): void {
    const revision = window.prompt('Опишіть потрібні зміни до сценарію:');
    if (revision?.trim()) this.runAction('revision', () => this.jobsApi.requestScriptRevision(this.job()!.job_id, revision.trim()));
  }

  regenerateScriptAction(): void {
    const revision = window.prompt('Залиште коментар для регенерації (необовʼязково):') || undefined;
    this.runAction('regenerate', () => this.jobsApi.regenerateScript(this.job()!.job_id, revision));
  }

  rejectStoryboard(): void {
    const j = this.job();
    if (!j?.active_storyboard_version) return;
    this.confirm.confirm({ title: 'Відхилити розкадровку', message: 'Відхилити поточну версію розкадровки?' }).subscribe(ok => {
      if (ok) this.runAction('reject', () => this.jobsApi.rejectStoryboard(j.job_id, j.active_storyboard_version!));
    });
  }

  requestStoryboardRevision(): void {
    const j = this.job();
    if (!j?.active_storyboard_version) return;
    const revision = window.prompt('Опишіть потрібні зміни до розкадровки:');
    if (revision?.trim()) this.runAction('revision', () => this.jobsApi.regenerateStoryboard(j.job_id, j.active_storyboard_version!, revision.trim()));
  }

  rejectApproval(): void {
    if (this.canReviewScript()) {
      this.confirm.confirm({ title: 'Відхилити сценарій', message: 'Скасувати завдання зі сценарієм?' }).subscribe(ok => {
        if (ok) this.runAction('reject', () => this.jobsApi.cancel(this.job()!.job_id));
      });
      return;
    }
    if (this.canReviewStoryboard()) {
      this.rejectStoryboard();
      return;
    }
    this.confirm.confirm({ title: 'Відхилити завдання', message: 'Відхилити це завдання?' }).subscribe(ok => {
      if (ok) this.runAction('reject', () => this.jobsApi.cancel(this.job()!.job_id));
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
      if (ok) this.runAction('revision', () => this.jobsApi.regenerate(this.job()!.job_id));
    });
  }

  retryJob(): void {
    this.confirm.confirm({ title: 'Повторити завдання', message: 'Спробувати виконати завдання ще раз?' }).subscribe((ok) => {
      if (!ok) return;
      this.runAction('retry', () => this.jobsApi.retry(this.job()!.job_id));
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
      this.runAction('regenerate', () => this.jobsApi.regenerate(this.job()!.job_id));
    });
  }

  confirmCancel(): void {
    this.confirm.confirm({ title: 'Скасувати завдання', message: 'Ви впевнені, що хочете скасувати це завдання?' }).subscribe((ok) => {
      if (!ok) return;
      this.runAction('cancel', () => this.jobsApi.cancel(this.job()!.job_id));
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

  structuredEvents(): Array<{ key: string; message: string; timestamp?: string; type?: string; state?: string; node?: string; task_id?: string; artifact_id?: string; error?: string; attempt?: number }> {
    const j = this.job();
    if (!j) return [];
    if (Array.isArray(j.event_log) && j.event_log.length) {
      return j.event_log.map((event, idx) => ({
        key: `ev-${idx}-${event.timestamp || idx}`,
        timestamp: event.timestamp,
        message: event.message,
        type: event.type,
        state: event.state,
        attempt: event.attempt,
        node: event.node,
        task_id: event.task_id,
        artifact_id: event.artifact_id,
        error: event.error,
      }));
    }
    // Legacy events fallback removed - show only structured events
    return [];
  }

  verifyArtifact(artifactId: string): void {
    const j = this.job();
    if (!j) return;
    this.verifying.set(artifactId);
    this.jobsApi.verifyArtifacts(j.job_id).subscribe({
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
    this.jobsApi.uploadArtifact(j.job_id, folder, file.name, file).subscribe({
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
    this.jobsApi.exportJob(j.job_id).subscribe({
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
    this.jobsApi.importProject(file).subscribe({
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
    this.jobsApi.publish(j.job_id, channels).subscribe({
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
    this.jobsApi.publish(j.job_id, [channel]).subscribe({
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
