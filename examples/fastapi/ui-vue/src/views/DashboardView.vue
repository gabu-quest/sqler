<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import {
  NGrid,
  NGridItem,
  NCard,
  NStatistic,
  NSpace,
  NButton,
  NAlert,
  NSpin,
  NTag,
  NProgress,
  NText,
  useMessage
} from 'naive-ui'
import { PhMapPin, PhPen, PhArticle, PhArrowRight } from '@phosphor-icons/vue'
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

interface CacheStats {
  size: number
  max_size: number
  hits: number
  misses: number
}

const { t } = useI18n()
const router = useRouter()
const message = useMessage()

const health = ref<HealthStatus | null>(null)
const stats = ref<DbStats | null>(null)
const cache = ref<CacheStats | null>(null)
const loading = ref(true)
const error = ref<string | null>(null)
const actionLoading = ref<string | null>(null)

async function loadDashboard() {
  loading.value = true
  error.value = null
  try {
    const [healthResult, statsResult, cacheResult] = await Promise.all([
      fetchApi<HealthStatus>('/api/db/health'),
      fetchApi<DbStats>('/api/db/stats'),
      fetchApi<CacheStats>('/api/cache/stats').catch(() => null)
    ])
    health.value = healthResult
    stats.value = statsResult
    cache.value = cacheResult
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

function formatLatency(ms: number | undefined): string {
  if (ms === undefined || ms === null) return '-'
  return ms.toFixed(2)
}

function cacheHitRate(): number {
  if (!cache.value) return 0
  const total = cache.value.hits + cache.value.misses
  return total > 0 ? Math.round((cache.value.hits / total) * 100) : 0
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

      <!-- Health & Stats Row -->
      <NGrid :cols="4" :x-gap="12" :y-gap="12" style="margin-bottom: 16px" responsive="screen" :item-responsive="true">
        <NGridItem :span="24" :md="6">
          <NCard size="small">
            <NStatistic :label="t('dashboard.healthStatus')">
              <template #prefix>
                <NTag :type="health?.healthy ? 'success' : 'error'" size="small">
                  {{ health?.healthy ? t('dashboard.healthy') : t('dashboard.unhealthy') }}
                </NTag>
              </template>
            </NStatistic>
          </NCard>
        </NGridItem>
        <NGridItem :span="24" :md="6">
          <NCard size="small">
            <NStatistic :label="t('dashboard.latency')" :value="formatLatency(health?.latency_ms)" suffix="ms" />
          </NCard>
        </NGridItem>
        <NGridItem :span="24" :md="6">
          <NCard size="small">
            <NStatistic :label="t('dashboard.dbSize')" :value="formatBytes(stats?.size_bytes ?? 0)" />
          </NCard>
        </NGridItem>
        <NGridItem :span="24" :md="6">
          <NCard size="small">
            <NStatistic :label="t('dashboard.walMode')">
              <template #prefix>
                <NTag :type="health?.wal_mode ? 'success' : 'warning'" size="small">
                  {{ health?.journal_mode ?? 'N/A' }}
                </NTag>
              </template>
            </NStatistic>
          </NCard>
        </NGridItem>
      </NGrid>

      <!-- Cache & Actions Row -->
      <NGrid :cols="2" :x-gap="12" :y-gap="12" style="margin-bottom: 16px" responsive="screen" :item-responsive="true">
        <NGridItem :span="24" :md="12">
          <NCard size="small" :title="t('dashboard.cacheTitle')">
            <NSpace vertical>
              <NSpace justify="space-between">
                <NText>{{ t('dashboard.hitRate') }}</NText>
                <NText strong>{{ cacheHitRate() }}%</NText>
              </NSpace>
              <NProgress
                type="line"
                :percentage="cacheHitRate()"
                :indicator-placement="'inside'"
                :height="20"
                :border-radius="4"
              />
              <NSpace size="small">
                <NTag size="small">{{ t('dashboard.hits') }}: {{ cache?.hits ?? 0 }}</NTag>
                <NTag size="small">{{ t('dashboard.misses') }}: {{ cache?.misses ?? 0 }}</NTag>
              </NSpace>
            </NSpace>
          </NCard>
        </NGridItem>
        <NGridItem :span="24" :md="12">
          <NCard size="small" :title="t('dashboard.maintenance')">
            <NSpace size="small">
              <NButton
                size="small"
                @click="runVacuum"
                :loading="actionLoading === 'vacuum'"
                :disabled="actionLoading !== null"
              >
                {{ t('dashboard.vacuum') }}
              </NButton>
              <NButton
                size="small"
                @click="runCheckpoint"
                :loading="actionLoading === 'checkpoint'"
                :disabled="actionLoading !== null"
              >
                {{ t('dashboard.checkpoint') }}
              </NButton>
              <NButton size="small" @click="loadDashboard" :loading="loading">
                {{ t('common.refresh') }}
              </NButton>
            </NSpace>
          </NCard>
        </NGridItem>
      </NGrid>

      <!-- Feature Cards - 3 columns -->
      <NGrid :cols="3" :x-gap="12" :y-gap="12" responsive="screen" :item-responsive="true">
        <NGridItem :span="24" :md="8">
          <NCard size="small" hoverable class="feature-card" @click="router.push('/locations')">
            <NSpace align="center" justify="space-between">
              <NSpace align="center">
                <PhMapPin :size="28" weight="duotone" class="feature-icon" />
                <div>
                  <NText strong>{{ t('dashboard.features.locations.title') }}</NText>
                  <NText depth="3" tag="p" style="margin: 2px 0 0 0; font-size: 12px">
                    {{ t('dashboard.features.locations.desc') }}
                  </NText>
                </div>
              </NSpace>
              <PhArrowRight :size="16" weight="bold" />
            </NSpace>
          </NCard>
        </NGridItem>
        <NGridItem :span="24" :md="8">
          <NCard size="small" hoverable class="feature-card" @click="router.push('/writers')">
            <NSpace align="center" justify="space-between">
              <NSpace align="center">
                <PhPen :size="28" weight="duotone" class="feature-icon" />
                <div>
                  <NText strong>{{ t('dashboard.features.writers.title') }}</NText>
                  <NText depth="3" tag="p" style="margin: 2px 0 0 0; font-size: 12px">
                    {{ t('dashboard.features.writers.desc') }}
                  </NText>
                </div>
              </NSpace>
              <PhArrowRight :size="16" weight="bold" />
            </NSpace>
          </NCard>
        </NGridItem>
        <NGridItem :span="24" :md="8">
          <NCard size="small" hoverable class="feature-card" @click="router.push('/articles')">
            <NSpace align="center" justify="space-between">
              <NSpace align="center">
                <PhArticle :size="28" weight="duotone" class="feature-icon" />
                <div>
                  <NText strong>{{ t('dashboard.features.articles.title') }}</NText>
                  <NText depth="3" tag="p" style="margin: 2px 0 0 0; font-size: 12px">
                    {{ t('dashboard.features.articles.desc') }}
                  </NText>
                </div>
              </NSpace>
              <PhArrowRight :size="16" weight="bold" />
            </NSpace>
          </NCard>
        </NGridItem>
      </NGrid>
    </NSpin>
  </div>
</template>

<style scoped>
.dashboard {
  max-width: 900px;
  margin: 0 auto;
}

.feature-card {
  cursor: pointer;
  transition: border-color 0.15s ease;
}

.feature-card:hover {
  border-color: var(--n-primary-color);
}

.feature-icon {
  color: var(--n-primary-color);
}
</style>
