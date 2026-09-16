# Vendored: Able Pro (Bootstrap 5 admin template)

Source: `github.com/giga-brdg/Design-system`, commit `05ddaf1e70a3f832da268c7b28bc6c212dfb7f54`.

Able Pro is Phoenixcoded's commercially-licensed Bootstrap 5 admin dashboard
template. Per the design-system repo's own readme: **internal Supplax use
only — do not make this public or redistribute its contents outside the
company.**

Do not hand-edit any file under this directory.

**`css/style.css` and `css/style-preset.css` are NOT a plain copy of
`dist/assets/css/`** - they're recompiled from the design-system repo's own
Sass source with the brand palette overridden, because Bootstrap bakes
primary/success/warning/danger as literal per-component values at Sass
compile time (e.g. `.btn-primary` sets `--bs-btn-bg: #4680ff` directly - it
does not read a `--bs-primary` custom property at runtime), so a runtime CSS
override can't retint buttons/badges/alerts/etc. To reproduce or update:

1. In a checkout of `github.com/giga-brdg/Design-system`, edit
   `src/assets/scss/settings/_bootstrap-variables.scss` - set `$primary`,
   `$success`, `$warning`, `$danger` to this app's brand hex (see the OKLCH ->
   hex values below).
2. Edit `src/assets/scss/settings/_theme-variables.scss` - set the matching
   `$primary-text/-bg-subtle/-border-subtle` (and success/warning/danger
   equivalents), plus `preset-1`'s `primary` entry in `$preset-colors`, to the
   same hex values.
3. `npm install sass bootstrap` (only those two packages are actually needed
   to compile `style.scss`/`style-preset.scss` - the full `package.json` pulls
   in the whole Gulp/plugin toolchain unnecessarily for this).
4. Compile both entry points with dart-sass (`loadPaths: ['.']`) and copy the
   output here as `css/style.css` / `css/style-preset.css`.

Brand hex used (converted from this app's original OKLCH tokens in the old
`src/static/style.css`): primary/accent-ink `#4d2809`, success/green
`#488c54`, warning/amber `#c38300`, danger/red `#c04442`, plus each color's
`-bg-subtle` (light tint) and `-border-subtle` (mid tint) shades - see the
git history of the two settings files above for the exact values used.

Everything else under this directory (fonts, JS) IS a plain, unmodified copy
of `dist/assets/`. Non-color customizations for this app (surface/neutral
tokens Able Pro only sets as plain `:root` values, not baked per-component)
live in `src/static/supplax-overrides.css`, loaded after these files (mirrors
the design-system repo's own "overrides imported last" rule, ADR-0002 in
their `ARCHITECTURE.md`). To pick up template updates, redo the recompile
above and re-copy the non-CSS files below from a newer commit of the
design-system repo's `dist/assets/`.

## Files copied (curated subset — the full `dist/assets/` is ~51MB/679 files;
this app only needs the vertical-sidebar shell + core components, no charts,
no i18n, no demo icon sets)

```
css/style.css                    dist/assets/css/style.css
css/style-preset.css             dist/assets/css/style-preset.css
fonts/inter/inter.css            dist/assets/fonts/inter/inter.css
fonts/inter/Inter-roman.var.woff2  dist/assets/fonts/inter/Inter-roman.var.woff2
fonts/inter/Inter-italic.var.woff2 dist/assets/fonts/inter/Inter-italic.var.woff2
fonts/tabler-icons.min.css       dist/assets/fonts/tabler-icons.min.css
fonts/tabler/tabler-icons.*      dist/assets/fonts/tabler/tabler-icons.*  (eot/svg/ttf/woff/woff2)
js/plugins/popper.min.js         dist/assets/js/plugins/popper.min.js
js/plugins/simplebar.min.js      dist/assets/js/plugins/simplebar.min.js
js/plugins/bootstrap.min.js      dist/assets/js/plugins/bootstrap.min.js  (not bootstrap.bundle — popper loads separately, before this)
js/script.js                     dist/assets/js/script.js
js/theme.js                      dist/assets/js/theme.js
```

## Deliberately excluded

- `js/icon/custom-font.js` — a `document.write()`-based SVG icon-sprite
  injector that Able Pro's own stock sidebar markup depends on. We render
  every icon (including sidebar nav icons) with plain Tabler font classes
  (`<i class="ti ti-...">`) instead, so this file and the sprite it injects
  are never referenced.
- `apexcharts.min.js` + all `js/widgets/*-graph.js` — no charts in this app.
- `i18next*.js`, `js/multi-lang.js` — this app is Ukrainian-only, no runtime
  i18n.
- `js/tech-stack.js` — demo-page-only.
- Phosphor/Feather/FontAwesome/Material icon fonts — only Tabler is used.
- All demo/marketing images.
- Every vendor analytics/tracking snippet present in the template's own demo
  pages (Google Tag Manager, Microsoft Clarity, a Cloudways pixel, a "fomo"
  pixel) — never carried into this app regardless of anything else.

## Known integration shim

`js/script.js` calls `feather.replace()` unconditionally inside its
`DOMContentLoaded` handler. Since Feather icons aren't vendored (Tabler covers
every icon this app uses), `base.html` defines a no-op
`window.feather = { replace: function () {} }` shim *before* loading
`script.js`, so that call doesn't throw and abort the rest of the handler
(which also wires up sidebar collapse/toggle behavior).
