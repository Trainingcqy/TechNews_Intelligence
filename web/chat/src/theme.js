import { computed, onBeforeUnmount, onMounted, ref } from "vue";

const STORAGE_KEY = "technews_theme";
const root = document.documentElement;
const media = window.matchMedia("(prefers-color-scheme: dark)");

function storedTheme() {
  let value = "";
  try {
    value = localStorage.getItem(STORAGE_KEY);
  } catch {
    value = "";
  }
  return value === "dark" || value === "light" ? value : "";
}

function systemTheme() {
  return media.matches ? "dark" : "light";
}

// Elements with a color transition would otherwise reach the new theme later than the rest of the page.
function withoutTransitions(change) {
  const style = document.createElement("style");
  style.textContent = "*, *::before, *::after { transition: none !important; }";
  document.head.append(style);
  change();
  // Reading layout applies the new colors while transitions are still off.
  void document.body.offsetWidth;
  setTimeout(() => style.remove());
}

// index.html applies the initial theme before first paint; this keeps it in sync afterwards.
export function useTheme() {
  const theme = ref(root.dataset.theme || systemTheme());

  function applyTheme(next, persist) {
    theme.value = next;
    withoutTransitions(() => {
      root.dataset.theme = next;
      root.style.colorScheme = next;
    });
    if (!persist) return;
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // The toggle keeps working when storage is unavailable.
    }
  }

  function toggleTheme() {
    applyTheme(theme.value === "dark" ? "light" : "dark", true);
  }

  function followSystem() {
    if (!storedTheme()) applyTheme(systemTheme(), false);
  }

  onMounted(() => media.addEventListener("change", followSystem));
  onBeforeUnmount(() => media.removeEventListener("change", followSystem));

  return { isDark: computed(() => theme.value === "dark"), toggleTheme };
}
