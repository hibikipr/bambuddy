#!/usr/bin/env node
// Copies the Tesseract.js worker/core/language-data files this app needs to
// run OCR fully offline (see public/tesseract/README.md for why) from
// node_modules into public/tesseract/, where Vite serves them as static
// assets at the site root.
//
// Regenerated from node_modules on every `npm install` (wired into
// "postinstall") rather than committed to git: the WASM core and traineddata
// files are multi-megabyte binaries, well over this repo's 1 MB
// check-added-large-files pre-commit limit. package-lock.json already pins
// tesseract.js/tesseract.js-core/@tesseract.js-data to exact versions, so
// this is fully reproducible without needing the bytes themselves in git
// history.

import { copyFileSync, existsSync, mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const frontendRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const nodeModules = join(frontendRoot, 'node_modules');
const destDir = join(frontendRoot, 'public', 'tesseract');

const files = [
  [join(nodeModules, 'tesseract.js', 'dist', 'worker.min.js'), 'worker.min.js'],
  [join(nodeModules, 'tesseract.js-core', 'tesseract-core-lstm.wasm.js'), 'tesseract-core-lstm.wasm.js'],
  [join(nodeModules, 'tesseract.js-core', 'tesseract-core-lstm.wasm'), 'tesseract-core-lstm.wasm'],
  [join(nodeModules, '@tesseract.js-data', 'eng', '4.0.0_best_int', 'eng.traineddata.gz'), 'eng.traineddata.gz'],
];

mkdirSync(destDir, { recursive: true });

let missing = 0;
for (const [src, name] of files) {
  if (!existsSync(src)) {
    console.error(`[copy-tesseract-assets] missing source file: ${src}`);
    missing += 1;
    continue;
  }
  copyFileSync(src, join(destDir, name));
}

if (missing > 0) {
  console.error(
    '[copy-tesseract-assets] one or more source files were missing - ' +
      'is npm install still in progress, or did tesseract.js/tesseract.js-core/@tesseract.js-data get removed?',
  );
  process.exit(1);
}

console.log(`[copy-tesseract-assets] copied ${files.length} file(s) into public/tesseract/`);
