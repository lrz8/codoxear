# EPISTEMIC

## Phenomenon

Opening a markdown (or any HTML) file in the file viewer showed Monaco source instead of the rendered document. The user asked for preview to be the default for `md`/`html` and similar documents.

## Mechanism (current model)

The viewer's open-time mode had two independent causes pushing toward source, and neither was "the default is preview":

1. **Wrong state for the decision.** `resolveFileOpenMode` consulted the *remembered current viewer mode* (`codexweb.fileNonDiffMode`) to decide whether a markdown file opens in preview. That key is written by `setFileViewMode("file")` on every non-previewable open, so the value meant "the mode I last looked at", not "the default for markdown". Its initial default was `"file"`.
2. **Entry-path override.** Transcript file links (the dominant way these files get opened in this product) hardcoded `mode: "file"`, which set the global mode to source before resolution ran, so even a correct per-kind default would be bypassed.

HTML had a third, structural gap: there was no preview renderer for it at all (server kind was `text`; `_send_preview_blob` refuses non-media kinds; the preview control was revealed only for `kind === "markdown"`).

The fix follows the mechanism: make the per-kind default the *file's own document kind* (authoritative, from the server on load), keep an explicit caller mode as the override, and add the missing HTML surface.

## Live mechanisms / decisions

- **Open-mode resolution order** (`resolveFileOpenMode`): explicit `requestedMode` → changed diffable candidate (diff) → previewable kind (preview) → source. The explicit mode is forwarded from `showFileViewer`'s `mode` through the lifecycle into `openFilePathWithResolvedMode`, so a line-targeted transcript reference (`doc.md:120`) keeps source view — the one case where source is strictly more informative than preview.
- **Per-kind default, not remembered default.** Chosen deliberately: the remembered-sticky alternative (`codexweb.filePreviewMode`, written only by the preview toggle) was implemented mentally and rejected because a single "view source once to edit" click would then flip the default for all later markdown opens, which reads as "the feature broke" rather than "I chose this". `codexweb.fileNonDiffMode` keeps its remaining, narrower job: the non-diff mode to return to when leaving diff. Revisit only if the user asks for a sticky source preference.
- **File picker changed rows still open as diffs.** `openFilePathWithResolvedMode` receives `changed`/`gitPath` from the picker entry; a changed diffable candidate resolves to `diff` before the kind default applies. Transcript links carry `changed: null, gitPath: false` (`resolveFileViewerOpenTarget` source "explicit"), so they get the kind default instead. Verified by test.
- **Two previewable-kind predicates, deliberately.** The server kind is authoritative *after* a load (`isPreviewableFileKind`), while the pre-load clamp and the mode decision need the same answer from the path alone (`isPreviewableFilePath`). The extension sets live in one client module and are pinned against `codoxear/file_types.py` by a test, because a silent divergence would show up as "the mode says preview but nothing renders".
- **HTML preview surface.** Server-provided `preview_url` (`/api/sessions/<id>/file/raw/<path>`) is rendered in an `iframe.fileHtmlPreview`. Mechanism choice: a real directory-shaped URL instead of `srcdoc` + attribute rewriting, so the browser resolves the document's own relative subresources (img, stylesheet href, CSS `url()`) natively — rewriting could never cover CSS-level references. The route serves arbitrary regular files with an extension-derived MIME type (default octet-stream) so referenced assets load.
- **Sandbox is the security mechanism, not decoration.** The app is single-user, cookie-authenticated, and the same origin serves its own API, so an unsandboxed session-cwd document would script with the user's authority (read transcripts, delete sessions, send prompts). Controls: iframe `sandbox="allow-same-origin"` without `allow-scripts`; route-level `Content-Security-Policy: sandbox allow-same-origin` so a direct navigation to the raw URL is inert too; `X-Content-Type-Options: nosniff` so an unknown type is never sniffed into a document. `allow-same-origin` is load-bearing: `SameSite=Strict` means an opaque-origin frame gets no cookie and every relative subresource 401s. Scripts therefore never run in preview; an HTML page that depends on JS shows its static form (accepted boundary, documented).
- **Honest boundary instead of an empty frame.** If an html result arrives without `preview_url`, the viewer shows source, corrects the mode to `file`, and puts `preview unavailable` in the status line rather than rendering an empty sandboxed frame.

## Ruled out

- Persisting a preview preference for previewable kinds (see "per-kind default" above) — rejected on product grounds, not evidence.
- Client-side relative-URL rewriting for HTML (`srcset`, stylesheet `href`, CSS `url()` inside `<style>`) — cannot cover CSS-internal references, and the raw route covers them for free.
- Rendering HTML into the markdown preview container — a document's own `<style>` would leak into the app chrome; only a frame gives CSS isolation.
- Letting scripts run with the sandbox only on the iframe (no route CSP): leaves a direct-navigation gap on an authenticated origin. Rejected.

## Unexplained / residual risk

- Keyboard focus inside the preview frame follows normal iframe semantics: after clicking into the preview, app-level vim/hint keys reach the frame, not the parent document. Accepted for now (no prior iframe existed, so nothing regressed); a mitigation would need a forwarding mechanism from the frame's document.
- Failure surfaced, not yet root-caused: `tests/test_file_picker_session_state.py` broke during the client work purely because its fake dependency bag did not carry the new predicates; fixed by supplying them. No product implication.
- Not yet verified in a real browser (Docker): the html preview's relative asset resolution, the CSS-isolated rendering, the picker/transcript entry paths end to end, and that a changed markdown row still opens as a diff. This is the open obligation.
