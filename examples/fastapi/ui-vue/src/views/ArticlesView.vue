<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  NCard,
  NButton,
  NSpace,
  NInput,
  NModal,
  NForm,
  NFormItem,
  NSelect,
  NAlert,
  NSpin,
  NTag,
  NText,
  NDynamicTags,
  NDrawer,
  NDrawerContent,
  NTimeline,
  NTimelineItem,
  NPopconfirm,
  useMessage
} from 'naive-ui'
import { PhMagnifyingGlass, PhArrowsClockwise, PhPencil, PhTrash, PhClockCounterClockwise, PhPlus } from '@phosphor-icons/vue'
import { fetchApi } from '@/composables/useApi'

interface Writer {
  id: number
  name: string
}

interface Article {
  id: number
  version: number
  title: string
  content: string
  tags: string[]
  writer: { _id: number; name: string } | null
  created_at: string | null
  updated_at: string | null
}

interface SearchResult {
  article: Article
  score: number | null
  highlights: Record<string, string> | null
}

interface AuditEntry {
  timestamp: string
  action: string
  changes: Record<string, { old: unknown; new: unknown }> | null
}

const { t } = useI18n()
const message = useMessage()

const articles = ref<Article[]>([])
const writers = ref<Writer[]>([])
const searchResults = ref<SearchResult[]>([])
const loading = ref(false)
const searchQuery = ref('')
const isSearching = ref(false)
const showModal = ref(false)
const editingArticle = ref<Article | null>(null)

// Audit drawer
const showAuditDrawer = ref(false)
const auditLog = ref<AuditEntry[]>([])
const auditArticleTitle = ref('')

const formData = ref({
  title: '',
  content: '',
  writer_id: null as number | null,
  tags: [] as string[]
})

const writerOptions = computed(() =>
  writers.value.map((w) => ({ label: w.name, value: w.id }))
)

async function loadData() {
  loading.value = true
  try {
    const [articlesRes, writersRes] = await Promise.all([
      fetchApi<Article[]>('/api/articles?limit=50'),
      fetchApi<Writer[]>('/api/writers')
    ])
    articles.value = articlesRes
    writers.value = writersRes
  } finally {
    loading.value = false
  }
}

async function search() {
  if (!searchQuery.value.trim()) {
    isSearching.value = false
    return
  }
  loading.value = true
  isSearching.value = true
  try {
    searchResults.value = await fetchApi<SearchResult[]>(
      `/api/articles/search/query?q=${encodeURIComponent(searchQuery.value)}&highlight=true`
    )
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editingArticle.value = null
  formData.value = { title: '', content: '', writer_id: null, tags: [] }
  showModal.value = true
}

function openEdit(article: Article) {
  editingArticle.value = article
  formData.value = {
    title: article.title,
    content: article.content,
    writer_id: article.writer?._id ?? null,
    tags: [...article.tags]
  }
  showModal.value = true
}

async function saveArticle() {
  try {
    const payload = {
      title: formData.value.title,
      content: formData.value.content,
      writer_id: formData.value.writer_id,
      tags: formData.value.tags
    }

    if (editingArticle.value) {
      await fetchApi(`/api/articles/${editingArticle.value.id}`, {
        method: 'PATCH',
        body: JSON.stringify(payload)
      })
      message.success(t('articles.messages.updated'))
    } else {
      await fetchApi('/api/articles', {
        method: 'POST',
        body: JSON.stringify(payload)
      })
      message.success(t('articles.messages.created'))
    }
    showModal.value = false
    if (isSearching.value) {
      search()
    } else {
      loadData()
    }
  } catch (e) {
    message.error(e instanceof Error ? e.message : t('common.error'))
  }
}

async function deleteArticle(id: number) {
  try {
    await fetchApi(`/api/articles/${id}`, { method: 'DELETE' })
    message.success(t('articles.messages.deleted'))
    if (isSearching.value) {
      search()
    } else {
      loadData()
    }
  } catch (e) {
    message.error(t('common.error'))
  }
}

async function rebuildIndex() {
  try {
    await fetchApi('/api/articles/fts/rebuild', { method: 'POST' })
    message.success(t('articles.messages.indexRebuilt'))
  } catch (e) {
    message.error(e instanceof Error ? e.message : t('common.error'))
  }
}

async function openAuditDrawer(article: Article) {
  auditArticleTitle.value = article.title
  try {
    const log = await fetchApi<AuditEntry[]>(`/api/articles/${article.id}/audit-log`)
    auditLog.value = log
    showAuditDrawer.value = true
  } catch (e) {
    message.error(t('common.error'))
  }
}

function formatScore(score: number | null): string {
  return score?.toFixed(3) ?? '-'
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

function getWriterName(article: Article): string {
  return article.writer?.name ?? t('articles.noWriter')
}

// Debounced search
let searchTimeout: ReturnType<typeof setTimeout>
watch(searchQuery, () => {
  clearTimeout(searchTimeout)
  if (!searchQuery.value.trim()) {
    isSearching.value = false
    return
  }
  searchTimeout = setTimeout(search, 300)
})

onMounted(loadData)

onBeforeUnmount(() => {
  clearTimeout(searchTimeout)
})
</script>

<template>
  <div class="articles-view">
    <NCard :title="t('articles.title')" size="small">
      <template #header-extra>
        <NSpace size="small">
          <NButton size="small" @click="rebuildIndex" :disabled="loading">
            <template #icon><PhArrowsClockwise :size="14" /></template>
            {{ t('articles.rebuildIndex') }}
          </NButton>
          <NButton size="small" type="primary" @click="openCreate">
            <template #icon><PhPlus weight="bold" /></template>
            {{ t('articles.createArticle') }}
          </NButton>
        </NSpace>
      </template>

      <!-- Search Bar -->
      <div class="search-container">
        <NInput
          v-model:value="searchQuery"
          :placeholder="t('articles.searchPlaceholder')"
          clearable
          size="large"
        >
          <template #prefix>
            <PhMagnifyingGlass :size="18" />
          </template>
        </NInput>
      </div>

      <NSpin :show="loading">
        <!-- Search Results -->
        <template v-if="isSearching">
          <NAlert type="info" style="margin-bottom: 16px">
            {{ t('articles.resultsFound', { count: searchResults.length, query: searchQuery }) }}
          </NAlert>

          <div v-if="searchResults.length === 0" class="no-results">
            <NText depth="3">{{ t('common.noData') }}</NText>
          </div>

          <div v-for="result in searchResults" :key="result.article.id" class="result-card">
            <NSpace justify="space-between" align="start">
              <div class="result-content">
                <NSpace align="center" style="margin-bottom: 4px">
                  <NText strong style="font-size: 16px">{{ result.article.title }}</NText>
                  <NTag type="primary" size="small">{{ t('articles.score') }}: {{ formatScore(result.score) }}</NTag>
                </NSpace>
                <NText depth="3" style="display: block; margin-bottom: 8px">
                  {{ t('articles.writer') }}: {{ getWriterName(result.article) }}
                </NText>
                <!-- Highlights -->
                <div
                  v-if="result.highlights?.content"
                  v-html="result.highlights.content"
                  class="highlight-content"
                />
                <NSpace style="margin-top: 8px">
                  <NTag v-for="tag in result.article.tags" :key="tag" size="small">{{ tag }}</NTag>
                </NSpace>
              </div>
              <NSpace size="small">
                <NButton size="tiny" quaternary @click="openAuditDrawer(result.article)">
                  <template #icon><PhClockCounterClockwise weight="regular" /></template>
                </NButton>
                <NButton size="tiny" quaternary @click="openEdit(result.article)">
                  <template #icon><PhPencil weight="regular" /></template>
                </NButton>
                <NPopconfirm @positive-click="deleteArticle(result.article.id)">
                  <template #trigger>
                    <NButton size="tiny" quaternary type="error">
                      <template #icon><PhTrash weight="regular" /></template>
                    </NButton>
                  </template>
                  {{ t('common.confirm') }}?
                </NPopconfirm>
              </NSpace>
            </NSpace>
          </div>
        </template>

        <!-- All Articles (when not searching) -->
        <template v-else>
          <div v-if="articles.length === 0" class="no-results">
            <NText depth="3">{{ t('common.noData') }}</NText>
          </div>

          <div v-for="article in articles" :key="article.id" class="result-card">
            <NSpace justify="space-between" align="start">
              <div class="result-content">
                <NText strong style="font-size: 16px; display: block; margin-bottom: 4px">
                  {{ article.title }}
                </NText>
                <NText depth="3" style="display: block; margin-bottom: 8px">
                  {{ t('articles.writer') }}: {{ getWriterName(article) }}
                </NText>
                <NText depth="2" style="display: block; margin-bottom: 8px" class="article-preview">
                  {{ article.content.slice(0, 150) }}{{ article.content.length > 150 ? '...' : '' }}
                </NText>
                <NSpace>
                  <NTag v-for="tag in article.tags" :key="tag" size="small">{{ tag }}</NTag>
                </NSpace>
              </div>
              <NSpace size="small">
                <NButton size="tiny" quaternary @click="openAuditDrawer(article)">
                  <template #icon><PhClockCounterClockwise weight="regular" /></template>
                </NButton>
                <NButton size="tiny" quaternary @click="openEdit(article)">
                  <template #icon><PhPencil weight="regular" /></template>
                </NButton>
                <NPopconfirm @positive-click="deleteArticle(article.id)">
                  <template #trigger>
                    <NButton size="tiny" quaternary type="error">
                      <template #icon><PhTrash weight="regular" /></template>
                    </NButton>
                  </template>
                  {{ t('common.confirm') }}?
                </NPopconfirm>
              </NSpace>
            </NSpace>
          </div>
        </template>
      </NSpin>
    </NCard>

    <!-- Create/Edit Modal -->
    <NModal
      v-model:show="showModal"
      :title="editingArticle ? t('articles.editArticle') : t('articles.createArticle')"
      preset="card"
      style="width: 600px"
    >
      <NForm :model="formData" label-placement="top">
        <NFormItem :label="t('articles.fields.title')" path="title">
          <NInput v-model:value="formData.title" :placeholder="t('articles.fields.title')" />
        </NFormItem>
        <NFormItem :label="t('articles.selectWriter')" path="writer_id">
          <NSelect
            v-model:value="formData.writer_id"
            :options="writerOptions"
            :placeholder="t('articles.selectWriter')"
            clearable
          />
        </NFormItem>
        <NFormItem :label="t('articles.fields.content')" path="content">
          <NInput
            v-model:value="formData.content"
            type="textarea"
            :rows="6"
            :placeholder="t('articles.fields.content')"
          />
        </NFormItem>
        <NFormItem :label="t('articles.fields.tags')" path="tags">
          <NDynamicTags v-model:value="formData.tags" />
        </NFormItem>
      </NForm>
      <template #footer>
        <NSpace justify="end">
          <NButton @click="showModal = false">{{ t('common.cancel') }}</NButton>
          <NButton type="primary" @click="saveArticle">{{ t('common.save') }}</NButton>
        </NSpace>
      </template>
    </NModal>

    <!-- Audit Log Drawer -->
    <NDrawer v-model:show="showAuditDrawer" :width="400">
      <NDrawerContent :title="`${t('audit.title')} - ${auditArticleTitle}`">
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
.articles-view {
  max-width: 900px;
  margin: 0 auto;
}

.search-container {
  margin-bottom: 24px;
}

.result-card {
  padding: 16px;
  border: 1px solid var(--n-border-color);
  border-radius: 8px;
  margin-bottom: 12px;
  transition: border-color 0.15s ease;
}

.result-card:hover {
  border-color: var(--n-primary-color);
}

.result-content {
  flex: 1;
  min-width: 0;
}

.article-preview {
  line-height: 1.5;
}

.no-results {
  text-align: center;
  padding: 48px 0;
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

<style>
/* Global styles for highlight content */
.highlight-content {
  background: var(--n-card-color);
  padding: 8px 12px;
  border-radius: 4px;
  font-size: 13px;
  line-height: 1.6;
}

.highlight-content b,
.highlight-content mark {
  background-color: rgba(0, 229, 255, 0.3);
  color: inherit;
  padding: 0 2px;
  border-radius: 2px;
}
</style>
