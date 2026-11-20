<template>
  <div>
    <div class="hero">
      <div>
        <h1>总览</h1>
        <p>数字只读 overview_stats 物化表；每次读取都与底表（三腿 / 批表 / 写闸）互证，分叉即标 stale，绝不假绿。</p>
      </div>
      <div class="row" style="margin: 0">
        <button class="warn" @click="recompute">强制重算</button>
        <button @click="load">刷新</button>
      </div>
    </div>

    <div class="panel stale-banner" v-if="d && d.stale">
      <h2>⚠ 总览与底表分叉（stale）</h2>
      <p>
        当前展示的是 {{ d.computed_at }} 的物化快照，与 reconcile_batches / 三腿正式表 / 写闸的实时聚合不一致。
        请点「强制重算」按底表重建物化。
      </p>
      <table v-if="d.divergence && d.divergence.length">
        <thead><tr><th>字段</th><th>物化快照值</th><th>底表实时值</th></tr></thead>
        <tbody>
          <tr v-for="row in d.divergence" :key="row.field">
            <td>{{ row.field }}</td>
            <td>{{ fmt(row.materialized) }}</td>
            <td>{{ fmt(row.live) }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="grid" :class="{ stale: d && d.stale }" v-if="d">
      <div class="stat"><div class="k">到货日</div><div class="v">{{ d.dock_day }}</div></div>
      <div class="stat"><div class="k">预报 / 实收 / 回执</div><div class="v">{{ d.expect_count }} / {{ d.receive_count }} / {{ d.confirm_count }}</div></div>
      <div class="stat"><div class="k">未决差额</div><div class="v">{{ d.pending_variance }}</div></div>
      <div class="stat">
        <div class="k">写闸</div>
        <div class="v"><span class="tag" :class="gateClass">{{ gateText }}</span></div>
      </div>
      <div class="stat"><div class="k">结案批次</div><div class="v">{{ d.sealed_count }}</div></div>
      <div class="stat"><div class="k">开放批次</div><div class="v">{{ d.open_count }}</div></div>
      <div class="stat">
        <div class="k">缺腿提示（当日）</div>
        <div class="v small">缺预报 {{ d.missing_expect }} · 缺实收 {{ d.missing_receive }} · 缺回执 {{ d.missing_confirm }}</div>
      </div>
      <div class="stat"><div class="k">物化时间</div><div class="v small">{{ d.computed_at }}</div></div>
    </div>

    <div class="panel" v-if="d">
      <h2>可写日</h2>
      <p class="days">
        <span v-for="row in d.days" :key="row.dock_day" class="tag day" :class="dayClass(row)">
          {{ row.dock_day }} · {{ row.writable ? '可写' : '已结案' }}
          （预 {{ row.expect_count }} / 实 {{ row.receive_count }} / 回 {{ row.confirm_count }}）
        </span>
      </p>
    </div>

    <div class="panel" v-if="d && d.latest_batch">
      <h2>最近对账批次</h2>
      <p>{{ d.latest_batch.batch_no }} · 对齐 {{ d.latest_batch.matched_count }} · 差额 {{ d.latest_batch.variance_count }} · {{ d.latest_batch.status }}</p>
    </div>
    <p class="msg">{{ err }}</p>
  </div>
</template>
<script setup>
import { ref, computed, onMounted } from 'vue'
import { api } from '../api'
const d = ref(null)
const err = ref('')
const gateClass = computed(() => {
  if (!d.value) return 'bad'
  if (d.value.stale) return 'bad'
  return d.value.writable_today ? 'ok' : 'seal'
})
const gateText = computed(() => {
  if (!d.value) return ''
  if (d.value.stale) return '分叉待重算'
  return d.value.writable_today ? '可写' : '已结案'
})
function fmt(v) {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}
function dayClass(row) {
  if (d.value && d.value.stale) return 'bad'
  return row.writable ? 'ok' : 'seal'
}
async function load() {
  err.value = ''
  try { d.value = await api('/dashboard') } catch (e) { err.value = e.message }
}
async function recompute() {
  err.value = ''
  try { d.value = await api('/dashboard/recompute', { method: 'POST' }) } catch (e) { err.value = e.message }
}
onMounted(load)
</script>
