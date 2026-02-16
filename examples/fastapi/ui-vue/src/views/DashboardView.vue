<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import {
  NGrid,
  NGi,
  NCard,
  NButton,
  NAlert,
  NSpin,
  NTooltip,
  useMessage
} from 'naive-ui'
import { PhMapPin, PhPen, PhArticle, PhArrowRight, PhHeartbeat, PhClock, PhDatabase, PhScroll } from '@phosphor-icons/vue'
import { fetchApi } from '@/composables/useApi'

interface HealthStatus {
  healthy: boolean
  latency_ms: number
  wal_mode: boolean
  journal_mode: string
}

interface DbStats {
  size_bytes: number
  table_count: number
  index_count: number
}

const { t } = useI18n()
const router = useRouter()
const message = useMessage()

const health = ref<HealthStatus | null>(null)
const stats = ref<DbStats | null>(null)
const loading = ref(true)
const error = ref<string | null>(null)
const actionLoading = ref<string | null>(null)

const isHealthy = computed(() => health.value?.healthy ?? false)
const latencyMs = computed(() => health.value?.latency_ms?.toFixed(1) ?? '—')
const journalMode = computed(() => health.value?.journal_mode?.toUpperCase() ?? 'N/A')
const isWalMode = computed(() => health.value?.wal_mode ?? false)

async function loadDashboard() {
  loading.value = true
  error.value = null
  try {
    const [healthResult, statsResult] = await Promise.all([
      fetchApi<HealthStatus>('/api/db/health'),
      fetchApi<DbStats>('/api/db/stats')
    ])
    health.value = healthResult
    stats.value = statsResult
  } catch (e) {
    error.value = e instanceof Error ? e.message : t('common.error')
  } finally {
    loading.value = false
  }
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
  return (bytes / (1024 * 1024)).toFixed(2) + ' MB'
}

async function runVacuum() {
  actionLoading.value = 'vacuum'
  try {
    await fetchApi('/api/db/vacuum', { method: 'POST' })
    message.success(t('dashboard.vacuumComplete'))
    loadDashboard()
  } catch (e) {
    message.error(e instanceof Error ? e.message : t('common.error'))
  } finally {
    actionLoading.value = null
  }
}

async function runCheckpoint() {
  actionLoading.value = 'checkpoint'
  try {
    await fetchApi('/api/db/checkpoint', { method: 'POST' })
    message.success(t('dashboard.checkpointComplete'))
  } catch (e) {
    message.error(e instanceof Error ? e.message : t('common.error'))
  } finally {
    actionLoading.value = null
  }
}

onMounted(loadDashboard)
</script>

<template>
  <div class="dashboard">
    <NSpin :show="loading">
      <NAlert v-if="error" type="error" :title="error" style="margin-bottom: 16px" closable />

      <!-- Row 1: Health, Latency, DB Size, WAL -->
      <NGrid :cols="4" :x-gap="12" :y-gap="12" style="margin-bottom: 12px" layout-shift-disabled>
        <NGi>
          <NTooltip>
            <template #trigger>
              <div class="stat-card" :class="isHealthy ? 'stat-healthy' : 'stat-error'">
                <PhHeartbeat :size="32" weight="duotone" class="stat-icon" />
                <div class="stat-value">{{ isHealthy ? t('dashboard.healthy') : t('dashboard.unhealthy') }}</div>
                <div class="stat-label">{{ t('dashboard.healthStatus') }}</div>
              </div>
            </template>
            {{ t('dashboard.healthStatusTooltip') }}
          </NTooltip>
        </NGi>
        <NGi>
          <NTooltip>
            <template #trigger>
              <div class="stat-card stat-info">
                <PhClock :size="32" weight="duotone" class="stat-icon" />
                <div class="stat-value">{{ latencyMs }}<span class="stat-unit">ms</span></div>
                <div class="stat-label">{{ t('dashboard.latency') }}</div>
              </div>
            </template>
            {{ t('dashboard.latencyTooltip') }}
          </NTooltip>
        </NGi>
        <NGi>
          <NTooltip>
            <template #trigger>
              <div class="stat-card stat-default">
                <PhDatabase :size="32" weight="duotone" class="stat-icon" />
                <div class="stat-value">{{ formatBytes(stats?.size_bytes ?? 0) }}</div>
                <div class="stat-label">{{ t('dashboard.dbSize') }}</div>
              </div>
            </template>
            {{ t('dashboard.dbSizeTooltip') }}
          </NTooltip>
        </NGi>
        <NGi>
          <NTooltip>
            <template #trigger>
              <div class="stat-card" :class="isWalMode ? 'stat-success' : 'stat-warning'">
                <PhScroll :size="32" weight="duotone" class="stat-icon" />
                <div class="stat-value">{{ journalMode }}</div>
                <div class="stat-label">{{ t('dashboard.walMode') }}</div>
              </div>
            </template>
            {{ t('dashboard.walModeTooltip') }}
          </NTooltip>
        </NGi>
      </NGrid>

      <!-- Row 2: DB Details, Maintenance -->
      <NGrid :cols="2" :x-gap="12" :y-gap="12" style="margin-bottom: 12px" layout-shift-disabled>
        <NGi>
          <NCard size="small" :title="t('dashboard.dbDetails')" class="full-height-card">
            <NTooltip>
              <template #trigger>
                <div class="db-details db-details-clickable" @click="router.push('/schema')">
                  <div class="db-detail-item">
                    <span class="db-detail-value">{{ stats?.table_count ?? 0 }}</span>
                    <span class="db-detail-label">{{ t('dashboard.tableCount') }}</span>
                  </div>
                  <div class="db-detail-divider"></div>
                  <div class="db-detail-item">
                    <span class="db-detail-value">{{ stats?.index_count ?? 0 }}</span>
                    <span class="db-detail-label">{{ t('dashboard.indexCount') }}</span>
                  </div>
                </div>
              </template>
              {{ t('dashboard.viewSchemaTooltip') }}
            </NTooltip>
          </NCard>
        </NGi>
        <NGi>
          <NCard size="small" :title="t('dashboard.maintenance')" class="full-height-card">
            <div class="maintenance-buttons">
              <NTooltip>
                <template #trigger>
                  <NButton
                    type="warning"
                    @click="runVacuum"
                    :loading="actionLoading === 'vacuum'"
                    :disabled="actionLoading !== null"
                  >
                    {{ t('dashboard.vacuum') }}
                  </NButton>
                </template>
                {{ t('dashboard.vacuumTooltip') }}
              </NTooltip>
              <NTooltip>
                <template #trigger>
                  <NButton
                    type="info"
                    @click="runCheckpoint"
                    :loading="actionLoading === 'checkpoint'"
                    :disabled="actionLoading !== null"
                  >
                    {{ t('dashboard.checkpoint') }}
                  </NButton>
                </template>
                {{ t('dashboard.checkpointTooltip') }}
              </NTooltip>
              <NButton @click="loadDashboard" :loading="loading">
                {{ t('common.refresh') }}
              </NButton>
            </div>
          </NCard>
        </NGi>
      </NGrid>

      <!-- Row 3: Feature links -->
      <NGrid :cols="3" :x-gap="12" :y-gap="12" layout-shift-disabled>
        <NGi>
          <div class="feature-card feature-locations" @click="router.push('/locations')">
            <PhMapPin :size="24" weight="duotone" class="feature-icon" />
            <div class="feature-text">
              <span class="feature-title">{{ t('dashboard.features.locations.title') }}</span>
              <span class="feature-desc">{{ t('dashboard.features.locations.desc') }}</span>
            </div>
            <PhArrowRight :size="16" weight="bold" class="feature-arrow" />
          </div>
        </NGi>
        <NGi>
          <div class="feature-card feature-writers" @click="router.push('/writers')">
            <PhPen :size="24" weight="duotone" class="feature-icon" />
            <div class="feature-text">
              <span class="feature-title">{{ t('dashboard.features.writers.title') }}</span>
              <span class="feature-desc">{{ t('dashboard.features.writers.desc') }}</span>
            </div>
            <PhArrowRight :size="16" weight="bold" class="feature-arrow" />
          </div>
        </NGi>
        <NGi>
          <div class="feature-card feature-articles" @click="router.push('/articles')">
            <PhArticle :size="24" weight="duotone" class="feature-icon" />
            <div class="feature-text">
              <span class="feature-title">{{ t('dashboard.features.articles.title') }}</span>
              <span class="feature-desc">{{ t('dashboard.features.articles.desc') }}</span>
            </div>
            <PhArrowRight :size="16" weight="bold" class="feature-arrow" />
          </div>
        </NGi>
      </NGrid>
    </NSpin>
  </div>
</template>

<style scoped>
.dashboard {
  max-width: 900px;
  margin: 0 auto;
}

/* Stat Cards */
.stat-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 20px 16px;
  border-radius: 8px;
  min-height: 120px;
  text-align: center;
}

.stat-icon {
  margin-bottom: 8px;
  opacity: 0.9;
}

.stat-value {
  font-size: 28px;
  font-weight: 700;
  line-height: 1.2;
}

.stat-unit {
  font-size: 14px;
  font-weight: 400;
  margin-left: 2px;
  opacity: 0.8;
}

.stat-label {
  font-size: 12px;
  margin-top: 4px;
  opacity: 0.8;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

/* Stat card colors */
.stat-healthy {
  background: linear-gradient(135deg, #10b981 0%, #059669 100%);
  color: white;
}

.stat-error {
  background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%);
  color: white;
}

.stat-info {
  background: linear-gradient(135deg, #3b82f6 0%, #2563eb 100%);
  color: white;
}

.stat-success {
  background: linear-gradient(135deg, #22c55e 0%, #16a34a 100%);
  color: white;
}

.stat-warning {
  background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%);
  color: white;
}

.stat-default {
  background: linear-gradient(135deg, #6366f1 0%, #4f46e5 100%);
  color: white;
}

/* Cards that need to match stat card height */
.full-height-card {
  height: 100%;
}

/* DB Details */
.db-details {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 24px;
  padding: 8px 0;
}

.db-detail-item {
  display: flex;
  flex-direction: column;
  align-items: center;
}

.db-detail-value {
  font-size: 28px;
  font-weight: 700;
  color: var(--n-text-color-1);
}

.db-detail-label {
  font-size: 11px;
  color: var(--n-text-color-3);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.db-detail-divider {
  width: 1px;
  height: 40px;
  background: var(--n-border-color);
}

.db-details-clickable {
  cursor: pointer;
  border-radius: 6px;
  padding: 12px;
  margin: -12px;
  transition: background-color 0.15s ease;
}

.db-details-clickable:hover {
  background: var(--n-color-embedded);
}

.db-details-clickable:hover .db-detail-value {
  color: var(--n-primary-color);
}

/* Maintenance buttons */
.maintenance-buttons {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
}

.maintenance-buttons .n-button {
  height: 44px;
  font-weight: 500;
}

/* Feature Cards */
.feature-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 16px;
  border-radius: 8px;
  cursor: pointer;
  transition: transform 0.15s ease, box-shadow 0.15s ease;
  height: 56px;
}

.feature-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
}

.feature-icon {
  flex-shrink: 0;
}

.feature-text {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.feature-title {
  font-weight: 600;
  font-size: 14px;
}

.feature-desc {
  font-size: 11px;
  opacity: 0.85;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.feature-arrow {
  flex-shrink: 0;
  opacity: 0.7;
}

.feature-locations {
  background: linear-gradient(135deg, #8b5cf6 0%, #7c3aed 100%);
  color: white;
}

.feature-writers {
  background: linear-gradient(135deg, #06b6d4 0%, #0891b2 100%);
  color: white;
}

.feature-articles {
  background: linear-gradient(135deg, #f97316 0%, #ea580c 100%);
  color: white;
}
</style>
