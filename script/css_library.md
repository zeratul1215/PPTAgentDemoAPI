### Design tokens (overridable via class or inline style)

- **Colors**: `--paper` (page background), `--ink` (main text), `--muted` (secondary text), `--purple/--orange/--green` (palette)
- **Accent**: `--accent` (accent color, default purple), `--accent-ink` (text color on accent backgrounds)
- **Spacing**: `--space-s` `--space-m` `--space-l`
- **Radii**: `--radius` `--radius-pill`
- **Typography**: `--text-lg` `--text-md` `--text-sm` and line-heights `--lh-lg` `--lh-md` `--lh-sm`

### Page root container

- **`.page`**: the root container for each slide (fixed size, `overflow:hidden`, `page-break-after:always`). Put all page content inside `<div class="page">...</div>`.

### Palette helpers (set the accent color `--accent`)

- **`.accent-purple`**: set `--accent` to purple  
- **`.accent-orange`**: set `--accent` to orange  
- **`.accent-green`**: set `--accent` to green  

These classes are typically applied on the page or a large container, affecting child components that use `--accent` (e.g. `.band` / `.card-accent` / `.badge`).

### Layout utilities

These are typically applied on container elements:

- **`.u-flex`**: `display:flex` with `gap: var(--gap, var(--space-m))`
- **`.u-grid`**: `display:grid` with `gap: var(--gap, var(--space-m))`
- **`.u-row`**: flex direction row (`flex-direction:row`)
- **`.u-col`**: flex direction column (`flex-direction:column`)
- **`.u-center`**: center children (`align-items:center; justify-content:center`)
- **`.u-between`**: space-between on main axis (`justify-content:space-between`)
- **`.u-wrap`**: allow wrapping (`flex-wrap:wrap`)

### Spacing helpers and 3 scales (`s/m/l`)

#### Spacing classes (apply on containers)

- **`.gap`**: set `gap: var(--gap, var(--space-m))` (works on flex/grid)
- **`.pad`**: set `padding: var(--pad, var(--space-m))`
- **`.pad-x`**: horizontal padding only `padding: 0 var(--pad-x, var(--space-m))`
- **`.pad-y`**: vertical padding only `padding: var(--pad-y, var(--space-m)) 0`

#### Scale modifiers (switch variables to small/medium/large)

`.s` / `.m` / `.l` set a group of CSS variables (`--gap/--pad/--pad-x/--pad-y/--stack-gap`).  
Because CSS variables inherit, you can apply them on the same container **or** on an ancestor container so children can reuse the scale.

- **`.s`**: set those vars to `--space-s`
- **`.m`**: set those vars to `--space-m`
- **`.l`**: set those vars to `--space-l`

#### Vertical rhythm (Stack)

- **`.stack`**: add vertical spacing between direct children: `.stack > * + * { margin-top: var(--stack-gap, var(--space-m)) }`

### Typography (3 sizes)

Typically applied to text elements or text containers (`div/p/span`):

- **`.t-lg`**: large (title-level)  
  Key effects: `font-size: var(--text-lg)`, `font-weight:900`, `line-height: var(--lh-lg)`, `letter-spacing:0.4pt`, `color:inherit` (inherits text color from parent)
- **`.t-md`**: medium (subtitle / larger body)  
  Key effects: `font-size: var(--text-md)`, `font-weight:700`, `line-height: var(--lh-md)`, `overflow-wrap:anywhere`, `color:inherit`
- **`.t-sm`**: small (body / notes)  
  Key effects: `font-size: var(--text-sm)`, `line-height: var(--lh-sm)`, `overflow-wrap:anywhere`, `color:inherit`
- **`.t-muted`**: secondary text color (`color: var(--muted)`)
- **`.t-strong`**: strong emphasis (`font-weight:900`)
- **`.ls-wide`**: wider letter spacing (`letter-spacing:0.8pt`)

### Images

Usually a 2-layer structure: an outer container + an inner `<img>`:

- **`.img-frame`**: image frame (rounded corners, clip, light background, subtle border).  
  Note: `.img-frame` does not set a size; the container should be sized via layout or inline style.
- **`.img-cover`**: on `<img>`, `object-fit:cover` (fills, may crop)
- **`.img-contain`**: on `<img>`, `object-fit:contain` (no crop, may letterbox)

### Surfaces / components

- **`.band`**: a colored band / title bar (background uses `--accent`, text uses `--accent-ink`, padding is `space-m space-l`)
- **`.card`**: translucent white card (rounded, padded, subtle border, dark text)
- **`.card-accent`**: accent card (background `--accent`, text `--accent-ink`, no border)
- **`.badge`**: pill badge (inline-flex, centered, `border-radius: var(--radius-pill)`, small font, `--accent` background)

### Lists

- **`.list-reset`**: remove default list style (`margin:0; padding:0; list-style:none`)
- **`.cols-2`**: 2-column text flow (`columns:2`, `column-gap: var(--space-l)`)

