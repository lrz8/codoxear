function clamp(value, lower, upper) {
  return Math.min(Math.max(value, lower), upper);
}

/*
 * Decide a picker rectangle in viewport coordinates. `wrapLeft` and
 * `wrapRight` are the usable input-wrap edges after its horizontal gutter has
 * been applied. The caller converts top/left to the picker's positioned
 * ancestor before writing styles.
 */
function decidePickerGeometry({
  caretTop,
  caretBottom,
  tokenLeft,
  menuNaturalHeight,
  menuWidth,
  wrapLeft,
  wrapRight,
  minTop,
  maxBottom,
  textareaTop,
  textareaBottom,
  gap,
  minHeight,
}) {
  const above = Math.max(0, caretTop - minTop - gap);
  const below = Math.max(0, maxBottom - caretBottom - gap);
  let dir = above < menuNaturalHeight && below > above ? "down" : "up";
  if (above < minHeight && below < minHeight) dir = below > above ? "down" : "up";

  const available = dir === "up" ? above : below;
  const maxHeight = Math.max(minHeight, Math.min(menuNaturalHeight, available));
  const renderedHeight = Math.min(menuNaturalHeight, maxHeight);
  const desiredTop = dir === "up"
    ? caretTop - gap - renderedHeight
    : caretBottom + gap;
  const top = clamp(desiredTop, minTop, Math.max(minTop, maxBottom - renderedHeight));
  const left = clamp(tokenLeft, wrapLeft, Math.max(wrapLeft, wrapRight - menuWidth));
  const visible = caretTop >= textareaTop && caretBottom <= textareaBottom;

  return { top, left, maxHeight, dir, visible };
}

function measureTextareaCaret(textarea, index, { documentTarget = document, getComputedStyleFn = getComputedStyle } = {}) {
  if (!textarea || !documentTarget || !documentTarget.body || typeof textarea.getBoundingClientRect !== "function") return null;
  const rect = textarea.getBoundingClientRect();
  const styles = getComputedStyleFn(textarea);
  const mirror = documentTarget.createElement("div");
  const marker = documentTarget.createElement("span");
  const value = String(textarea.value || "");
  const caret = Math.max(0, Math.min(Number.isFinite(index) ? index : 0, value.length));

  Object.assign(mirror.style, {
    position: "fixed",
    left: `${rect.left}px`,
    top: `${rect.top}px`,
    width: `${textarea.clientWidth}px`,
    height: `${textarea.clientHeight}px`,
    margin: "0",
    overflow: "hidden",
    visibility: "hidden",
    pointerEvents: "none",
    whiteSpace: "pre-wrap",
    boxSizing: styles.boxSizing,
    fontFamily: styles.fontFamily,
    fontSize: styles.fontSize,
    fontWeight: styles.fontWeight,
    fontStyle: styles.fontStyle,
    fontVariant: styles.fontVariant,
    fontStretch: styles.fontStretch,
    fontKerning: styles.fontKerning,
    fontFeatureSettings: styles.fontFeatureSettings,
    lineHeight: styles.lineHeight,
    paddingTop: styles.paddingTop,
    paddingRight: styles.paddingRight,
    paddingBottom: styles.paddingBottom,
    paddingLeft: styles.paddingLeft,
    borderTopWidth: styles.borderTopWidth,
    borderRightWidth: styles.borderRightWidth,
    borderBottomWidth: styles.borderBottomWidth,
    borderLeftWidth: styles.borderLeftWidth,
    borderStyle: "solid",
    borderColor: "transparent",
    overflowWrap: styles.overflowWrap,
    wordBreak: styles.wordBreak,
    wordSpacing: styles.wordSpacing,
    letterSpacing: styles.letterSpacing,
    tabSize: styles.tabSize,
    direction: styles.direction,
    textTransform: styles.textTransform,
    textIndent: styles.textIndent,
  });
  mirror.appendChild(documentTarget.createTextNode(value.slice(0, caret)));
  marker.textContent = "\u200b";
  mirror.appendChild(marker);
  mirror.appendChild(documentTarget.createTextNode(value.slice(caret)));
  documentTarget.body.appendChild(mirror);
  mirror.scrollTop = textarea.scrollTop;
  mirror.scrollLeft = textarea.scrollLeft;
  const markerRect = marker.getBoundingClientRect();
  mirror.remove();
  return markerRect;
}

export { decidePickerGeometry, measureTextareaCaret };
