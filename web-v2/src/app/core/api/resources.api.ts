import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { catchError, map } from 'rxjs/operators';
import { BaseApiService } from './base-api.service';
import { Character, Brand, Channel, Workflow, BrandCreateDTO, BrandUpdateDTO, ChannelCreateDTO, ChannelUpdateDTO, WorkflowSaveResponse, WorkflowDeleteResponse } from '../models';

export interface CharacterReference { filename: string; sha256: string; size: number; }

export interface ModuleCharacter {
  id: string;
  name: string;
  language: string;
  enabled: boolean;
  workflow?: string;
  owner_brand_id: string;
  active: boolean;
  created_at?: string;
  updated_at?: string;
  system_prompt: string;
  voice: Record<string, unknown>;
  visual: Record<string, unknown>;
  generation: Record<string, unknown>;
  publishing: Record<string, unknown>;
  references: CharacterReference[];
  collaborations: string[];
  is_owner: boolean;
}

export interface ModuleBrand {
  id: string;
  name: string;
  description: string;
  logo_url?: string;
  website?: string;
  social_links: Record<string, string>;
  owner_user_id: string;
  active: boolean;
  created_at: string;
  updated_at: string;
}

export interface ModuleBrandCollaboration {
  id: number;
  brand_id: string;
  collaborator_user_id: string;
  permission_level: string;
  active: boolean;
  created_at: string;
  updated_at: string;
}

export interface MigrationResult {
  migrated: string[];
  skipped: Array<{ id: string; reason: string }>;
  errors: Array<{ id: string; error: string }>;
}

export interface BrandsMigrationResult {
  migrated: number;
  skipped: number;
}

export interface ModuleScript {
  id: string;
  brand_id: string;
  title: string;
  description: string;
  status: string;
  script_data: Record<string, unknown>;
  owner_user_id: string;
  active: boolean;
  created_at: string;
  updated_at: string;
}

export interface ModuleScene {
  id: string;
  script_id: string;
  scene_number: number;
  title: string;
  description: string;
  scene_data: Record<string, unknown>;
  status: string;
  generated_assets: any[];
  created_at: string;
  updated_at: string;
}

export interface ModuleStoryboard {
  id: string;
  script_id: string;
  name: string;
  panels: any[];
  owner_user_id: string;
  active: boolean;
  created_at: string;
  updated_at: string;
}

export interface ModuleStoryboardCollaboration {
  id: number;
  entity_type: string;
  entity_id: string;
  collaborator_user_id: string;
  permission_level: string;
  active: boolean;
  created_at: string;
  updated_at: string;
}

export interface StoryboardMigrationResult {
  migrated: number;
  skipped: number;
}

@Injectable({ providedIn: 'root' })
export class ResourcesApiService {
  private h = () => this.base.headers();
  constructor(private http: HttpClient, private base: BaseApiService) {}
  // Legacy character endpoints (filesystem-based)
  characters = (): Observable<Character[]> => this.http.get<Character[]>(`${this.base.url}/characters`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  characterReferences = (id: string): Observable<CharacterReference[]> => this.http.get<CharacterReference[]>(`${this.base.url}/characters/${encodeURIComponent(id)}/references`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  uploadCharacterReference = (id: string, file: File): Observable<CharacterReference> => this.http.post<CharacterReference>(`${this.base.url}/characters/${encodeURIComponent(id)}/references`, file).pipe(catchError(this.base.handleError));
  deleteCharacterReference = (id: string, name: string): Observable<unknown> => this.http.delete(`${this.base.url}/characters/${encodeURIComponent(id)}/references/${encodeURIComponent(name)}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  characterReferenceUrl = (id: string, name: string): string => `${this.base.url}/characters/${encodeURIComponent(id)}/references/${encodeURIComponent(name)}`;
  character = (id: string): Observable<Character> => this.http.get<Character>(`${this.base.url}/characters/${this.base.enc(id)}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  createCharacter = (p: Character): Observable<Character> => this.http.post<Character>(`${this.base.url}/characters`, p, { headers: this.h() }).pipe(catchError(this.base.handleError));
  updateCharacter = (id: string, p: Character): Observable<Character> => this.http.put<Character>(`${this.base.url}/characters/${this.base.enc(id)}`, p, { headers: this.h() }).pipe(catchError(this.base.handleError));
  deleteCharacter = (id: string): Observable<{ deleted: string }> => this.http.delete<{ deleted: string }>(`${this.base.url}/characters/${this.base.enc(id)}`, { headers: this.h() }).pipe(catchError(this.base.handleError));

  // Characters Module API (Application Module)
  moduleCharacters = (): Observable<ModuleCharacter[]> => this.http.get<ModuleCharacter[]>(`${this.base.url}/modules/characters/v1/characters`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleCharacter = (id: string): Observable<ModuleCharacter> => this.http.get<ModuleCharacter>(`${this.base.url}/modules/characters/v1/characters/${this.base.enc(id)}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleCreateCharacter = (p: ModuleCharacter): Observable<ModuleCharacter> => this.http.post<ModuleCharacter>(`${this.base.url}/modules/characters/v1/characters`, p, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleUpdateCharacter = (id: string, p: ModuleCharacter): Observable<ModuleCharacter> => this.http.put<ModuleCharacter>(`${this.base.url}/modules/characters/v1/characters/${this.base.enc(id)}`, p, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleDeleteCharacter = (id: string): Observable<{ deleted: string }> => this.http.delete<{ deleted: string }>(`${this.base.url}/modules/characters/v1/characters/${this.base.enc(id)}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleActivateCharacter = (id: string): Observable<ModuleCharacter> => this.http.post<ModuleCharacter>(`${this.base.url}/modules/characters/v1/characters/${this.base.enc(id)}/activate`, {}, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleDeactivateCharacter = (id: string): Observable<ModuleCharacter> => this.http.post<ModuleCharacter>(`${this.base.url}/modules/characters/v1/characters/${this.base.enc(id)}/deactivate`, {}, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleCharacterReferences = (id: string): Observable<CharacterReference[]> => this.http.get<CharacterReference[]>(`${this.base.url}/modules/characters/v1/characters/${encodeURIComponent(id)}/references`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleUploadCharacterReference = (id: string, file: File): Observable<CharacterReference> => this.http.post<CharacterReference>(`${this.base.url}/modules/characters/v1/characters/${encodeURIComponent(id)}/references`, file).pipe(catchError(this.base.handleError));
  moduleDeleteCharacterReference = (id: string, name: string): Observable<unknown> => this.http.delete(`${this.base.url}/modules/characters/v1/characters/${encodeURIComponent(id)}/references/${encodeURIComponent(name)}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleCharacterReferenceUrl = (id: string, name: string): string => `${this.base.url}/modules/characters/v1/characters/${encodeURIComponent(id)}/references/${encodeURIComponent(name)}`;
  moduleAddCollaboration = (id: string, brandId: string): Observable<ModuleCharacter> => this.http.post<ModuleCharacter>(`${this.base.url}/modules/characters/v1/characters/${this.base.enc(id)}/collaborations`, { brand_id: brandId }, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleRemoveCollaboration = (id: string, brandId: string): Observable<ModuleCharacter> => this.http.delete(`${this.base.url}/modules/characters/v1/characters/${this.base.enc(id)}/collaborations/${this.base.enc(brandId)}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleMigrateCharacters = (defaultOwnerBrand = 'vertep'): Observable<MigrationResult> => this.http.post<MigrationResult>(`${this.base.url}/modules/characters/v1/migrate`, { default_owner_brand: defaultOwnerBrand }, { headers: this.h() }).pipe(catchError(this.base.handleError));

  // Brands Module API (Application Module)
  moduleBrands = (): Observable<ModuleBrand[]> => this.http.get<ModuleBrand[]>(`${this.base.url}/modules/brands/v1/brands`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleBrand = (id: string): Observable<ModuleBrand> => this.http.get<ModuleBrand>(`${this.base.url}/modules/brands/v1/brands/${this.base.enc(id)}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleCreateBrand = (p: ModuleBrand): Observable<ModuleBrand> => this.http.post<ModuleBrand>(`${this.base.url}/modules/brands/v1/brands`, p, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleUpdateBrand = (id: string, p: ModuleBrand): Observable<ModuleBrand> => this.http.put<ModuleBrand>(`${this.base.url}/modules/brands/v1/brands/${this.base.enc(id)}`, p, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleDeleteBrand = (id: string): Observable<{ deleted: string }> => this.http.delete<{ deleted: string }>(`${this.base.url}/modules/brands/v1/brands/${this.base.enc(id)}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleActivateBrand = (id: string): Observable<ModuleBrand> => this.http.post<ModuleBrand>(`${this.base.url}/modules/brands/v1/brands/${this.base.enc(id)}/activate`, {}, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleDeactivateBrand = (id: string): Observable<ModuleBrand> => this.http.post<ModuleBrand>(`${this.base.url}/modules/brands/v1/brands/${this.base.enc(id)}/deactivate`, {}, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleBrandCollaborations = (brandId: string): Observable<ModuleBrandCollaboration[]> => this.http.get<ModuleBrandCollaboration[]>(`${this.base.url}/modules/brands/v1/brands/${encodeURIComponent(brandId)}/collaborations`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleAddBrandCollaboration = (brandId: string, collaboratorUserId: string, permissionLevel: string): Observable<ModuleBrandCollaboration> => this.http.post<ModuleBrandCollaboration>(`${this.base.url}/modules/brands/v1/brands/${this.base.enc(brandId)}/collaborations`, { collaborator_user_id: collaboratorUserId, permission_level: permissionLevel }, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleRemoveBrandCollaboration = (brandId: string, collabId: number): Observable<{ deleted: boolean }> => this.http.delete(`${this.base.url}/modules/brands/v1/brands/${this.base.enc(brandId)}/collaborations/${collabId}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleMigrateBrands = (brands: ModuleBrand[]): Observable<BrandsMigrationResult> => this.http.post<BrandsMigrationResult>(`${this.base.url}/modules/brands/v1/migrate`, { brands }, { headers: this.h() }).pipe(catchError(this.base.handleError));

  // Storyboard Module API (Application Module)
  moduleScripts = (): Observable<ModuleScript[]> => this.http.get<ModuleScript[]>(`${this.base.url}/modules/storyboard/v1/scripts`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleScript = (id: string): Observable<ModuleScript> => this.http.get<ModuleScript>(`${this.base.url}/modules/storyboard/v1/scripts/${this.base.enc(id)}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleCreateScript = (p: ModuleScript): Observable<ModuleScript> => this.http.post<ModuleScript>(`${this.base.url}/modules/storyboard/v1/scripts`, p, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleUpdateScript = (id: string, p: ModuleScript): Observable<ModuleScript> => this.http.put<ModuleScript>(`${this.base.url}/modules/storyboard/v1/scripts/${this.base.enc(id)}`, p, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleDeleteScript = (id: string): Observable<{ deleted: string }> => this.http.delete<{ deleted: string }>(`${this.base.url}/modules/storyboard/v1/scripts/${this.base.enc(id)}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleActivateScript = (id: string): Observable<ModuleScript> => this.http.post<ModuleScript>(`${this.base.url}/modules/storyboard/v1/scripts/${this.base.enc(id)}/activate`, {}, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleDeactivateScript = (id: string): Observable<ModuleScript> => this.http.post<ModuleScript>(`${this.base.url}/modules/storyboard/v1/scripts/${this.base.enc(id)}/deactivate`, {}, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleScriptScenes = (scriptId: string): Observable<ModuleScene[]> => this.http.get<ModuleScene[]>(`${this.base.url}/modules/storyboard/v1/scripts/${encodeURIComponent(scriptId)}/scenes`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleScriptCollaborations = (scriptId: string): Observable<ModuleStoryboardCollaboration[]> => this.http.get<ModuleStoryboardCollaboration[]>(`${this.base.url}/modules/storyboard/v1/scripts/${encodeURIComponent(scriptId)}/collaborations`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleAddScriptCollaboration = (scriptId: string, collaboratorUserId: string, permissionLevel: string): Observable<ModuleStoryboardCollaboration> => this.http.post<ModuleStoryboardCollaboration>(`${this.base.url}/modules/storyboard/v1/scripts/${this.base.enc(scriptId)}/collaborations`, { collaborator_user_id: collaboratorUserId, permission_level: permissionLevel }, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleRemoveScriptCollaboration = (scriptId: string, collabId: number): Observable<{ deleted: boolean }> => this.http.delete(`${this.base.url}/modules/storyboard/v1/scripts/${this.base.enc(scriptId)}/collaborations/${collabId}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleScenes = (): Observable<ModuleScene[]> => this.http.get<ModuleScene[]>(`${this.base.url}/modules/storyboard/v1/scenes`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleScene = (id: string): Observable<ModuleScene> => this.http.get<ModuleScene>(`${this.base.url}/modules/storyboard/v1/scenes/${this.base.enc(id)}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleCreateScene = (p: ModuleScene): Observable<ModuleScene> => this.http.post<ModuleScene>(`${this.base.url}/modules/storyboard/v1/scenes`, p, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleUpdateScene = (id: string, p: ModuleScene): Observable<ModuleScene> => this.http.put<ModuleScene>(`${this.base.url}/modules/storyboard/v1/scenes/${this.base.enc(id)}`, p, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleDeleteScene = (id: string): Observable<{ deleted: string }> => this.http.delete<{ deleted: string }>(`${this.base.url}/modules/storyboard/v1/scenes/${this.base.enc(id)}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleSceneCollaborations = (sceneId: string): Observable<ModuleStoryboardCollaboration[]> => this.http.get<ModuleStoryboardCollaboration[]>(`${this.base.url}/modules/storyboard/v1/scenes/${encodeURIComponent(sceneId)}/collaborations`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleAddSceneCollaboration = (sceneId: string, collaboratorUserId: string, permissionLevel: string): Observable<ModuleStoryboardCollaboration> => this.http.post<ModuleStoryboardCollaboration>(`${this.base.url}/modules/storyboard/v1/scenes/${this.base.enc(sceneId)}/collaborations`, { collaborator_user_id: collaboratorUserId, permission_level: permissionLevel }, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleRemoveSceneCollaboration = (sceneId: string, collabId: number): Observable<{ deleted: boolean }> => this.http.delete(`${this.base.url}/modules/storyboard/v1/scenes/${this.base.enc(sceneId)}/collaborations/${collabId}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleStoryboards = (): Observable<ModuleStoryboard[]> => this.http.get<ModuleStoryboard[]>(`${this.base.url}/modules/storyboard/v1/storyboards`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleStoryboard = (id: string): Observable<ModuleStoryboard> => this.http.get<ModuleStoryboard>(`${this.base.url}/modules/storyboard/v1/storyboards/${this.base.enc(id)}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleCreateStoryboard = (p: ModuleStoryboard): Observable<ModuleStoryboard> => this.http.post<ModuleStoryboard>(`${this.base.url}/modules/storyboard/v1/storyboards`, p, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleUpdateStoryboard = (id: string, p: ModuleStoryboard): Observable<ModuleStoryboard> => this.http.put<ModuleStoryboard>(`${this.base.url}/modules/storyboard/v1/storyboards/${this.base.enc(id)}`, p, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleDeleteStoryboard = (id: string): Observable<{ deleted: string }> => this.http.delete<{ deleted: string }>(`${this.base.url}/modules/storyboard/v1/storyboards/${this.base.enc(id)}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleStoryboardCollaborations = (storyboardId: string): Observable<ModuleStoryboardCollaboration[]> => this.http.get<ModuleStoryboardCollaboration[]>(`${this.base.url}/modules/storyboard/v1/storyboards/${encodeURIComponent(storyboardId)}/collaborations`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleAddStoryboardCollaboration = (storyboardId: string, collaboratorUserId: string, permissionLevel: string): Observable<ModuleStoryboardCollaboration> => this.http.post<ModuleStoryboardCollaboration>(`${this.base.url}/modules/storyboard/v1/storyboards/${this.base.enc(storyboardId)}/collaborations`, { collaborator_user_id: collaboratorUserId, permission_level: permissionLevel }, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleRemoveStoryboardCollaboration = (storyboardId: string, collabId: number): Observable<{ deleted: boolean }> => this.http.delete(`${this.base.url}/modules/storyboard/v1/storyboards/${this.base.enc(storyboardId)}/collaborations/${collabId}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  moduleMigrateScripts = (scripts: ModuleScript[]): Observable<StoryboardMigrationResult> => this.http.post<StoryboardMigrationResult>(`${this.base.url}/modules/storyboard/v1/scripts/migrate`, { scripts }, { headers: this.h() }).pipe(catchError(this.base.handleError));

  brands = (): Observable<Brand[]> => this.http.get<Brand[]>(`${this.base.url}/brands`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  createBrand = (p: BrandCreateDTO): Observable<Brand> => this.http.post<Brand>(`${this.base.url}/brands`, p, { headers: this.h() }).pipe(catchError(this.base.handleError));
  updateBrand = (id: string, p: BrandUpdateDTO): Observable<Brand> => this.http.put<Brand>(`${this.base.url}/brands/${this.base.enc(id)}`, p, { headers: this.h() }).pipe(catchError(this.base.handleError));
  deleteBrand = (id: string): Observable<{ deleted: string }> => this.http.delete<{ deleted: string }>(`${this.base.url}/brands/${this.base.enc(id)}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  channels = (brandId: string): Observable<Channel[]> => this.http.get<Channel[]>(`${this.base.url}/brands/${this.base.enc(brandId)}/channels`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  channelTypes = (): Observable<string[]> => this.http.get<string[]>(`${this.base.url}/channels/types`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  createChannel = (brandId: string, p: ChannelCreateDTO): Observable<Channel> => this.http.post<Channel>(`${this.base.url}/brands/${this.base.enc(brandId)}/channels`, p, { headers: this.h() }).pipe(catchError(this.base.handleError));
  updateChannel = (channelId: string, p: ChannelUpdateDTO): Observable<Channel> => this.http.put<Channel>(`${this.base.url}/channels/${this.base.enc(channelId)}`, p, { headers: this.h() }).pipe(catchError(this.base.handleError));
  deleteChannel = (channelId: string): Observable<{ deleted: string }> => this.http.delete<{ deleted: string }>(`${this.base.url}/channels/${this.base.enc(channelId)}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  workflows = (): Observable<Workflow[]> => this.http.get<Workflow[]>(`${this.base.url}/workflows`, { headers: this.h() }).pipe(
    catchError(this.base.handleError),
    map((items) => (items || []).map((item): Workflow => ({ ...item, kind: item.kind || item.type || '' })))
  );
  validateWorkflow = (p: unknown): Observable<import('../models').WorkflowValidation> => this.http.post<import('../models').WorkflowValidation>(`${this.base.url}/workflows/validate`, p, { headers: this.h() }).pipe(catchError(this.base.handleError));
  workflow = (kind: string, name: string): Observable<import('../models').WorkflowDocument> => this.http.get<import('../models').WorkflowDocument>(`${this.base.url}/workflows/${this.base.enc(kind)}/${this.base.enc(name)}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  saveWorkflow = (kind: string, name: string, p: Record<string, unknown>, force = false): Observable<WorkflowSaveResponse> => this.http.put<WorkflowSaveResponse>(`${this.base.url}/workflows/${this.base.enc(kind)}/${this.base.enc(name)}${force ? '?force=true' : ''}`, p, { headers: this.h() }).pipe(catchError(this.base.handleError));
  deleteWorkflow = (kind: string, name: string, force = false): Observable<WorkflowDeleteResponse> => this.http.delete<WorkflowDeleteResponse>(`${this.base.url}/workflows/${this.base.enc(kind)}/${this.base.enc(name)}${force ? '?force=true' : ''}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  workflowVersions = (kind: string, name: string): Observable<import('../models').WorkflowVersion[]> => this.http.get<import('../models').WorkflowVersion[]>(`${this.base.url}/workflows/${this.base.enc(kind)}/${this.base.enc(name)}/versions`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  workflowVersion = (kind: string, name: string, version: number): Observable<import('../models').WorkflowDocument> => this.http.get<import('../models').WorkflowDocument>(`${this.base.url}/workflows/${this.base.enc(kind)}/${this.base.enc(name)}/versions/${version}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  restoreWorkflowVersion = (kind: string, name: string, version: number): Observable<import('../models').WorkflowSaveResponse> => this.http.post<import('../models').WorkflowSaveResponse>(`${this.base.url}/workflows/${this.base.enc(kind)}/${this.base.enc(name)}/versions/${version}/restore`, {}, { headers: this.h() }).pipe(catchError(this.base.handleError));
  workflowForm = (kind: string, name: string): Observable<import('../models').WorkflowFormSchema> => this.http.get<import('../models').WorkflowFormSchema>(`${this.base.url}/workflows/${this.base.enc(kind)}/${this.base.enc(name)}/form`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  workflowUsage = (kind: string, name: string): Observable<import('../models').WorkflowUsage> => this.http.get<import('../models').WorkflowUsage>(`${this.base.url}/workflows/${this.base.enc(kind)}/${this.base.enc(name)}/usage`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  logo = (): Observable<Blob> => this.http.get(`${this.base.url}/settings/logo`, { responseType: 'blob' }).pipe(catchError(this.base.handleError));
  uploadLogo = (file: File): Observable<{ saved: boolean }> => { const f = new FormData(); f.append('file', file); return this.http.post<{ saved: boolean }>(`${this.base.url}/settings/logo`, f, { headers: this.h() }).pipe(catchError(this.base.handleError)); };
  deleteLogo = (): Observable<{ deleted: boolean }> => this.http.delete<{ deleted: boolean }>(`${this.base.url}/settings/logo`, { headers: this.h() }).pipe(catchError(this.base.handleError));
}
