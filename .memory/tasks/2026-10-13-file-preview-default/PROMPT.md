# PROMPT

## Objective

User request: "when opening md/html etc in file viewer, enter preview mode by default."

Deliver: opening a previewable document (markdown, HTML) in the file viewer renders its preview by default instead of the editor source, in every entry path (file picker, transcript file links, remembered per-session file). Markdown already has a renderer; HTML has none, so HTML preview must exist for the request to hold for `.html`.

Done when, in a real browser against a Docker-isolated server: clicking a `.md` file link and a `.html` file link in the transcript (and picking the same files in the file picker) both land in preview mode showing rendered content, with source one click away, and no regression in diff mode, editing, or the existing markdown preview.

## Scope

### Root cause (mechanism)

Two independent mechanisms force source view today:

1. `resolveFileOpenMode` (`codoxear/static/app_file_viewer_controller.js`) returns `"preview"` for markdown only when the *globally persisted* `codexweb.fileNonDiffMode` is `"preview"`; its default is `"file"`. Worse, `setFileViewMode("file")` writes that same key on **every** non-previewable open, so "preview" never persists. The per-kind default is thus derived from a value that means "the current viewer mode", conflating two orthogonal states.
2. `openReference` (`codoxear/static/app_file_viewer.js`) hardcodes `mode: "file"` for every transcript file link, which *sets* the global mode to `file` before resolution runs — the dominant entry path bypasses the kind default entirely.

### Changes

- Per-kind default: previewable document kinds (markdown, html) open in preview unless the file is a git-changed diffable candidate (diff), or the caller requests an explicit mode.
- Explicit user override: the preview/source toggle on a previewable file persists a dedicated per-device preference (`codexweb.filePreviewMode`, default `preview`), written only by that toggle — never clobbered by opening other file kinds.
- Line-targeted transcript links (`path.md:12`) keep source view: a line anchor is only meaningful in source. `show({mode})` must forward its mode into the open-mode resolution so the explicit choice survives.
- HTML preview: new server-provided document kind `html` for `.html`/`.htm` plus `GET /api/sessions/<id>/file/raw/<path>` serving the document (and its relative subresources) with a real directory-shaped URL so the browser resolves `img/…`, `style.css`, and CSS `url()` natively. Rendered in a sandboxed iframe (`sandbox="allow-same-origin"`, no scripts) inside the existing file-viewer host.
- Never break: diff-first resolution for changed diffable files, file/write editing paths, plain-text fallback, image/pdf/video surfaces, and the markdown preview.

### Threat model for the sandbox

The app is single-user and cookie-authenticated (`SameSite=Strict`, httpOnly) on the same origin that serves the file. An unsandboxed HTML document from the session cwd would run scripts with that cookie and could call the app's own authenticated API (read transcripts, delete sessions, send prompts). Therefore: iframe sandbox without `allow-scripts`, plus `Content-Security-Policy: sandbox allow-same-origin` on document responses from the raw route so the guarantee survives a direct navigation to that URL. `allow-same-origin` is required (not optional) because a `SameSite=Strict` cookie is not sent from an opaque-origin frame, which would 401 every relative subresource.

## Deliverables

1. Server: `html` document kind + `/file/raw/<path>` route with MIME table, `nosniff`, sandbox CSP on documents, percent-decoding, behavioral tests.
2. Client: per-kind preview default, previewable-kind plumbing, explicit-mode forwarding, sandboxed HTML preview surface, client extension sets pinned against the Python sets.
3. AGENTS.md: durable invariants (preview-default rule, HTML preview sandbox requirement, raw route).
4. Task memory: OPS evidence trail, EPISTEMIC model.
5. Docker + real-browser verification of all entry paths.

## Constraints

- No source-string tests. Behavioral tests only (VM harness for JS, executed handlers for Python, real browser for the product).
- Docker is the isolation boundary for any server/browser verification; never probe the live deployment at 127.0.0.1:8743.
- `scripts/check_wiring.py` must pass; every controller dependency goes through `app_wiring.js` select contracts, no bags or spreads.
- Do not touch unrelated in-flight work in the shared checkout (voice, notifications, themes, other task dirs).
- Commit only explicit scoped files after staged-diff review.
