# Locally-bundled Tesseract.js assets

Bambuddy is frequently self-hosted air-gapped/offline. Tesseract.js defaults
to fetching its worker script, WASM core, and language data from the
`cdn.jsdelivr.net` CDN at runtime — these copies exist so the Photo tab's
OCR (`BarcodeScannerModal.tsx`) works fully offline and never makes an
un-opted external request.

| File | Source | Why this variant |
| --- | --- | --- |
| `worker.min.js` | `tesseract.js/dist/worker.min.js` | The Web Worker script itself. |
| `tesseract-core-lstm.wasm(.js)` | `tesseract.js-core` | LSTM-only build — the app never uses the legacy OCR engine (OEM default is `LSTM_ONLY`), so the plain (non-SIMD) LSTM-only core is the smallest correct choice. `tesseract.js-core` also ships SIMD/relaxed-SIMD variants for a speed bump on supporting browsers, intentionally not bundled here to keep this asset set small; swap in `tesseract-core-simd-lstm.wasm(.js)` if that trade-off is ever revisited. |
| `eng.traineddata.gz` | `@tesseract.js-data/eng`, the `4.0.0_best_int` variant | Quantized ("best_int") English model — matches what Tesseract.js's own CDN fallback picks for `lstmOnly` mode. ~3 MB vs. ~11 MB for the full-precision `4.0.0` model, no meaningful accuracy loss for this app's use (short printed label text, not handwriting). |

## Regenerated, not committed

These files aren't tracked in git — they're multi-megabyte binaries, well
over this repo's 1 MB `check-added-large-files` pre-commit limit.
`scripts/copy-tesseract-assets.mjs` copies them from `node_modules` into
this directory on every `npm install` (wired into `"postinstall"`), so they
always exist before `npm run dev`/`build` needs them. `package-lock.json`
already pins `tesseract.js`/`tesseract.js-core`/`@tesseract.js-data` to
exact versions, so this is fully reproducible without the bytes themselves
being in git history — bumping any of those three in `package.json` and
running `npm install` regenerates the matching assets automatically.

To refresh them manually without a full `npm install` (e.g. after editing
`copy-tesseract-assets.mjs` itself):

```sh
node scripts/copy-tesseract-assets.mjs
```
