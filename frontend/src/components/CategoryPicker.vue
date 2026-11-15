<template>
  <el-cascader
    :model-value="modelValue"
    :options="cascaderOptions"
    :props="cascaderProps"
    :placeholder="placeholder"
    :clearable="clearable"
    :filterable="true"
    :size="size"
    class="category-picker"
    @update:model-value="handleChange"
  />
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { useAppStore } from '@/stores/app';
import type { CategoryItemOut } from '@/types/api';

const props = withDefaults(
  defineProps<{
    modelValue?: number | number[] | null;
    placeholder?: string;
    clearable?: boolean;
    size?: '' | 'default' | 'small' | 'large';
    emitPath?: boolean; // false to emit just the child category ID
  }>(),
  {
    modelValue: null,
    placeholder: '选择分类',
    clearable: true,
    size: 'default',
    emitPath: false,
  }
);

const emit = defineEmits<{
  (e: 'update:modelValue', val: any): void;
  (e: 'change', val: any, node?: any): void;
}>();

const appStore = useAppStore();

const cascaderProps = computed(() => ({
  value: 'id',
  label: 'name',
  children: 'children',
  emitPath: props.emitPath,
  checkStrictly: true, // Allow selecting top-level categories
}));

interface CascaderOption {
  id: number;
  name: string;
  children?: CascaderOption[];
}

function mapTree(nodes: CategoryItemOut[]): CascaderOption[] {
  return nodes.map((node) => ({
    id: node.id,
    name: node.name,
    children: node.children && node.children.length > 0 ? mapTree(node.children) : undefined,
  }));
}

const cascaderOptions = computed(() => {
  return mapTree(appStore.categories);
});

function handleChange(val: any) {
  emit('update:modelValue', val);
  emit('change', val);
}
</script>

<style scoped>
.category-picker {
  width: 100%;
}
</style>
