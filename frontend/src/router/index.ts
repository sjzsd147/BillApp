import { createRouter, createWebHashHistory } from 'vue-router';

const routes = [
  {
    path: '/',
    redirect: '/dashboard',
  },
  {
    path: '/',
    component: () => import('@/layout/AppLayout.vue'),
    children: [
      {
        path: 'dashboard',
        name: 'Dashboard',
        component: () => import('@/views/DashboardView.vue'),
        meta: { title: '看板' },
      },
      {
        path: 'import',
        name: 'Import',
        component: () => import('@/views/ImportView.vue'),
        meta: { title: '导入' },
      },
      {
        path: 'review',
        name: 'Review',
        component: () => import('@/views/ReviewView.vue'),
        meta: { title: '待确认' },
      },
      {
        path: 'transactions',
        name: 'Transactions',
        component: () => import('@/views/TransactionsView.vue'),
        meta: { title: '明细' },
      },
      {
        path: 'rules',
        name: 'Rules',
        component: () => import('@/views/RulesView.vue'),
        meta: { title: '分类与规则' },
      },
      {
        path: 'accounts',
        name: 'Accounts',
        component: () => import('@/views/AccountsView.vue'),
        meta: { title: '账户' },
      },
      {
        path: 'settings/:tab?',
        name: 'Settings',
        component: () => import('@/views/SettingsView.vue'),
        meta: { title: '设置' },
      },
    ],
  },
  {
    // Mapping wizard has a dedicated full-screen layout per D8
    path: '/import/mapping/:token?',
    name: 'MappingWizard',
    component: () => import('@/views/MappingWizard.vue'),
    meta: { title: '字段映射向导' },
  },
];

export const router = createRouter({
  history: createWebHashHistory(),
  routes,
});

export default router;
