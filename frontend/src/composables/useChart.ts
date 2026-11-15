import { ref, onMounted, onUnmounted, shallowRef, type Ref } from 'vue';
import * as echarts from 'echarts/core';
import {
  BarChart,
  LineChart,
  PieChart,
  SunburstChart,
  HeatmapChart,
} from 'echarts/charts';
import {
  TitleComponent,
  TooltipComponent,
  GridComponent,
  LegendComponent,
  VisualMapComponent,
  CalendarComponent,
} from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import type { EChartsCoreOption, EChartsType } from 'echarts/core';

// Register necessary ECharts components
echarts.use([
  BarChart,
  LineChart,
  PieChart,
  SunburstChart,
  HeatmapChart,
  TitleComponent,
  TooltipComponent,
  GridComponent,
  LegendComponent,
  VisualMapComponent,
  CalendarComponent,
  CanvasRenderer,
]);

export function useChart(containerRef: Ref<HTMLElement | null>) {
  const chartInstance = shallowRef<EChartsType | null>(null);
  const resizeObserver = ref<ResizeObserver | null>(null);

  function initChart() {
    if (!containerRef.value) return;
    if (chartInstance.value) {
      chartInstance.value.dispose();
    }
    const chart = echarts.init(containerRef.value);
    chartInstance.value = chart;

    if (window.ResizeObserver) {
      resizeObserver.value = new ResizeObserver(() => {
        chart.resize();
      });
      resizeObserver.value.observe(containerRef.value);
    } else {
      window.addEventListener('resize', handleWindowResize);
    }
  }

  function handleWindowResize() {
    chartInstance.value?.resize();
  }

  function setOption(option: EChartsCoreOption, notMerge = false) {
    if (!chartInstance.value) {
      initChart();
    }
    chartInstance.value?.setOption(option, notMerge);
  }

  onMounted(() => {
    initChart();
  });

  onUnmounted(() => {
    if (resizeObserver.value) {
      resizeObserver.value.disconnect();
      resizeObserver.value = null;
    } else {
      window.removeEventListener('resize', handleWindowResize);
    }
    chartInstance.value?.dispose();
    chartInstance.value = null;
  });

  return {
    chartInstance,
    setOption,
    resize: () => chartInstance.value?.resize(),
  };
}
