// v-tip: tooltips on mouse hover and keyboard focus. One element on <body> serves all of them, so scroll areas with
// overflow or masks never clip it.
const SHOW_DELAY_MS = 300;
const GAP = 8;
const MARGIN = 8;

let tipEl = null;
let owner = null;
let pending = null;
let timer = 0;

function tipElement() {
  if (!tipEl) {
    tipEl = document.createElement("div");
    tipEl.className = "tooltip";
    tipEl.setAttribute("aria-hidden", "true");
    document.body.append(tipEl);
    window.addEventListener("scroll", hide, true);
    window.addEventListener("resize", hide);
  }
  return tipEl;
}

// Below the target and centered, above it when the viewport has no room below, always inside the viewport.
function place(target) {
  const box = target.getBoundingClientRect();
  const { offsetWidth: width, offsetHeight: height } = tipEl;
  const below = box.bottom + GAP;
  const top = below + height > window.innerHeight - MARGIN ? box.top - GAP - height : below;
  const left = Math.min(Math.max(box.left + (box.width - width) / 2, MARGIN), window.innerWidth - MARGIN - width);
  tipEl.style.top = `${top}px`;
  tipEl.style.left = `${left}px`;
}

function show(target) {
  pending = null;
  if (!target._tip) return;
  tipElement().textContent = target._tip;
  owner = target;
  place(target);
  tipEl.classList.add("visible");
}

function hide() {
  clearTimeout(timer);
  pending = null;
  owner = null;
  tipEl?.classList.remove("visible");
}

function schedule(target) {
  clearTimeout(timer);
  pending = target;
  timer = setTimeout(() => show(target), SHOW_DELAY_MS);
}

function onPointerEnter(event) {
  if (event.pointerType === "mouse") schedule(event.currentTarget);
}

function onFocus(event) {
  if (event.currentTarget.matches(":focus-visible")) schedule(event.currentTarget);
}

// Material Symbols ligatures such as "bookmark" are icon names, not readable text.
function isIconOnly(el) {
  const clone = el.cloneNode(true);
  clone.querySelectorAll(".material-symbols-outlined").forEach((icon) => icon.remove());
  return !clone.textContent.trim();
}

// The tooltip replaces title, which also gave icon-only buttons their accessible name.
function setText(el, text) {
  el._tip = text || "";
  if (el._tipNames) {
    if (el._tip) el.setAttribute("aria-label", el._tip);
    else el.removeAttribute("aria-label");
  }
  if (owner !== el) return;
  if (el._tip) {
    tipEl.textContent = el._tip;
    place(el);
  } else {
    hide();
  }
}

export const tip = {
  mounted(el, { value }) {
    el._tipNames = !el.hasAttribute("aria-label") && isIconOnly(el);
    setText(el, value);
    el.addEventListener("pointerenter", onPointerEnter);
    el.addEventListener("pointerleave", hide);
    el.addEventListener("focus", onFocus);
    el.addEventListener("blur", hide);
  },
  updated(el, { value }) {
    if ((value || "") !== el._tip) setText(el, value);
  },
  beforeUnmount(el) {
    if (owner === el || pending === el) hide();
    el.removeEventListener("pointerenter", onPointerEnter);
    el.removeEventListener("pointerleave", hide);
    el.removeEventListener("focus", onFocus);
    el.removeEventListener("blur", hide);
  }
};
