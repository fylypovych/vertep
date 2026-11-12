import { test, expect } from '@playwright/test';

// The TTS runtime contract: voices are objects; the id is what synthesis needs.
const VOICES = { voices: [{ id: 'en-us', name: 'English (America)' }, { id: 'uk', name: 'Ukrainian' }],
  default_voice: 'default', speed_range: { min: 80, max: 450 }, speed_unit: 'words_per_minute' };
// The smallest valid WAV header with one silent sample.
const WAV = Buffer.from('UklGRiYAAABXQVZFZm10IBAAAAABAAEAIlYAAESsAAACABAAZGF0YQIAAAAAAA==', 'base64');

interface State { voicesFail: number; previews: any[]; preview: (call: number) => { status: number; json?: any } }

function mockApi(page: any, state: State) {
  return page.route('**/api/**', async (route: any) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/session')) return route.fulfill({ json: { authenticated: true, username: 'admin', user: 'admin', role: 'admin' } });
    if (path.endsWith('/status')) return route.fulfill({ json: { system: { state: 'NORMAL', reason: null }, version: '0' } });
    if (path.endsWith('/models/voices')) {
      if (state.voicesFail > 0) { state.voicesFail--; return route.fulfill({ status: 502, json: { detail: 'TTS runtime is unreachable', code: 'tts_runtime_is_unreachable' } }); }
      return route.fulfill({ json: VOICES });
    }
    if (path.endsWith('/models/voices/preview')) {
      state.previews.push(JSON.parse(request.postData() || '{}'));
      const answer = state.preview(state.previews.length);
      if (answer.status !== 200) return route.fulfill({ status: answer.status, json: answer.json });
      return route.fulfill({ status: 200, contentType: 'audio/wav', body: WAV });
    }
    if (path.endsWith('/system/models')) return route.fulfill({ json: { models: [], nodes: [] } });
    if (path.endsWith('/system/models/catalog')) return route.fulfill({ json: { source: '', verified_at: '', models: [] } });
    return route.fulfill({ json: request.method() === 'GET' ? [] : {} });
  });
}

test('голоси показані назвами, у запит іде id; швидкість має назву й межі; успішне прослуховування', async ({ page }) => {
  const state: State = { voicesFail: 0, previews: [], preview: () => ({ status: 200 }) };
  await mockApi(page, state);
  await page.goto('/settings?tab=models');
  const select = page.getByTestId('voice-preview-voice');
  await expect(select.locator('option')).toHaveCount(3, { timeout: 20000 });
  await expect(select).not.toContainText('[object Object]');
  await expect(select.locator('option').nth(0)).toHaveText('За замовчуванням (голос рушія)');
  await expect(select.locator('option').nth(2)).toHaveText('Ukrainian (uk)');
  await expect(page.getByTestId('voice-preview')).toContainText('Швидкість, слів за хвилину');
  await expect(page.getByTestId('voice-speed-hint')).toContainText('80–450');
  await expect(page.getByTestId('voice-preview-speed')).toHaveAttribute('max', '450');
  await select.selectOption('uk');
  await page.getByTestId('voice-preview-button').click();
  await expect(page.getByTestId('voice-preview-audio')).toBeVisible();
  expect(state.previews).toEqual([{ text: 'Перевірка голосу Vertep.', voice: 'uk', speed: 150 }]);
});

test('причина відмови з тіла помилки, попереднє аудіо зникає, виправлення без reload; швидкість поза межами не надсилається', async ({ page }) => {
  const state: State = { voicesFail: 0, previews: [], preview: (call) => call === 2
    ? { status: 422, json: { detail: 'Unknown voice', code: 'unknown_voice' } } : { status: 200 } };
  await mockApi(page, state);
  await page.goto('/settings?tab=models');
  await expect(page.getByTestId('voice-preview-voice').locator('option')).toHaveCount(3, { timeout: 20000 });
  await page.getByTestId('voice-preview-button').click();
  await expect(page.getByTestId('voice-preview-audio')).toBeVisible();
  await page.getByTestId('voice-preview-voice').selectOption('en-us');
  await page.getByTestId('voice-preview-button').click();
  await expect(page.getByTestId('voice-preview-error')).toHaveText('Цей голос не підтримується голосовим сервісом');
  await expect(page.getByTestId('voice-preview-audio')).toHaveCount(0);
  await page.getByTestId('voice-preview-speed').fill('500');
  await page.getByTestId('voice-preview-button').click();
  await expect(page.getByTestId('voice-preview-error')).toContainText('допустимих межах');
  expect(state.previews.length).toBe(2);
  await page.getByTestId('voice-preview-speed').fill('200');
  await page.getByTestId('voice-preview-voice').selectOption('uk');
  await page.getByTestId('voice-preview-button').click();
  await expect(page.getByTestId('voice-preview-audio')).toBeVisible();
  await expect(page.getByTestId('voice-preview-error')).toHaveCount(0);
  expect(state.previews[2]).toEqual({ text: 'Перевірка голосу Vertep.', voice: 'uk', speed: 200 });
});

test('недоступний каталог голосів показано явно й повторено; англійська локаль', async ({ page }) => {
  const state: State = { voicesFail: Infinity, previews: [], preview: () => ({ status: 502, json: { detail: 'Voice preview failed', code: 'voice_preview_failed' } }) };
  await mockApi(page, state);
  await page.addInitScript(() => localStorage.setItem('vertep_language', 'en'));
  await page.goto('/settings?tab=models');
  await expect(page.getByTestId('voice-catalog-error')).toContainText('The voice service (TTS) is unreachable', { timeout: 20000 });
  state.voicesFail = 0;
  await page.getByTestId('voice-catalog-retry').click();
  await expect(page.getByTestId('voice-catalog-error')).toHaveCount(0);
  await expect(page.getByTestId('voice-preview-voice').locator('option').nth(2)).toHaveText('Ukrainian (uk)');
  await expect(page.getByTestId('voice-preview')).toContainText('Speed, words per minute');
  await page.getByTestId('voice-preview-button').click();
  await expect(page.getByTestId('voice-preview-error')).toHaveText('The voice service could not synthesize audio');
});
