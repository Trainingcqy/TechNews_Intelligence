import { createApp } from "vue";
import App from "./App.vue";
import "./styles.css";
import { tip } from "./tooltip";

createApp(App).directive("tip", tip).mount("#app");
