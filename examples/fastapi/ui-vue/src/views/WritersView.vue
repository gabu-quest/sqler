<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  NCard,
  NDataTable,
  NButton,
  NSpace,
  NModal,
  NForm,
  NFormItem,
  NInput,
  NSelect,
  NDrawer,
  NDrawerContent,
  NTimeline,
  NTimelineItem,
  NPopconfirm,
  NText,
  useMessage,
  type DataTableColumns
} from 'naive-ui'
import { PhPlus, PhPencil, PhTrash, PhClockCounterClockwise } from '@phosphor-icons/vue'
import { fetchApi, ApiError } from '@/composables/useApi'

const { t } = useI18n()
const message = useMessage()

interface Country {
  id: number
  name: string
  code: string
}

interface City {
  id: number
  name: string
  country: { _id: number; name: string; code: string } | null
}

interface Writer {
  id: number
  version: number
  name: string
  bio: string
  city: { _id: number; name: string; country?: { _id: number; name: string } } | null
  created_at: string | null
  updated_at: string | null
}

interface AuditEntry {
  timestamp: string
  action: string
  changes: Record<string, { old: unknown; new: unknown }> | null
}

const writers = ref<Writer[]>([])
const countries = ref<Country[]>([])
const cities = ref<City[]>([])
const loading = ref(false)

// Modal state
const showModal = ref(false)
const editingWriter = ref<Writer | null>(null)
const form = ref({
  name: '',
  bio: '',
  country_id: null as number | null,
  city_id: null as number | null
})

// Audit drawer
const showAuditDrawer = ref(false)
const auditLog = ref<AuditEntry[]>([])
const auditWriterName = ref('')

const countryOptions = computed(() =>
  countries.value.map((c) => ({ label: `${c.name} (${c.code})`, value: c.id }))
)

const cityOptions = computed(() => {
  if (!form.value.country_id) {
    return cities.value.map((c) => ({
      label: c.country ? `${c.name}, ${c.country.name}` : c.name,
      value: c.id
    }))
  }
  return cities.value
    .filter((c) => c.country?._id === form.value.country_id)
    .map((c) => ({ label: c.name, value: c.id }))
})

function getLocation(writer: Writer): string {
  if (!writer.city) return t('writers.noCity')
  const cityName = writer.city.name
  const countryName = writer.city.country?.name
  return countryName ? `${cityName}, ${countryName}` : cityName
}

const columns = computed<DataTableColumns<Writer>>(() => [
  {
    title: t('writers.writerName'),
    key: 'name',
    render: (row) => row.name
  },
  {
    title: t('writers.location'),
    key: 'location',
    render: (row) => getLocation(row)
  },
  {
    title: t('writers.writerBio'),
    key: 'bio',
    ellipsis: { tooltip: true },
    render: (row) => row.bio || '-'
  },
  {
    title: t('common.actions'),
    key: 'actions',
    width: 180,
    render: (row) => {
      return h(NSpace, { size: 'small' }, () => [
        h(
          NButton,
          {
            size: 'tiny',
            quaternary: true,
            onClick: () => openAuditDrawer(row)
          },
          { icon: () => h(PhClockCounterClockwise, { weight: 'regular' }) }
        ),
        h(
          NButton,
          {
            size: 'tiny',
            quaternary: true,
            onClick: () => openEditModal(row)
          },
          { icon: () => h(PhPencil, { weight: 'regular' }) }
        ),
        h(
          NPopconfirm,
          { onPositiveClick: () => deleteWriter(row.id) },
          {
            trigger: () =>
              h(
                NButton,
                { size: 'tiny', quaternary: true, type: 'error' },
                { icon: () => h(PhTrash, { weight: 'regular' }) }
              ),
            default: () => t('writers.confirmDelete')
          }
        )
      ])
    }
  }
])

import { h } from 'vue'

async function loadData() {
  loading.value = true
  try {
    const [writersRes, countriesRes, citiesRes] = await Promise.all([
      fetchApi<Writer[]>('/api/writers'),
      fetchApi<Country[]>('/api/locations/countries'),
      fetchApi<City[]>('/api/locations/cities')
    ])
    writers.value = writersRes
    countries.value = countriesRes
    cities.value = citiesRes
  } catch (e) {
    message.error(t('common.error'))
  } finally {
    loading.value = false
  }
}

function openCreateModal() {
  editingWriter.value = null
  form.value = { name: '', bio: '', country_id: null, city_id: null }
  showModal.value = true
}

function openEditModal(writer: Writer) {
  editingWriter.value = writer
  const countryId = writer.city?.country?._id ?? null
  form.value = {
    name: writer.name,
    bio: writer.bio,
    country_id: countryId,
    city_id: writer.city?._id ?? null
  }
  showModal.value = true
}

function onCountryChange() {
  // Reset city when country changes
  form.value.city_id = null
}

async function saveWriter() {
  if (!form.value.name) {
    message.warning(t('common.fillRequired'))
    return
  }

  try {
    if (editingWriter.value) {
      // Update
      await fetchApi(`/api/writers/${editingWriter.value.id}`, {
        method: 'PATCH',
        body: JSON.stringify({
          name: form.value.name,
          bio: form.value.bio,
          city_id: form.value.city_id
        })
      })
      message.success(t('writers.messages.updated'))
    } else {
      // Create
      await fetchApi('/api/writers', {
        method: 'POST',
        body: JSON.stringify({
          name: form.value.name,
          bio: form.value.bio,
          city_id: form.value.city_id
        })
      })
      message.success(t('writers.messages.created'))
    }
    showModal.value = false
    loadData()
  } catch (e) {
    message.error(t('common.error'))
  }
}

async function deleteWriter(id: number) {
  try {
    await fetchApi(`/api/writers/${id}`, { method: 'DELETE' })
    message.success(t('writers.messages.deleted'))
    loadData()
  } catch (e: unknown) {
    if (e instanceof ApiError && e.status === 409) {
      message.error(t('writers.hasDependencies'))
    } else {
      message.error(t('common.error'))
    }
  }
}

async function openAuditDrawer(writer: Writer) {
  auditWriterName.value = writer.name
  try {
    const log = await fetchApi<AuditEntry[]>(`/api/writers/${writer.id}/audit-log`)
    auditLog.value = log
    showAuditDrawer.value = true
  } catch (e) {
    message.error(t('common.error'))
  }
}

function formatTimestamp(ts: string): string {
  return new Date(ts).toLocaleString()
}

function getActionType(action: string): 'success' | 'warning' | 'error' | 'info' {
  if (action === 'created') return 'success'
  if (action === 'updated') return 'warning'
  if (action === 'deleted') return 'error'
  return 'info'
}

onMounted(loadData)
</script>

<template>
  <div class="writers">
    <NCard :title="t('writers.title')" size="small">
      <template #header-extra>
        <NButton size="small" type="primary" @click="openCreateModal">
          <template #icon><PhPlus weight="bold" /></template>
          {{ t('writers.createWriter') }}
        </NButton>
      </template>

      <NDataTable
        :columns="columns"
        :data="writers"
        :loading="loading"
        :bordered="false"
        size="small"
        :row-key="(row: Writer) => row.id"
      />
    </NCard>

    <!-- Create/Edit Modal -->
    <NModal
      v-model:show="showModal"
      preset="card"
      :title="editingWriter ? t('writers.editWriter') : t('writers.createWriter')"
      style="max-width: 500px"
    >
      <NForm>
        <NFormItem :label="t('writers.writerName')">
          <NInput v-model:value="form.name" />
        </NFormItem>
        <NFormItem :label="t('writers.writerBio')">
          <NInput v-model:value="form.bio" type="textarea" :rows="3" />
        </NFormItem>
        <NFormItem :label="t('locations.selectCountry')">
          <NSelect
            v-model:value="form.country_id"
            :options="countryOptions"
            clearable
            :placeholder="t('locations.selectCountry')"
            @update:value="onCountryChange"
          />
        </NFormItem>
        <NFormItem :label="t('writers.selectCity')">
          <NSelect
            v-model:value="form.city_id"
            :options="cityOptions"
            clearable
            :placeholder="t('writers.selectCity')"
            :disabled="cityOptions.length === 0"
          />
        </NFormItem>
      </NForm>
      <template #footer>
        <NSpace justify="end">
          <NButton @click="showModal = false">{{ t('common.cancel') }}</NButton>
          <NButton type="primary" @click="saveWriter">{{ t('common.save') }}</NButton>
        </NSpace>
      </template>
    </NModal>

    <!-- Audit Log Drawer -->
    <NDrawer v-model:show="showAuditDrawer" :width="400">
      <NDrawerContent :title="`${t('audit.title')} - ${auditWriterName}`">
        <NTimeline v-if="auditLog.length">
          <NTimelineItem
            v-for="(entry, idx) in auditLog"
            :key="idx"
            :type="getActionType(entry.action)"
            :title="t(`audit.${entry.action}`)"
            :time="formatTimestamp(entry.timestamp)"
          >
            <div v-if="entry.changes" class="audit-changes">
              <div v-for="(change, field) in entry.changes" :key="String(field)" class="change-row">
                <NText strong>{{ field }}</NText>
                <NText depth="3">{{ change.old ?? '-' }}</NText>
                <NText>&rarr;</NText>
                <NText>{{ change.new ?? '-' }}</NText>
              </div>
            </div>
          </NTimelineItem>
        </NTimeline>
        <NText v-else depth="3">{{ t('common.noAuditLog') }}</NText>
      </NDrawerContent>
    </NDrawer>
  </div>
</template>

<style scoped>
.writers {
  max-width: 1000px;
  margin: 0 auto;
}

.audit-changes {
  margin-top: 8px;
  font-size: 12px;
}

.change-row {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 4px;
}
</style>
