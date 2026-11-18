<template>
  <section class="page" data-module="stats">
    <header class="page-head">
      <div>
        <h2>表统计信息管理</h2>
        <p class="page-desc">维护表统计的行数、文件数与容量分析，为表统计信息提供可审计的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记统计报告</button>
        <button class="btn" type="button" @click="exportRows">导出表统计信息清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in statCards" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>报告编号 / 表名称 / 采集批次</span>
        <input v-model="keyword" placeholder="按报告编号、表名称或采集批次检索" />
      </label>
      <label class="filter-item">
        <span>报告状态</span>
        <input v-model="statusFilter" placeholder="草稿、已采集、已过期、已清理" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="selectRow(row)">查看报告</button>
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无表统计信息数据，可先登记统计报告</td>
        </tr>
      </tbody>
    </table>

    <section v-if="selected" class="report-detail">
      <header class="report-detail-head">
        <h3>完整统计报告：{{ selected['报告编号'] }}（{{ selected['表名称'] }}）</h3>
        <button class="btn ghost" type="button" @click="selected = null">收起</button>
      </header>
      <div class="report-detail-grid">
        <div><span>行总数</span><strong>{{ selected['行总数'] ?? '—' }}</strong></div>
        <div><span>文件总数</span><strong>{{ selected['文件总数'] ?? '—' }}</strong></div>
        <div><span>总容量</span><strong>{{ selected['总容量'] ?? '—' }}</strong></div>
        <div><span>采集批次</span><strong>{{ selected['采集批次'] ?? '—' }}</strong></div>
        <div><span>重采进度</span><strong>{{ selected['重采进度'] ?? '—' }}</strong></div>
        <div><span>批次状态</span><strong>{{ selected['status'] ?? '—' }}</strong></div>
        <div><span>统计时间</span><strong>{{ selected['统计时间'] ?? '—' }}</strong></div>
        <div><span>报告状态</span><strong>{{ selected['报告状态'] ?? '—' }}</strong></div>
      </div>
      <p class="report-columns">列统计：{{ selected['列统计'] ?? '—' }}</p>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条表统计信息记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/stats'
const columns = ["报告编号", "表名称", "行总数", "文件总数", "总容量", "采集批次", "重采进度", "统计时间", "报告状态"]
const actions = ["采集统计", "重新采集", "标记过期", "清理报告"]
const statuses = ["草稿", "已采集", "已过期", "已清理"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const keyword = ref('')
const statusFilter = ref('')
const selected = ref<Row | null>(null)

const statCards = computed(() => [
  { label: '统计报告总数', value: total.value },
  { label: '已采集报告', value: rows.value.filter((row) => row.status === '已采集').length },
  { label: '已过期报告', value: rows.value.filter((row) => row.status === '已过期').length },
])

function resetFilters() {
  keyword.value = ''
  statusFilter.value = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '统计报告登记入口尚未接入审批流'
}

function selectRow(row: Row) {
  selected.value = row
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message ?? payload.detail ?? '表统计信息动作未生效，请稍后重试')
    }
    errorMessage.value = payload.message ?? ''
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '表统计信息操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams()
  if (keyword.value) query.set('keyword', keyword.value)
  if (statusFilter.value) query.set('status', statusFilter.value)
  try {
    const response = await request(`${ENDPOINT}?${query.toString()}`)
    if (!response.ok) {
      throw new Error('统计报告列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    if (selected.value) {
      selected.value = rows.value.find((row) => row.id === selected.value?.id) ?? null
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '表统计信息列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.report-detail {
  margin-top: 12px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 16px;
}
.report-detail-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.report-detail-head h3 {
  margin: 0;
  font-size: 14px;
}
.report-detail-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 8px 16px;
  margin-top: 10px;
}
.report-detail-grid span {
  display: block;
  color: var(--muted);
  font-size: 12px;
}
.report-detail-grid strong {
  font-size: 15px;
}
.report-columns {
  margin: 10px 0 0;
  font-size: 13px;
  color: var(--muted);
}
</style>
