import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createWebHistory } from 'vue-router'
import App from './App.vue'
import ChatPage from './ChatPage.vue'
import './style.css'
const router = createRouter({ history: createWebHistory(), routes: [{ path: '/', component: ChatPage }, { path: '/sessions/:id', component: ChatPage }] })
createApp(App).use(createPinia()).use(router).mount('#app')
