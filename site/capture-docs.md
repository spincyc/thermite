# Documentation screenshots

The images in `assets/screenshots/` show the real web and terminal clients in
mock mode, using `oh_public.demo()`'s fictional inventory. They show no real
deployment. No device, native runtime or access service is contacted.

From the source checkout, install the capture-only dependencies in scratch:

```sh
npm install --ignore-scripts --no-audit --no-fund --prefix .scratch/docs-visuals/npm playwright@1.58.2 @xterm/xterm@6.0.0 blessed@0.1.81
PLAYWRIGHT_BROWSERS_PATH=.scratch/docs-visuals/browsers .scratch/docs-visuals/npm/node_modules/.bin/playwright install chromium
PLAYWRIGHT_BROWSERS_PATH=.scratch/docs-visuals/browsers python3 site/capture-docs.py --modules .scratch/docs-visuals/npm/node_modules
```

The script generates remote assets from the fictional public inventory under
`.scratch/docs-visuals/capture/`, launches the actual terminal program in a
154-column, 44-row PTY, and captures its ANSI output. Chromium renders that
output in xterm.js and opens the actual web client at 1440 × 1000 pixels.
Browser requests are restricted to the script's loopback file server.
The terminal exits and the file server stops after capture.

Chromium needs its platform libraries and installed sans-serif and monospace
fonts. You can use an existing compatible Chromium executable by setting
`THERMITE_TEST_CHROME` to its absolute path. The capture explicitly enables
terminal color, even if the calling shell sets `NO_COLOR` or `FORCE_COLOR`.
On a minimal host, pass `--fonts /path/to/DejaVu` to use a directory containing
DejaVu Sans, Serif and Sans Mono. The script writes a scratch fontconfig file
with the required generic-family aliases and default fallback. It changes no
system font settings.

Review both PNGs before publishing. `manifest.json` records their dimensions,
SHA-256 digests and the inventory digest for the public export guard. The
terminal's clock uses the capture time; fonts and browser versions can also
affect pixels. The manifest and screenshots belong in the same change.
