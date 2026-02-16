<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  NCard,
  NCollapse,
  NCollapseItem,
  NDataTable,
  NAlert,
  NTag,
  NSpace,
  NButton,
  NSpin,
  NTooltip,
  useMessage
} from 'naive-ui'
import { PhTable, PhArrowsClockwise } from '@phosphor-icons/vue'
import { fetchApi } from '@/composables/useApi'

interface Column {
  name: string
  type: string
  pk: boolean
  nullable: boolean
}

interface Table {
  name: string
  category: 'data' | 'audit' | 'fts' | 'system'
  columns: Column[]
  row_count: number
}

interface Index {
  name: string
  table: string
  columns: string[]
}

interface Relationship {
  from_table: string
  from_column: string
  to_table: string
  to_column: string
}

interface SchemaData {
  tables: Table[]
  indices: Index[]
  relationships: Relationship[]
}

const { t } = useI18n()
const message = useMessage()

const schema = ref<SchemaData | null>(null)
const loading = ref(true)
const error = ref<string | null>(null)
const expandedCategories = ref<string[]>(['data'])

// Group tables by category
const tablesByCategory = computed(() => {
  if (!schema.value) return { data: [], audit: [], fts: [], system: [] }
  const groups: Record<string, Table[]> = { data: [], audit: [], fts: [], system: [] }
  for (const table of schema.value.tables) {
    groups[table.category]?.push(table)
  }
  return groups
})

// Build entity cards with key info for ER diagram
interface EntityCard {
  name: string
  displayName: string
  keyField: string
  rowCount: number
  fkTo?: string  // Foreign key points to this table
  fkColumn?: string  // The FK column name (e.g., "country")
}

const entityCards = computed<EntityCard[]>(() => {
  if (!schema.value || schema.value.relationships.length === 0) return []

  const rels = schema.value.relationships
  const dataTables = tablesByCategory.value.data

  // Build dependency graph (child -> parent)
  const deps = new Map<string, { parent: string; fkColumn: string }>()
  for (const rel of rels) {
    const fromTable = dataTables.find(t => t.name === rel.from_table)
    const toTable = dataTables.find(t => t.name === rel.to_table)
    if (fromTable && toTable) {
      // Extract the reference name (e.g., "country" from "country._id")
      const fkColumn = rel.from_column.replace('._id', '')
      deps.set(rel.from_table, { parent: rel.to_table, fkColumn })
    }
  }

  // Find root (table with no parent)
  const children = new Set(deps.keys())
  const dataTableNames = new Set(dataTables.map(t => t.name))
  const roots = [...dataTableNames].filter(t => !children.has(t) && deps.size > 0)

  if (roots.length === 0) return []

  // Build chain from root to leaves
  const cards: EntityCard[] = []
  let current = roots[0]
  const visited = new Set<string>()

  while (current && !visited.has(current)) {
    const table = dataTables.find(t => t.name === current)
    if (table) {
      // Find the key display field (usually "name" or "title")
      const keyField = table.name === 'articles' ? 'title' : 'name'
      const depInfo = deps.get(current)

      cards.push({
        name: table.name,
        displayName: table.name.charAt(0).toUpperCase() + table.name.slice(1),
        keyField,
        rowCount: table.row_count,
        fkTo: depInfo?.parent,
        fkColumn: depInfo?.fkColumn,
      })
    }
    visited.add(current)
    // Find child that references current
    const nextEntry = [...deps.entries()].find(([_, info]) => info.parent === current)
    current = nextEntry?.[0] || ''
  }

  return cards
})

// Category metadata
const categories = computed(() => [
  {
    key: 'data',
    title: t('schema.categories.data'),
    tables: tablesByCategory.value.data,
    color: 'linear-gradient(135deg, #8b5cf6 0%, #7c3aed 100%)',
    explainer: t('schema.explainers.data'),
  },
  {
    key: 'audit',
    title: t('schema.categories.audit'),
    tables: tablesByCategory.value.audit,
    color: 'linear-gradient(135deg, #3b82f6 0%, #2563eb 100%)',
    explainer: t('schema.explainers.audit'),
  },
  {
    key: 'fts',
    title: t('schema.categories.fts'),
    tables: tablesByCategory.value.fts,
    color: 'linear-gradient(135deg, #f97316 0%, #ea580c 100%)',
    explainer: t('schema.explainers.fts'),
  },
  {
    key: 'system',
    title: t('schema.categories.system'),
    tables: tablesByCategory.value.system,
    color: 'linear-gradient(135deg, #6b7280 0%, #4b5563 100%)',
    explainer: t('schema.explainers.system'),
  },
])

// Indices table columns (computed for i18n reactivity)
const indexColumns = computed(() => [
  { title: t('schema.indexName'), key: 'name' },
  { title: t('schema.indexTable'), key: 'table' },
  {
    title: t('schema.indexColumns'),
    key: 'columns',
    render: (row: Index) => row.columns.join(', ')
  },
])

async function loadSchema() {
  loading.value = true
  error.value = null
  try {
    schema.value = await fetchApi<SchemaData>('/api/db/schema')
  } catch (e) {
    error.value = e instanceof Error ? e.message : t('common.error')
  } finally {
    loading.value = false
  }
}

onMounted(loadSchema)
</script>

<template>
  <div class="schema-view">
    <NSpin :show="loading">
      <NCard :title="t('schema.title')" size="small">
        <template #header-extra>
          <NTooltip>
            <template #trigger>
              <NButton size="small" @click="loadSchema" :loading="loading">
                <template #icon><PhArrowsClockwise weight="bold" /></template>
                {{ t('common.refresh') }}
              </NButton>
            </template>
            {{ t('schema.refreshTooltip') }}
          </NTooltip>
        </template>

        <NAlert v-if="error" type="error" :title="error" style="margin-bottom: 16px" closable />

        <!-- Entity Relationship Diagram -->
        <div v-if="schema && entityCards.length > 0" class="er-diagram">
          <div class="er-title">{{ t('schema.relationships') }}</div>
          <div class="er-flow">
            <template v-for="(card, idx) in entityCards" :key="card.name">
              <!-- Entity Card -->
              <div class="er-card">
                <div class="er-card-header">
                  <span class="er-card-name">{{ card.displayName }}</span>
                  <span class="er-card-count">{{ card.rowCount }}</span>
                </div>
                <div class="er-card-body">
                  <div class="er-card-field er-card-field-pk">
                    <span class="er-field-icon">🔑</span>
                    <span class="er-field-name">_id</span>
                  </div>
                  <div class="er-card-field">
                    <span class="er-field-icon">📝</span>
                    <span class="er-field-name">{{ card.keyField }}</span>
                  </div>
                  <div v-if="card.fkColumn" class="er-card-field er-card-field-fk">
                    <span class="er-field-icon">🔗</span>
                    <span class="er-field-name">{{ card.fkColumn }}</span>
                  </div>
                </div>
              </div>
              <!-- Connector Arrow -->
              <div v-if="idx < entityCards.length - 1" class="er-connector">
                <div class="er-connector-line"></div>
                <div class="er-connector-label">has many</div>
                <div class="er-connector-arrow">▶</div>
              </div>
            </template>
          </div>
        </div>

        <!-- Table Categories -->
        <NCollapse v-model:expanded-names="expandedCategories" accordion>
          <NCollapseItem
            v-for="cat in categories"
            :key="cat.key"
            :name="cat.key"
          >
            <template #header>
              <div class="category-header" :style="{ background: cat.color }">
                <span class="category-title">{{ cat.title }}</span>
                <NTag size="small" :bordered="false" color="#ffffff33">
                  {{ cat.tables.length }}
                </NTag>
              </div>
            </template>

            <div class="category-content">
              <NAlert type="info" :bordered="false" style="margin-bottom: 12px">
                {{ cat.explainer }}
              </NAlert>

              <div v-for="table in cat.tables" :key="table.name" class="table-item">
                <div class="table-header">
                  <PhTable :size="16" weight="duotone" class="table-icon" />
                  <span class="table-name">{{ table.name }}</span>
                  <NTag size="tiny" :bordered="false" type="default">
                    {{ table.row_count }} {{ t('schema.rows') }}
                  </NTag>
                </div>
                <div class="table-columns">
                  <div v-for="col in table.columns" :key="col.name" class="column-pill" :class="{ 'column-pill-pk': col.pk }">
                    <span class="column-pill-name">{{ col.name }}</span>
                    <span class="column-pill-type">{{ col.type }}</span>
                    <span v-if="col.pk" class="column-pill-badge pk-badge">PK</span>
                    <span v-else-if="!col.nullable" class="column-pill-badge nn-badge">NOT NULL</span>
                  </div>
                </div>
              </div>
            </div>
          </NCollapseItem>
        </NCollapse>

        <!-- Indices Section -->
        <div v-if="schema && schema.indices.length > 0" class="indices-section">
          <div class="section-title">{{ t('schema.indices') }}</div>
          <NDataTable
            :columns="indexColumns"
            :data="schema.indices"
            size="small"
            :bordered="false"
          />
        </div>
      </NCard>
    </NSpin>
  </div>
</template>

<style scoped>
.schema-view {
  max-width: 900px;
  margin: 0 auto;
}

/* ER Diagram */
.er-diagram {
  background: linear-gradient(135deg, rgba(20, 184, 166, 0.08) 0%, rgba(6, 182, 212, 0.08) 100%);
  border: 1px solid rgba(20, 184, 166, 0.3);
  border-radius: 12px;
  padding: 20px;
  margin-bottom: 16px;
}

.er-title {
  font-size: 12px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: #14b8a6;
  margin-bottom: 16px;
  text-align: center;
}

.er-flow {
  display: flex;
  align-items: stretch;
  justify-content: center;
  gap: 0;
  overflow-x: auto;
  padding-bottom: 8px;
  /* Smooth horizontal scroll */
  scroll-behavior: smooth;
  -webkit-overflow-scrolling: touch;
}

/* Custom scrollbar for ER diagram */
.er-flow::-webkit-scrollbar {
  height: 6px;
}

.er-flow::-webkit-scrollbar-track {
  background: rgba(20, 184, 166, 0.1);
  border-radius: 3px;
}

.er-flow::-webkit-scrollbar-thumb {
  background: rgba(20, 184, 166, 0.4);
  border-radius: 3px;
}

.er-flow::-webkit-scrollbar-thumb:hover {
  background: rgba(20, 184, 166, 0.6);
}

/* Entity Cards */
.er-card {
  background: var(--n-color-modal);
  border: 2px solid #14b8a6;
  border-radius: 8px;
  min-width: 100px;
  max-width: 130px;
  overflow: hidden;
  box-shadow: 0 2px 8px rgba(20, 184, 166, 0.15);
  flex-shrink: 0;
}

.er-card-header {
  background: linear-gradient(135deg, #14b8a6 0%, #06b6d4 100%);
  color: white;
  padding: 6px 10px;
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.er-card-name {
  font-weight: 600;
  font-size: 12px;
}

.er-card-count {
  font-size: 10px;
  background: rgba(255, 255, 255, 0.25);
  padding: 1px 5px;
  border-radius: 10px;
}

.er-card-body {
  padding: 6px;
}

.er-card-field {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 3px 5px;
  font-size: 11px;
  border-radius: 4px;
  margin-bottom: 3px;
}

.er-card-field:last-child {
  margin-bottom: 0;
}

.er-card-field-pk {
  background: rgba(234, 179, 8, 0.15);
}

.er-card-field-fk {
  background: rgba(59, 130, 246, 0.15);
}

.er-field-icon {
  font-size: 9px;
}

.er-field-name {
  font-family: 'JetBrains Mono', monospace;
  color: var(--n-text-color-2);
  font-size: 10px;
}

/* Connector between cards */
.er-connector {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 0 6px;
  min-width: 60px;
  flex-shrink: 0;
}

.er-connector-line {
  width: 100%;
  height: 2px;
  background: linear-gradient(90deg, #14b8a6 0%, #06b6d4 100%);
  position: relative;
}

.er-connector-label {
  font-size: 9px;
  color: var(--n-text-color-3);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin: 4px 0;
  white-space: nowrap;
}

.er-connector-arrow {
  color: #06b6d4;
  font-size: 10px;
}

/* Category Headers */
.category-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-radius: 6px;
  color: white;
  width: 100%;
}

.category-title {
  font-weight: 600;
  font-size: 14px;
}

.category-content {
  padding: 12px 0;
}

/* Table Items */
.table-item {
  background: var(--n-color-modal);
  border: 1px solid var(--n-border-color);
  border-radius: 6px;
  margin-bottom: 8px;
  overflow: hidden;
}

.table-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  background: var(--n-color-popover);
  border-bottom: 1px solid var(--n-border-color);
}

.table-icon {
  color: var(--n-primary-color);
}

.table-name {
  font-weight: 600;
  font-size: 13px;
  flex: 1;
}

.table-columns {
  padding: 10px 12px;
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 8px;
}

/* Column Pills - self-contained units */
.column-pill {
  display: inline-flex;
  align-items: center;
  gap: 0;
  border-radius: 6px;
  overflow: hidden;
  font-size: 12px;
  border: 1px solid var(--n-border-color);
  background: var(--n-color-embedded);
}

.column-pill-pk {
  border-color: rgba(139, 92, 246, 0.4);
  background: rgba(139, 92, 246, 0.08);
}

.column-pill-name {
  font-family: 'JetBrains Mono', monospace;
  padding: 4px 8px;
  color: var(--n-text-color-1);
  font-weight: 500;
  background: var(--n-color-popover);
}

.column-pill-pk .column-pill-name {
  color: #a78bfa;
  background: rgba(139, 92, 246, 0.15);
}

.column-pill-type {
  font-family: 'JetBrains Mono', monospace;
  padding: 4px 8px;
  color: var(--n-text-color-3);
  font-size: 11px;
}

.column-pill-badge {
  padding: 4px 6px;
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 0.5px;
}

.pk-badge {
  background: linear-gradient(135deg, #8b5cf6 0%, #7c3aed 100%);
  color: white;
}

.nn-badge {
  background: rgba(234, 179, 8, 0.2);
  color: #ca8a04;
}

/* Indices Section */
.indices-section {
  margin-top: 24px;
  padding-top: 16px;
  border-top: 1px solid var(--n-border-color);
}

.section-title {
  font-size: 14px;
  font-weight: 600;
  margin-bottom: 12px;
  color: var(--n-text-color-1);
}
</style>
