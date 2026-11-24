import * as echarts from 'echarts/core';
import { LegacyGridContainLabel } from 'echarts/features';

/*
 * ECharts 6 changed the default theme (palette, legend at the bottom, tooltip and axis styling) and
 * turned on new grid layout behaviour. NetworkMonitor keeps the ECharts 5 look, so every chart is
 * created through `initChart`, which uses the theme below:
 *
 *  - the ECharts 5 default theme, ported from `echarts/theme/v5.js` (Apache-2.0, Apache ECharts);
 *    the upgrade guide names that file as the way to restore the v5 defaults;
 *  - grid.outerBoundsMode 'none': no v6 anti-overflow shift of the grid;
 *  - axis nameMoveOverlap false: no v6 axis-name shifting;
 *  - axis containShape false: bars on time/value axes may reach the grid edge as in v5 (6.1);
 *  - LegacyGridContainLabel: `grid.containLabel` keeps the v5 algorithm (flows charts).
 */

const V5_COLORS = [
  '#5470c6',
  '#91cc75',
  '#fac858',
  '#ee6666',
  '#73c0de',
  '#3ba272',
  '#fc8452',
  '#9a60b4',
  '#ea7ccc',
];

const v5AxisCommon = () => {
  return {
    axisLine: {
      lineStyle: {
        color: '#6E7079',
      },
    },
    axisLabel: {
      color: null,
    },
    splitLine: {
      lineStyle: {
        color: ['#E0E6F1'],
      },
    },
    splitArea: {
      areaStyle: {
        color: ['rgba(250,250,250,0.2)', 'rgba(210,219,238,0.2)'],
      },
    },
    minorSplitLine: {
      color: '#F4F7FD',
    },
  };
};

const V5_GRADIENT = ['#f6efa6', '#d88273', '#bf444c'];

const V5_THEME = {
  color: V5_COLORS,
  gradientColor: V5_GRADIENT,

  loading: {
    textColor: 'red',
  },

  // Series

  bar: {
    defaultBarGap: '20%',
    select: {
      itemStyle: {
        borderColor: '#212121',
        borderWidth: 1,
      },
    },
  },

  boxplot: {
    emphasis: {
      itemStyle: {
        shadowColor: 'rgba(0,0,0,0.2)',
      },
    },
  },

  funnel: {
    bottom: 60,
    select: {
      itemStyle: {
        borderColor: '#212121',
        borderWidth: 1,
      },
    },
    label: {
      color: 'inherit',
    },
  },

  gauge: {
    axisLine: {
      lineStyle: {
        color: [[1, '#E6EBF8']],
      },
    },
    splitLine: {
      lineStyle: {
        color: '#63677A',
      },
    },
    axisTick: {
      lineStyle: {
        color: '#63677A',
      },
    },
    axisLabel: {
      color: '#464646',
    },
    anchor: {
      itemStyle: {
        color: '#fff',
        borderColor: '#5470c6',
      },
    },
    title: {
      color: '#464646',
    },
    detail: {
      backgroundColor: 'rgba(0,0,0,0)',
      borderColor: '#ccc',
      color: '#464646',
    },
  },

  graph: {
    lineStyle: {
      color: '#aaa',
    },
    select: {
      itemStyle: {
        borderColor: '#212121',
      },
    },
  },

  heatmap: {
    select: {
      itemStyle: {
        borderColor: '#212121',
      },
    },
  },

  line: {
    symbolSize: 4,
  },

  pictorialBar: {
    select: {
      itemStyle: {
        borderColor: '#212121',
        borderWidth: 1,
      },
    },
  },

  pie: {
    radius: [0, '75%'],
    labelLine: {
      length2: 15,
    },
  },

  map: {
    defaultItemStyleColor: '#eee',
    label: {
      color: '#000',
    },
    itemStyle: {
      borderColor: '#444',
      areaColor: '#eee',
    },
    emphasis: {
      label: {
        color: 'rgb(100,0,0)',
      },
      itemStyle: {
        areaColor: 'rgba(255,215,0,0.8)',
      },
    },
    select: {
      label: {
        color: 'rgb(100,0,0)',
      },
      itemStyle: {
        color: 'rgba(255,215,0,0.8)',
      },
    },
  },

  sankey: {
    lineStyle: {
      color: '#314656',
    },
    select: {
      itemStyle: {
        borderColor: '#212121',
      },
    },
  },

  scatter: {
    select: {
      itemStyle: {
        borderColor: '#212121',
      },
    },
  },

  tree: {
    lineStyle: {
      color: '#ccc',
    },
  },

  treemap: {
    left: 'center',
    top: 'middle',
    width: '80%',
    height: '80%',
    scaleLimit: {
      max: null,
      min: null,
    },
    breadcrumb: {
      top: 'bottom',
      bottom: null,
      itemStyle: {
        color: 'rgba(0,0,0,0.7)', // '#5793f3',
        textStyle: {
          color: '#fff',
        },
      },
      emphasis: {
        itemStyle: {
          color: 'rgba(0,0,0,0.9)', // '#5793f3',
        },
      },
    },
  },

  // Components

  timeAxis: v5AxisCommon(),
  logAxis: v5AxisCommon(),
  valueAxis: v5AxisCommon(),
  categoryAxis: { ...v5AxisCommon(), axisTick: { show: true } },

  axisPointer: {
    lineStyle: {
      color: '#B9BEC9',
    },
    shadowStyle: {
      color: 'rgba(210,219,238,0.2)',
    },
    label: {
      backgroundColor: 'auto',
      color: '#fff',
    },
    handle: {
      color: '#333',
      shadowBlur: 3,
      shadowColor: '#aaa',
      shadowOffsetX: 0,
      shadowOffsetY: 2,
    },
  },

  brush: {
    brushStyle: {
      color: 'rgba(210,219,238,0.3)',
      borderColor: '#D2DBEE',
    },
    defaultOutOfBrushColor: '#ddd',
  },

  calendar: {
    splitLine: {
      lineStyle: {
        color: '#000',
      },
    },
    itemStyle: {
      borderColor: '#ccc',
    },
    dayLabel: {
      margin: '50%',
      color: '#000',
    },
    monthLabel: {
      margin: 5,
      color: '#000',
    },
    yearLabel: {
      margin: 30,
      color: '#ccc',
    },
  },

  dataZoom: {
    borderColor: '#d2dbee',
    borderRadius: 3,
    backgroundColor: 'rgba(47,69,84,0)',
    dataBackground: {
      lineStyle: {
        color: '#d2dbee',
        width: 0.5,
      },
      areaStyle: {
        color: '#d2dbee',
        opacity: 0.2,
      },
    },
    selectedDataBackground: {
      lineStyle: {
        color: '#8fb0f7',
        width: 0.5,
      },
      areaStyle: {
        color: '#8fb0f7',
        opacity: 0.2,
      },
    },
    handleStyle: {
      color: '#fff',
      borderColor: '#ACB8D1',
    },
    moveHandleStyle: {
      color: '#D2DBEE',
      opacity: 0.7,
    },
    textStyle: {
      color: '#6E7079',
    },
    brushStyle: {
      color: 'rgba(135,175,274,0.15)',
    },
    emphasis: {
      handleStyle: {
        borderColor: '#8FB0F7',
      },
      moveHandleStyle: {
        color: '#8FB0F7',
        opacity: 0.7,
      },
    },
    defaultLocationEdgeGap: 7,
  },

  geo: {
    defaultItemStyleColor: '#eee',
    label: {
      color: '#000',
    },
    itemStyle: {
      borderColor: '#444',
    },
    emphasis: {
      label: {
        color: 'rgb(100,0,0)',
      },
      itemStyle: {
        color: 'rgba(255,215,0,0.8)',
      },
    },
    select: {
      label: {
        color: 'rgb(100,0,0)',
      },
      itemStyle: {
        color: 'rgba(255,215,0,0.8)',
      },
    },
  },

  grid: {
    left: '10%',
    top: 60,
    bottom: 70,
    borderColor: '#ccc',
  },

  legend: {
    top: 0,
    bottom: null,
    backgroundColor: 'rgba(0,0,0,0)',
    borderColor: '#ccc',
    itemGap: 10,
    inactiveColor: '#ccc',
    inactiveBorderColor: '#ccc',
    lineStyle: {
      inactiveColor: '#ccc',
    },
    textStyle: {
      color: '#333',
    },
    selectorLabel: {
      color: '#666',
      borderColor: '#666',
    },
    emphasis: {
      selectorLabel: {
        color: '#eee',
        backgroundColor: '#666',
      },
    },
    pageIconColor: '#2f4554',
    pageIconInactiveColor: '#aaa',
    pageTextStyle: {
      color: '#333',
    },
  },

  radar: {
    ...v5AxisCommon(),
    radius: '75%',
    axisName: { color: '#bbb' },
    axisLine: { lineStyle: { color: '#bbb' } },
  },

  timeline: {
    padding: 5,
    borderColor: '#ccc',
    lineStyle: {
      color: '#DAE1F5',
    },
    label: {
      color: '#A4B1D7',
    },
    itemStyle: {
      color: '#A4B1D7',
      borderWidth: 1,
    },
    checkpointStyle: {
      color: '#316bf3',
      borderColor: '#fff',
      borderWidth: 2,
      shadowBlur: 2,
      shadowOffsetX: 1,
      shadowOffsetY: 1,
      shadowColor: 'rgba(0, 0, 0, 0.3)',
    },
    controlStyle: {
      playIcon:
        'path://M31.6,53C17.5,53,6,41.5,6,27.4S17.5,1.8,31.6,1.8C45.7,1.8,57.2,13.3,57.2,27.4S45.7,53,31.6,53z M31.6,3.3 C18.4,3.3,7.5,14.1,7.5,27.4c0,13.3,10.8,24.1,24.1,24.1C44.9,51.5,55.7,40.7,55.7,27.4C55.7,14.1,44.9,3.3,31.6,3.3z M24.9,21.3 c0-2.2,1.6-3.1,3.5-2l10.5,6.1c1.899,1.1,1.899,2.9,0,4l-10.5,6.1c-1.9,1.1-3.5,0.2-3.5-2V21.3z',
      stopIcon:
        'path://M30.9,53.2C16.8,53.2,5.3,41.7,5.3,27.6S16.8,2,30.9,2C45,2,56.4,13.5,56.4,27.6S45,53.2,30.9,53.2z M30.9,3.5C17.6,3.5,6.8,14.4,6.8,27.6c0,13.3,10.8,24.1,24.101,24.1C44.2,51.7,55,40.9,55,27.6C54.9,14.4,44.1,3.5,30.9,3.5z M36.9,35.8c0,0.601-0.4,1-0.9,1h-1.3c-0.5,0-0.9-0.399-0.9-1V19.5c0-0.6,0.4-1,0.9-1H36c0.5,0,0.9,0.4,0.9,1V35.8z M27.8,35.8 c0,0.601-0.4,1-0.9,1h-1.3c-0.5,0-0.9-0.399-0.9-1V19.5c0-0.6,0.4-1,0.9-1H27c0.5,0,0.9,0.4,0.9,1L27.8,35.8L27.8,35.8z',
      nextIcon:
        'M2,18.5A1.52,1.52,0,0,1,.92,18a1.49,1.49,0,0,1,0-2.12L7.81,9.36,1,3.11A1.5,1.5,0,1,1,3,.89l8,7.34a1.48,1.48,0,0,1,.49,1.09,1.51,1.51,0,0,1-.46,1.1L3,18.08A1.5,1.5,0,0,1,2,18.5Z',
      prevIcon:
        'M10,.5A1.52,1.52,0,0,1,11.08,1a1.49,1.49,0,0,1,0,2.12L4.19,9.64,11,15.89a1.5,1.5,0,1,1-2,2.22L1,10.77A1.48,1.48,0,0,1,.5,9.68,1.51,1.51,0,0,1,1,8.58L9,.92A1.5,1.5,0,0,1,10,.5Z',
      color: '#A4B1D7',
      borderColor: '#A4B1D7',
      borderWidth: 1,
    },
    emphasis: {
      label: {
        color: '#6f778d',
      },
      itemStyle: {
        color: '#316BF3',
      },
      controlStyle: {
        color: '#316BF3',
        borderColor: '#316BF3',
        borderWidth: 2,
      },
    },
    progress: {
      lineStyle: {
        color: '#316BF3',
      },
      itemStyle: {
        color: '#316BF3',
      },
      label: {
        color: '#6f778d',
      },
    },
  },

  title: {
    left: 0,
    top: 0,
    backgroundColor: 'rgba(0,0,0,0)',
    borderColor: '#ccc',
    textStyle: {
      color: '#464646',
    },
    subtextStyle: {
      color: '#6E7079',
    },
  },

  toolbox: {
    borderColor: '#ccc',
    padding: 5,
    itemGap: 8,
    iconStyle: {
      borderColor: '#666',
    },
    emphasis: {
      iconStyle: {
        borderColor: '#3E98C5',
      },
    },
  },

  tooltip: {
    axisPointer: {
      crossStyle: {
        color: '#999',
      },
    },
    textStyle: {
      color: '#666',
    },
    backgroundColor: '#fff',
    borderWidth: 1,
    defaultBorderColor: '#fff',
  },

  visualMap: {
    color: [V5_GRADIENT[2], V5_GRADIENT[1], V5_GRADIENT[0]],
    inactive: ['rgba(0,0,0,0)'],
    indicatorStyle: {
      shadowColor: 'rgba(0,0,0,0.2)',
    },
    backgroundColor: 'rgba(0,0,0,0)',
    borderColor: '#ccc',
    contentColor: '#5793f3',
    inactiveColor: '#aaa',
    padding: 5,
    textStyle: {
      color: '#333',
    },
  },
};

const V5_COMPAT_AXIS = { nameMoveOverlap: false, containShape: false };

export const NETMON_CHART_THEME = 'netmon';

echarts.use([LegacyGridContainLabel]);
echarts.registerTheme(NETMON_CHART_THEME, {
  ...V5_THEME,
  grid: { ...V5_THEME.grid, outerBoundsMode: 'none' },
  timeAxis: { ...V5_THEME.timeAxis, ...V5_COMPAT_AXIS },
  logAxis: { ...V5_THEME.logAxis, ...V5_COMPAT_AXIS },
  valueAxis: { ...V5_THEME.valueAxis, ...V5_COMPAT_AXIS },
  categoryAxis: { ...V5_THEME.categoryAxis, nameMoveOverlap: false },
});

/** `echarts.init` with the NetworkMonitor theme (canvas renderer). */
export function initChart(el: HTMLElement): echarts.ECharts {
  return echarts.init(el, NETMON_CHART_THEME, { renderer: 'canvas' });
}
