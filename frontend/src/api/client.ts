import axios, { type AxiosInstance, type AxiosRequestConfig } from 'axios';
import { ElMessage } from 'element-plus';
import type { ApiError } from '@/types/api';

const baseURL = typeof window !== 'undefined' && window.__BILL_API__ ? window.__BILL_API__ : '/api';

export const apiClient: AxiosInstance = axios.create({
  baseURL,
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
});

apiClient.interceptors.response.use(
  (response) => {
    return response.data;
  },
  (error) => {
    let message = '网络请求失败';
    let code = 'NETWORK_ERROR';

    if (error.response) {
      const data = error.response.data as ApiError | undefined;
      if (data && typeof data === 'object') {
        message = data.message || `请求失败 (${error.response.status})`;
        code = data.code || `HTTP_${error.response.status}`;
      } else {
        message = `服务器错误 (${error.response.status})`;
      }
    } else if (error.message) {
      message = error.message;
    }

    // Suppress noisy UI error toasts for silent background checks if requested
    const config = error.config as (AxiosRequestConfig & { silent?: boolean }) | undefined;
    const toasted = !config?.silent;
    if (toasted) {
      ElMessage.error(message);
    }

    return Promise.reject({ code, message, original: error, toasted });
  }
);

export default apiClient;
