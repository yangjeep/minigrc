# Third-party notices

miniGRC is licensed under the Apache License 2.0 (see `LICENSE`). It
vendors or depends on the following third-party software, all under
permissive licenses compatible with Apache-2.0 (no copyleft/GPL
dependencies anywhere in this project).

## Vendored frontend assets (`app/static/vendor/`)

| Library | Version | License | Source |
|---|---|---|---|
| Bootstrap | 5.3.3 | MIT | https://getbootstrap.com/ |
| Bootstrap Icons | 1.11.3 | MIT | https://icons.getbootstrap.com/ |
| Tabulator | 6.3.1 | MIT | https://tabulator.info/ |

Bootstrap and Bootstrap Icons' minified files carry their own embedded
`/*! ... Licensed under MIT ... */` header comments. Tabulator's
vendored files only embed a bare copyright line
(`Tabulator v6.3.1 (c) Oliver Folkerd 2025`) without restating the
license inline — this file exists in part to make that explicit:
Tabulator is MIT-licensed per its project source
(https://github.com/olifolkerd/tabulator/blob/master/LICENSE).

## Python dependencies added for the Trust Center feature

| Package | License | Purpose |
|---|---|---|
| `markdown` | BSD-3-Clause | Markdown-to-HTML conversion (`app/markdown_render.py`) |
| `nh3` (Rust `ammonia` bindings) | MIT | HTML sanitization of the converted output |

See `pyproject.toml` for the full dependency list and
`docs/decisions/architectural-decisions.md` (#24) for the rationale
behind each runtime dependency choice made during the platform pivot.
