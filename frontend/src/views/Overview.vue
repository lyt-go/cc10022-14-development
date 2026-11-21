<template>
  <section class="page">
    <header class="page-head">
      <div>
        <h2>运营概览</h2>
        <p class="page-desc">汇总各业务模块的关键指标，先看总量再看异常。</p>
      </div>
    </header>
    <div class="stat-row">
      <article v-for="card in cards" :key="card.label" class="stat-card">
        <span class="stat-label">{{ card.label }}</span>
        <strong class="stat-value">{{ card.value }}</strong>
      </article>
    </div>
    <table class="data-table">
      <thead>
        <tr><th>业务模块</th><th>今日新增</th><th>待处理</th><th>异常量</th></tr>
      </thead>
      <tbody>
        <tr v-for="row in moduleRows" :key="row.name">
          <td>{{ row.name }}</td>
          <td>{{ row.created }}</td>
          <td>{{ row.pending }}</td>
          <td>{{ row.abnormal }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { fetchJson } from '@/api/client'

type Overview = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}

const cards = ref<Overview['cards']>([])
const moduleRows = ref<Overview['modules']>([])

onMounted(async () => {
  try {
    const payload = await fetchJson<Overview>('/api/overview')
    cards.value = payload.cards
    moduleRows.value = payload.modules
  } catch {
    cards.value = [{"label": "业务模块", "value": 0}, {"label": "今日新增", "value": 0}]
    moduleRows.value = [{"name": "来源系统注册", "created": 0, "pending": 0, "abnormal": 0}, {"name": "实体模型定义", "created": 0, "pending": 0, "abnormal": 0}, {"name": "匹配规则引擎", "created": 0, "pending": 0, "abnormal": 0}, {"name": "疑似重复簇", "created": 0, "pending": 0, "abnormal": 0}, {"name": "合并追溯日志", "created": 0, "pending": 0, "abnormal": 0}, {"name": "黄金记录管理", "created": 0, "pending": 0, "abnormal": 0}, {"name": "字段标准化清洗", "created": 0, "pending": 0, "abnormal": 0}, {"name": "优胜策略配置", "created": 0, "pending": 0, "abnormal": 0}, {"name": "数据入库规则", "created": 0, "pending": 0, "abnormal": 0}, {"name": "批次导入管理", "created": 0, "pending": 0, "abnormal": 0}, {"name": "记录级血缘", "created": 0, "pending": 0, "abnormal": 0}, {"name": "字段归属管理", "created": 0, "pending": 0, "abnormal": 0}, {"name": "字段归属分发明细", "created": 0, "pending": 0, "abnormal": 0}, {"name": "主数据分发同步", "created": 0, "pending": 0, "abnormal": 0}, {"name": "黄金记录版本", "created": 0, "pending": 0, "abnormal": 0}, {"name": "自动匹配任务", "created": 0, "pending": 0, "abnormal": 0}, {"name": "主数据质量统计", "created": 0, "pending": 0, "abnormal": 0}, {"name": "参考数据管理", "created": 0, "pending": 0, "abnormal": 0}, {"name": "主数据查询接口", "created": 0, "pending": 0, "abnormal": 0}, {"name": "主数据治理看板", "created": 0, "pending": 0, "abnormal": 0}]
  }
})
</script>
