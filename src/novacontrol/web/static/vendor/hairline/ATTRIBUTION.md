# Hairline (vendored)

- Project: Hairline — thirty-three isometric line figures that answer the pointer
- Author: Lucas Marques
- Source: https://github.com/lucasmarkes/hairline
- Package: `@lucasmarkes/hairline@0.5.1` (`dist/index.js`)
- Licence: MIT (see `LICENSE` in this directory)

## What is vendored

`index.js` is the published ESM bundle, copied unmodified except that the final
`//# sourceMappingURL=index.js.map` comment was replaced with a note that the
source map is not vendored. It has no runtime dependencies.

NovaControl vendors it because this project has no npm toolchain (`AGENTS.md`:
never npm) and Hairline is ESM-only. The Atrium imports the file lazily, only
when the panel is first opened, so the 214 KB bundle is not part of the initial
page load.

## Figures used

`hub`, `query`, `dish`, `phone`, `loupe`, `terrain`, `laptop`, `drawer`,
`cabinet`, `vault`, `terminal`.

Each is mounted into its own element with `theme: "dark"`; NovaControl supplies
the object labels, live status, layout, connectors, and navigation. The MIT
notice in `LICENSE` covers the vendored code.
