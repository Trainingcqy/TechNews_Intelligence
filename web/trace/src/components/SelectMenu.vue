<script setup>
import { ChevronDown } from "@lucide/vue";
import { computed, onBeforeUnmount, ref, useId, watch } from "vue";

const props = defineProps({
  modelValue: { type: String, default: "" },
  options: { type: Array, required: true },
  label: { type: String, required: true }
});
const emit = defineEmits(["update:modelValue"]);

const id = useId();
const root = ref(null);
const open = ref(false);
const active = ref(0);
const current = computed(() => props.options.find((option) => option.value === props.modelValue) || props.options[0]);

function show() {
  active.value = Math.max(props.options.indexOf(current.value), 0);
  open.value = true;
}

function toggle() {
  if (open.value) open.value = false;
  else show();
}

function choose(option) {
  open.value = false;
  if (option.value !== props.modelValue) emit("update:modelValue", option.value);
}

function onKeydown(event) {
  const count = props.options.length;
  if (!open.value) {
    if (["ArrowDown", "ArrowUp", "Enter", " "].includes(event.key)) {
      event.preventDefault();
      show();
    }
    return;
  }
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    active.value = (active.value + (event.key === "ArrowDown" ? 1 : count - 1)) % count;
  } else if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    choose(props.options[active.value]);
  } else if (event.key === "Escape" || event.key === "Tab") {
    open.value = false;
  }
}

function closeOutside(event) {
  if (!root.value?.contains(event.target)) open.value = false;
}

watch(open, (value) => {
  if (value) document.addEventListener("pointerdown", closeOutside);
  else document.removeEventListener("pointerdown", closeOutside);
});
onBeforeUnmount(() => document.removeEventListener("pointerdown", closeOutside));
</script>

<template>
  <div ref="root" class="select" :class="{ open }">
    <button
      type="button"
      role="combobox"
      class="select-trigger"
      aria-haspopup="listbox"
      :aria-label="label"
      :aria-expanded="open"
      :aria-controls="`${id}-menu`"
      :aria-activedescendant="open ? `${id}-${active}` : undefined"
      @click="toggle"
      @keydown="onKeydown"
    >
      <span class="select-value">{{ current.label }}</span>
      <ChevronDown :size="18" class="select-chevron" />
    </button>
    <Transition name="menu">
      <!-- mousedown is prevented so the focus stays on the trigger. -->
      <ul v-if="open" :id="`${id}-menu`" class="select-menu" role="listbox" :aria-label="label" @mousedown.prevent>
        <li
          v-for="(option, index) in options"
          :id="`${id}-${index}`"
          :key="option.value"
          role="option"
          class="select-option"
          :class="{ active: index === active }"
          :aria-selected="option.value === modelValue"
          @pointerenter="active = index"
          @click="choose(option)"
        >
          {{ option.label }}
        </li>
      </ul>
    </Transition>
  </div>
</template>
