<template>
  <section class="page" data-module="quota">
    <header class="page-head">
      <div>
        <h2>湖存储配额管理</h2>
        <p class="page-desc">维护湖存储的空间配额、表数量限制与超限处理，为湖存储配额管理提供可审计的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记配额配置</button>
        <button class="btn" type="button" @click="exportRows">导出湖存储配额清单</button>
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
        <span>配置编号</span>
        <input v-model="keyword" placeholder="按配置编号检索" />
      </label>
      <label class="filter-item">
        <span>配额状态</span>
        <input v-model="statusFilter" placeholder="草稿、已生效、已告警、已超限" />
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
          <td :colspan="columns.length + 1" class="empty-state">暂无湖存储配额数据，可先登记配额配置</td>
        </tr>
      </tbody>
    </table>

    <section class="alert-panel">
      <header class="alert-panel-head">
        <h3>容量告警清单</h3>
        <span class="alert-panel-desc">与统计列表使用同一份行数、文件数、容量和采集批次</span>
      </header>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in alertColumns" :key="column">{{ column }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="alert in alerts" :key="String(alert.id)">
            <td v-for="column in alertColumns" :key="column">{{ alert[column] ?? '—' }}</td>
          </tr>
          <tr v-if="!alerts.length">
            <td :colspan="alertColumns.length" class="empty-state">暂无容量告警记录，可对配额配置执行「核对统计报告」</td>
          </tr>
        </tbody>
      </table>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条湖存储配额记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/quota'
const columns = ["配置编号", "存储空间", "表数量上限", "文件数上限", "告警比例", "超限策略", "有效期至", "配额状态"]
const actions = ["生效配额", "调整配额", "取消配额", "核对统计报告"]
const statuses = ["草稿", "已生效", "已告警", "已超限"]
const alertColumns = ["告警编号", "表名称", "行总数", "文件总数", "总容量", "采集批次", "核对结论", "核对状态", "来源模块", "更新时间"]

const rows = ref<Row[]>([])
const alerts = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const keyword = ref('')
const statusFilter = ref('')

const statCards = computed(() => [
  { label: '配额配置总数', value: total.value },
  { label: '已生效配额', value: rows.value.filter((row) => row.status === '已生效').length },
  { label: '已告警配额', value: rows.value.filter((row) => row.status === '已告警').length },
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
  errorMessage.value = '配额配置登记入口尚未接入审批流'
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
      throw new Error(payload.message ?? payload.detail ?? '湖存储配额动作未生效，请稍后重试')
    }
    errorMessage.value = payload.message ?? ''
    await Promise.all([reload(), reloadAlerts()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '湖存储配额操作失败'
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
      throw new Error('配额配置列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '湖存储配额列表读取失败'
  }
}

async function reloadAlerts() {
  try {
    const response = await request(`${ENDPOINT}/alerts`)
    if (!response.ok) {
      throw new Error('容量告警清单读取失败')
    }
    const payload = await response.json()
    alerts.value = payload.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '容量告警清单读取失败'
  }
}

onMounted(() => {
  void reload()
  void reloadAlerts()
})
</script>

<style scoped>
.alert-panel {
  margin-top: 16px;
}
.alert-panel-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 8px;
}
.alert-panel-head h3 {
  margin: 0;
  font-size: 14px;
}
.alert-panel-desc {
  color: var(--muted);
  font-size: 12px;
}
</style>
