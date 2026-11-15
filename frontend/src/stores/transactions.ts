import { defineStore } from 'pinia';
import { ref, reactive } from 'vue';
import { getTransactions, bulkPatchTransactions, deleteTransaction } from '@/api/transactions';
import type {
  TransactionItem,
  TransactionListQueryParams,
  DirectionType,
} from '@/types/api';

export const useTransactionsStore = defineStore('transactions', () => {
  const items = ref<TransactionItem[]>([]);
  const total = ref(0);
  const loading = ref(false);
  const selectedIds = ref<number[]>([]);

  const pagination = reactive({
    page: 1,
    pageSize: 20,
  });

  const filters = reactive<{
    dateRange: [string, string] | null;
    accountId: number | null;
    fundAccountId: number | null;
    source: string | null;
    categoryId: number | null;
    direction: DirectionType | null;
    status: string | null;
    q: string;
    excluded: number | null;
    mergeDuplicates: boolean;
    unclassified: boolean;
    currency: string | null;
    fx: string | null;
  }>({
    dateRange: null,
    accountId: null,
    fundAccountId: null,
    source: null,
    categoryId: null,
    direction: null,
    status: null,
    q: '',
    excluded: 0, // Default hide excluded records
    mergeDuplicates: true, // Default merge duplicate records of the same transaction
    unclassified: false,
    currency: null,
    fx: null,
  });

  async function fetchList() {
    loading.value = true;
    try {
      const params: TransactionListQueryParams = {
        page: pagination.page,
        page_size: pagination.pageSize,
        q: filters.q ? filters.q.trim() : undefined,
      };

      if (filters.dateRange && filters.dateRange.length === 2) {
        params.from = filters.dateRange[0];
        params.to = filters.dateRange[1];
      }
      if (filters.accountId !== null) params.account_id = filters.accountId;
      if (filters.fundAccountId !== null) params.fund_account_id = filters.fundAccountId;
      if (filters.source) params.source = filters.source;
      if (filters.categoryId !== null) params.category_id = filters.categoryId;
      if (filters.direction) params.direction = filters.direction;
      if (filters.status) params.status = filters.status;
      if (filters.excluded !== null) params.excluded = filters.excluded;
      if (filters.mergeDuplicates) params.is_duplicate = false;
      if (filters.unclassified) params.unclassified = true;
      if (filters.currency) params.currency = filters.currency;
      if (filters.fx) params.fx = filters.fx;

      const res = await getTransactions(params);
      items.value = res.items;
      total.value = res.total;
    } catch (e) {
      console.error('Failed to load transactions', e);
    } finally {
      loading.value = false;
    }
  }

  function resetFilters() {
    filters.dateRange = null;
    filters.accountId = null;
    filters.fundAccountId = null;
    filters.source = null;
    filters.categoryId = null;
    filters.direction = null;
    filters.status = null;
    filters.q = '';
    filters.excluded = 0;
    filters.mergeDuplicates = true;
    filters.unclassified = false;
    filters.currency = null;
    filters.fx = null;
    pagination.page = 1;
    return fetchList();
  }

  async function bulkSetCategory(categoryId: number | null) {
    if (selectedIds.value.length === 0) return;
    await bulkPatchTransactions({
      ids: selectedIds.value,
      category_id: categoryId,
    });
    selectedIds.value = [];
    await fetchList();
  }

  async function bulkSetExcluded(excluded: number) {
    if (selectedIds.value.length === 0) return;
    await bulkPatchTransactions({
      ids: selectedIds.value,
      excluded,
    });
    selectedIds.value = [];
    await fetchList();
  }

  async function removeTransaction(id: number) {
    await deleteTransaction(id);
    await fetchList();
  }

  return {
    items,
    total,
    loading,
    selectedIds,
    pagination,
    filters,
    fetchList,
    resetFilters,
    bulkSetCategory,
    bulkSetExcluded,
    removeTransaction,
  };
});
