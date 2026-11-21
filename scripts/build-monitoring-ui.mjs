#!/usr/bin/env node
// Builds modules/monitoring/ui/index.html from ui-src without any CDN: the module UI runs in a
// sandboxed iframe whose CSP allows only inline scripts and styles (core/module/gateway.py UI_CSP),
// so Tailwind utilities are compiled into an inline <style> and the vendored Chart.js is inlined.
// The signed manifest digest of index.html is updated in the same run.
//
//   node scripts/build-monitoring-ui.mjs          (uses web-v2/node_modules for Tailwind)
//   node scripts/build-monitoring-ui.mjs --check  (fails when ui/ or the digest is stale)
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const moduleDir = join(root, 'modules', 'monitoring');
const webV2 = join(root, 'web-v2');
const require = createRequire(join(webV2, 'package.json'));
const { compile, optimize } = require('@tailwindcss/node');
const { Scanner } = require('@tailwindcss/oxide');

const template = readFileSync(join(moduleDir, 'ui-src', 'index.html'), 'utf8');
const chart = readFileSync(join(moduleDir, 'ui-src', 'vendor', 'chart.umd.min.js'), 'utf8');
if (chart.includes('</script')) throw new Error('Chart.js cannot be inlined: it contains </script');

// Tailwind 4 with the Tailwind 3 default border colour the page was written for (cdn.tailwindcss.com).
const input = `@import "tailwindcss";
@layer base { *, ::after, ::before, ::backdrop { border-color: var(--color-gray-200, currentColor); } }`;
const compiler = await compile(input, { base: webV2, onDependency: () => {} });
const scanner = new Scanner({ sources: [{ base: join(moduleDir, 'ui-src'), pattern: 'index.html', negated: false }] });
const css = optimize(compiler.build(scanner.scan()), { minify: true }).code;

const marker = (name) => `<!-- vertep:${name} -->`;
for (const name of ['styles', 'chartjs']) {
  if (template.split(marker(name)).length !== 2) throw new Error(`ui-src/index.html needs exactly one ${marker(name)}`);
}
const html = template
  .replace(marker('styles'), () => `<style>${css}</style>`)
  .replace(marker('chartjs'), () => `<script>${chart}</script>`);
const digest = createHash('sha256').update(html).digest('hex');
const manifestPath = join(moduleDir, 'manifest.yaml');
const manifest = readFileSync(manifestPath, 'utf8');
const updated = manifest.replace(/(\n {4}index\.html: ")[0-9a-f]{64}(")/, `$1${digest}$2`);
if (updated === manifest && !manifest.includes(digest)) throw new Error('manifest.yaml has no ui.files index.html digest');
const outputPath = join(moduleDir, 'ui', 'index.html');
if (process.argv.includes('--check')) {
  if (readFileSync(outputPath, 'utf8') !== html || updated !== manifest) {
    console.error('modules/monitoring/ui is stale: run node scripts/build-monitoring-ui.mjs');
    process.exit(1);
  }
} else {
  writeFileSync(outputPath, html);
  writeFileSync(manifestPath, updated);
}
console.log(JSON.stringify({ index_html: digest, bytes: Buffer.byteLength(html), css_bytes: css.length }));
