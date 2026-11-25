import { createApp } from 'vue';
import { createPinia } from 'pinia';
import ElementPlus from 'element-plus';
import 'element-plus/dist/index.css';

import App from './App.vue';
import router from './router';
import { installGlobalErrorCapture } from './utils/logCollector';

const app = createApp(App);
const pinia = createPinia();

installGlobalErrorCapture(app);

app.use(pinia);
app.use(router);
app.use(ElementPlus);

app.mount('#app');
