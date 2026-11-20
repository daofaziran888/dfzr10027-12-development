<template>
  <section class="page" data-module="experiment">
    <header class="page-head">
      <div>
        <h2>实验管理管理</h2>
        <p class="page-desc">维护AB实验的假设设计、分组方案、指标选取和显著性检验流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记AB实验</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出实验管理清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无实验管理数据，可先登记AB实验</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记AB实验</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <p v-if="createHint" class="hint-text">{{ createHint }}</p>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <section class="readiness-section" data-testid="readiness-panel">
      <header class="section-head">
        <div>
          <h3>实验就绪与试投排名</h3>
          <p class="page-desc">两组各自达到最小样本量才发布显著性；不同观测期的迟到批次不并入本期。</p>
        </div>
        <div class="page-actions">
          <button class="btn" data-testid="open-observation" type="button" @click="openObservation">登记采集批次</button>
          <button class="btn primary" data-testid="sync-mart" type="button" :disabled="syncing" @click="syncStat">
            {{ syncing ? '同步中…' : '校验并同步集市' }}
          </button>
        </div>
      </header>

      <div class="stat-row">
        <article v-for="card in readinessStats" :key="card.label" class="stat-card">
          <span class="stat-label">{{ card.label }}</span>
          <strong class="stat-value">{{ card.value }}</strong>
        </article>
      </div>

      <table class="data-table">
        <thead>
          <tr>
            <th>实验编号</th><th>观测期</th><th>对照组样本</th><th>实验组样本</th>
            <th>缺数组别</th><th>就绪状态</th><th>复盘状态</th><th>提升量</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="item in readinessRows"
            :key="`ready-${item.experiment_code}`"
            :data-testid="`readiness-${item.experiment_code}`"
            :data-ready="item.ready ? 'true' : 'false'"
          >
            <td>{{ item.experiment_code }}</td>
            <td>{{ item.observation_period ?? '—' }}</td>
            <td>{{ groupCell(item, 'control') }}</td>
            <td>{{ groupCell(item, 'treatment') }}</td>
            <td>{{ item.缺数组别名称?.join('、') || '—' }}</td>
            <td><span :class="['state-pill', item.ready ? 'ok' : 'warn']">{{ item.readiness_status }}</span></td>
            <td>
              <span :class="['state-pill', reviewClass(item.review_status)]" :data-review="item.review_status">
                {{ item.review_status }}
              </span>
            </td>
            <td>{{ item.lift === null || item.lift === undefined ? '—' : `${(item.lift * 100).toFixed(2)}%` }}</td>
            <td class="row-actions">
              <button class="link" type="button" :data-testid="`review-${item.experiment_code}`" @click="openReview(item.experiment_code)">查看复盘</button>
              <button class="link" type="button" :data-testid="`threshold-${item.experiment_code}`" @click="openThreshold(item)">设置门槛</button>
            </td>
          </tr>
          <tr v-if="!readinessRows.length">
            <td colspan="9" class="empty-state">暂无实验就绪数据</td>
          </tr>
        </tbody>
      </table>

      <div class="ranking-summary" data-testid="ranking-summary">
        <strong>试投名单：</strong>
        <span v-if="ranking?.items?.length">
          {{ ranking.items.map((item) => `${item.排名}. ${item.实验编号}（提升量 ${(Number(item.提升量) * 100).toFixed(2)}%）`).join('；') }}
        </span>
        <span v-else>本期没有可安排试投的实验</span>
        <span class="muted"> 覆盖 {{ ranking?.coverage.ranked ?? 0 }}/{{ ranking?.coverage.total_experiments ?? 0 }}，未参加排名 {{ ranking?.unranked.length ?? 0 }} 个。</span>
      </div>
      <table v-if="ranking?.unranked?.length" class="data-table" data-testid="unranked-table">
        <thead><tr><th>未参加排名实验</th><th>观测期</th><th>原因</th><th>缺数组别</th></tr></thead>
        <tbody>
          <tr v-for="item in ranking.unranked" :key="`unranked-${item.实验编号}`">
            <td>{{ item.实验编号 }}</td>
            <td>{{ item.观测期 ?? '—' }}</td>
            <td>{{ item.原因 }}</td>
            <td>{{ item.缺数组别名称?.join('、') || item.缺数组别?.join('、') || '—' }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <div v-if="review" class="modal-backdrop" data-testid="review-modal">
      <div class="modal">
        <h3>{{ review.实验编号 }} · 复盘记录</h3>
        <dl class="review-detail">
          <dt>观测期</dt><dd>{{ review.观测期 ?? '—' }}</dd>
          <dt>复盘状态</dt><dd><span :class="['state-pill', reviewClass(review.复盘状态)]">{{ review.复盘状态 }}</span></dd>
          <dt>结论</dt><dd>{{ review.结论 }}</dd>
          <dt>说明</dt><dd>{{ review.说明 }}</dd>
          <dt>对照组</dt><dd>{{ review.样本.control.sample_size }} 人 / 门槛 {{ review.最小样本 }}</dd>
          <dt>实验组</dt><dd>{{ review.样本.treatment.sample_size }} 人 / 门槛 {{ review.最小样本 }}</dd>
          <dt>缺数组别</dt><dd>{{ review.缺数组别名称?.join('、') || '无' }}</dd>
          <dt>提升量</dt><dd>{{ review.提升量 === null || review.提升量 === undefined ? '未发布' : `${(review.提升量 * 100).toFixed(2)}%` }}</dd>
          <dt>显著性</dt><dd>{{ review.统计 ? (review.统计.显著 ? '显著' : '不显著') : '未检验' }}</dd>
          <dt>跨期数据</dt><dd>{{ review.跨期数据.批次.length ? review.跨期数据.批次.map((batch) => batch.observation_period).join('、') + '（未并入本期）' : '无' }}</dd>
        </dl>
        <div class="page-actions">
          <button class="btn" type="button" data-testid="close-review" @click="review = null">关闭</button>
        </div>
      </div>
    </div>

    <div v-if="showObservation" class="modal-backdrop" data-testid="observation-modal">
      <form class="modal" @submit.prevent="submitObservation">
        <h3>登记采集批次</h3>
        <label v-for="field in observationFields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="observationValues[field]" :data-testid="`observation-${field}`" required />
        </label>
        <p class="hint-text">分组支持 control/treatment 或 对照组/实验组；同一批次号重复提交按覆盖处理，不重复累加。</p>
        <div class="page-actions">
          <button class="btn" type="button" @click="showObservation = false">取消</button>
          <button class="btn primary" data-testid="submit-observation" type="submit" :disabled="savingObservation">
            {{ savingObservation ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="thresholdTarget" class="modal-backdrop" data-testid="threshold-modal">
      <form class="modal" @submit.prevent="submitThreshold">
        <h3>{{ thresholdTarget.experiment_code }} · 设置按组最小样本</h3>
        <label class="create-field">
          <span>每组最小样本量</span>
          <input v-model="thresholdValue" data-testid="threshold-input" type="number" min="1" required />
        </label>
        <p class="hint-text">对照组与实验组各自达到该值才发布显著性结果；修改后需重新校验同步。</p>
        <div class="page-actions">
          <button class="btn" type="button" @click="thresholdTarget = null">取消</button>
          <button class="btn primary" data-testid="submit-threshold" type="submit">确认</button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条实验管理记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

type GroupDetail = {
  label: string
  sample_size: number
  conversions: number
  rate: number | null
  threshold: number
  threshold_met: boolean
  gap: number
  present: boolean
}

type Readiness = {
  experiment_code: string
  experiment_name: string
  observation_period: string | null
  readiness_status: string
  review_status: string
  ready: boolean
  lift: number | null
  缺数组别: string[]
  缺数组别名称: string[]
  groups: Record<string, GroupDetail>
  conclusion: string
}

type Ranking = {
  items: { 实验编号: string; 排名: number; 提升量: number | null; 观测期: string }[]
  unranked: { 实验编号: string; 原因: string; 观测期: string | null; 缺数组别?: string[]; 缺数组别名称?: string[] }[]
  coverage: { total_experiments: number; ranked: number; unranked: number; coverage_rate: number }
  message: string
}

type Review = {
  实验编号: string
  观测期: string | null
  复盘状态: string
  结论: string
  说明: string
  最小样本: number
  缺数组别名称: string[]
  提升量: number | null
  样本: Record<string, { sample_size: number }>
  统计: { 显著: boolean } | null
  跨期数据: { 批次: { observation_period: string }[] }
}

const ENDPOINT = '/api/experiment'
const READINESS_ENDPOINT = '/api/experiment-readiness'
const columns = ["实验编号", "实验假设", "分组方案", "对照组比例", "核心指标", "最小样本", "显著性水平", "实验周期"]
const actions = ["启动实验", "终止实验", "归档结果"]
const statuses = ["设计阶段", "运行中", "已完成", "已终止"]
const metricNames = ["实验总数", "运行中实验", "已完成实验"]
const observationFields = ["实验编号", "观测期", "分组", "样本量", "转化人数", "批次号"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const readinessRows = ref<Readiness[]>([])
const ranking = ref<Ranking | null>(null)
const review = ref<Review | null>(null)
const showObservation = ref(false)
const savingObservation = ref(false)
const observationValues = ref<Record<string, string>>({})
const syncing = ref(false)
const thresholdTarget = ref<Readiness | null>(null)
const thresholdValue = ref('')
const createHint = computed(() =>
  createValues.value['最小样本']
    ? `按组门槛：对照组与实验组各自需达到 ${createValues.value['最小样本']} 人`
    : '最小样本按组分计：两组各自达标后才发布显著性结果',
)
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])
const readinessStats = computed(() => [
  { label: '已就绪实验', value: readinessRows.value.filter((item) => item.ready).length },
  { label: '样本未到齐', value: readinessRows.value.filter((item) => !item.ready).length },
  { label: '可安排试投', value: ranking.value?.items.length ?? 0 },
  { label: '未参加排名', value: ranking.value?.unranked.length ?? 0 },
])

function groupCell(item: Readiness, group: string) {
  const detail = item.groups?.[group]
  if (!detail) return '—'
  if (!detail.present) return `缺数据（0/${detail.threshold}）`
  return `${detail.sample_size}/${detail.threshold}${detail.threshold_met ? '' : `（差 ${detail.gap}）`}`
}

function reviewClass(status: string) {
  if (status === '已检验-显著') return 'ok'
  if (status === '已检验-不显著') return 'neutral'
  if (status === '样本未到齐') return 'warn'
  return 'muted'
}

async function reloadReadiness() {
  try {
    const response = await request(`${READINESS_ENDPOINT}/readiness`)
    if (!response.ok) throw new Error('实验就绪状态读取失败')
    const payload = await response.json()
    readinessRows.value = payload.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '实验就绪状态读取失败'
  }
  try {
    const response = await request(`${READINESS_ENDPOINT}/ranking`)
    if (!response.ok) throw new Error('试投排名读取失败')
    ranking.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '试投排名读取失败'
  }
}

async function syncStat() {
  syncing.value = true
  errorMessage.value = ''
  try {
    const response = await request(`${READINESS_ENDPOINT}/sync`, { method: 'POST' })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '同步失败')
    await reloadReadiness()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '同步失败'
  } finally {
    syncing.value = false
  }
}

async function openReview(code: string) {
  errorMessage.value = ''
  try {
    const response = await request(`${READINESS_ENDPOINT}/review/${code}`)
    if (!response.ok) throw new Error('复盘记录读取失败')
    review.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '复盘记录读取失败'
  }
}

function openObservation() {
  observationValues.value = Object.fromEntries(observationFields.map((field) => [field, '']))
  errorMessage.value = ''
  showObservation.value = true
}

async function submitObservation() {
  savingObservation.value = true
  errorMessage.value = ''
  try {
    const response = await request(`${READINESS_ENDPOINT}/observations`, {
      method: 'POST', body: JSON.stringify(observationValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '采集批次登记失败')
    showObservation.value = false
    await reloadReadiness()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集批次登记失败'
  } finally {
    savingObservation.value = false
  }
}

function openThreshold(item: Readiness) {
  thresholdTarget.value = item
  thresholdValue.value = String(
    item.groups?.control?.threshold ?? item.groups?.treatment?.threshold ?? 500,
  )
}

async function submitThreshold() {
  if (!thresholdTarget.value) return
  const entry = rows.value.find((row) => row['实验编号'] === thresholdTarget.value?.experiment_code)
  if (!entry) {
    errorMessage.value = '未找到该实验的记录'
    return
  }
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${entry.id}/min-sample`, {
      method: 'POST', body: JSON.stringify({ 最小样本: Number(thresholdValue.value) }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '门槛设置失败')
    thresholdTarget.value = null
    await reload()
    await reloadReadiness()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '门槛设置失败'
  }
}

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'experiment.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('实验管理动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '实验管理操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('AB实验列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '实验管理列表读取失败'
  }
}

onMounted(() => {
  void reload()
  void reloadReadiness()
})
</script>
