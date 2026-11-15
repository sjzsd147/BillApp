import { defineStore } from 'pinia';
import { ref, computed } from 'vue';
import { getReviewList, confirmReview, rejectReview, bulkConfirmReview } from '@/api/review';
import { useAppStore } from './app';
import type { ReviewItemOut } from '@/types/api';

export const useReviewStore = defineStore('review', () => {
  const items = ref<ReviewItemOut[]>([]);
  const loading = ref(false);
  const currentIndex = ref(0);

  const appStore = useAppStore();

  const currentItem = computed(() => {
    if (items.value.length === 0) return null;
    if (currentIndex.value >= items.value.length) {
      return items.value[items.value.length - 1];
    }
    return items.value[currentIndex.value];
  });

  const highConfidenceItems = computed(() => {
    return items.value.filter((i) => i.score >= 0.85);
  });

  async function fetchList() {
    loading.value = true;
    try {
      items.value = await getReviewList();
      appStore.setPendingReviewCount(items.value.length);
      if (currentIndex.value >= items.value.length) {
        currentIndex.value = Math.max(0, items.value.length - 1);
      }
    } catch (e) {
      console.error('Failed to load review candidates', e);
    } finally {
      loading.value = false;
    }
  }

  async function accept(linkId: number) {
    await confirmReview(linkId);
    items.value = items.value.filter((item) => item.id !== linkId);
    appStore.decrementReviewCount();
    if (currentIndex.value >= items.value.length) {
      currentIndex.value = Math.max(0, items.value.length - 1);
    }
  }

  async function reject(linkId: number) {
    await rejectReview(linkId);
    items.value = items.value.filter((item) => item.id !== linkId);
    appStore.decrementReviewCount();
    if (currentIndex.value >= items.value.length) {
      currentIndex.value = Math.max(0, items.value.length - 1);
    }
  }

  async function acceptAllHighConfidence() {
    const ids = highConfidenceItems.value.map((i) => i.id);
    if (ids.length === 0) return;
    await bulkConfirmReview(ids);
    await fetchList();
  }

  function deferCurrent() {
    if (items.value.length <= 1) return;
    const [current] = items.value.splice(currentIndex.value, 1);
    items.value.push(current);
  }

  function next() {
    if (currentIndex.value < items.value.length - 1) {
      currentIndex.value++;
    }
  }

  function prev() {
    if (currentIndex.value > 0) {
      currentIndex.value--;
    }
  }

  return {
    items,
    loading,
    currentIndex,
    currentItem,
    highConfidenceItems,
    fetchList,
    accept,
    reject,
    acceptAllHighConfidence,
    deferCurrent,
    next,
    prev,
  };
});
