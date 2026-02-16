<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  NCard,
  NCollapse,
  NCollapseItem,
  NList,
  NListItem,
  NThing,
  NButton,
  NSpace,
  NEmpty,
  NModal,
  NForm,
  NFormItem,
  NInput,
  NTag,
  NPopconfirm,
  NTooltip,
  useMessage
} from 'naive-ui'
import { PhPlus, PhTrash, PhGlobe, PhMapPin } from '@phosphor-icons/vue'
import { fetchApi, ApiError } from '@/composables/useApi'

const { t } = useI18n()
const message = useMessage()

interface Country {
  _id: number
  version: number
  name: string
  code: string
}

interface City {
  _id: number
  version: number
  name: string
  country: { _id: number; name: string; code: string } | null
}

const countries = ref<Country[]>([])
const cities = ref<City[]>([])
const loading = ref(false)
const expandedNames = ref<string[]>([])

// Modals
const showCountryModal = ref(false)
const showCityModal = ref(false)
const countryForm = ref({ name: '', code: '' })
const cityForm = ref({ name: '', country_id: null as number | null })
const addingCityToCountryId = ref<number | null>(null)

function getCitiesForCountry(countryId: number): City[] {
  return cities.value.filter((c) => c.country?._id === countryId)
}

function getCityCount(countryId: number): number {
  return getCitiesForCountry(countryId).length
}

async function loadData() {
  loading.value = true
  try {
    const [countriesRes, citiesRes] = await Promise.all([
      fetchApi<Country[]>('/api/locations/countries'),
      fetchApi<City[]>('/api/locations/cities')
    ])
    countries.value = countriesRes
    cities.value = citiesRes
  } catch (e) {
    message.error(t('common.error'))
  } finally {
    loading.value = false
  }
}

function openCountryModal() {
  countryForm.value = { name: '', code: '' }
  showCountryModal.value = true
}

async function saveCountry() {
  if (!countryForm.value.name || !countryForm.value.code) {
    message.warning(t('common.fillRequired'))
    return
  }
  try {
    await fetchApi('/api/locations/countries', {
      method: 'POST',
      body: countryForm.value
    })
    message.success(t('locations.messages.countryCreated'))
    showCountryModal.value = false
    loadData()
  } catch (e) {
    message.error(t('common.error'))
  }
}

async function deleteCountry(id: number) {
  try {
    await fetchApi(`/api/locations/countries/${id}`, { method: 'DELETE' })
    message.success(t('locations.messages.countryDeleted'))
    loadData()
  } catch (e: unknown) {
    if (e instanceof ApiError && e.status === 409) {
      message.error(t('locations.hasDependencies'))
    } else {
      message.error(t('common.error'))
    }
  }
}

function openCityModal(countryId: number) {
  addingCityToCountryId.value = countryId
  cityForm.value = {
    name: '',
    country_id: countryId
  }
  showCityModal.value = true
}

async function saveCity() {
  if (!cityForm.value.name || !cityForm.value.country_id) {
    message.warning(t('common.fillRequired'))
    return
  }
  try {
    await fetchApi('/api/locations/cities', {
      method: 'POST',
      body: cityForm.value
    })
    message.success(t('locations.messages.cityCreated'))
    showCityModal.value = false
    loadData()
  } catch (e) {
    message.error(t('common.error'))
  }
}

async function deleteCity(id: number) {
  try {
    await fetchApi(`/api/locations/cities/${id}`, { method: 'DELETE' })
    message.success(t('locations.messages.cityDeleted'))
    loadData()
  } catch (e: unknown) {
    if (e instanceof ApiError && e.status === 409) {
      message.error(t('locations.hasDependencies'))
    } else {
      message.error(t('common.error'))
    }
  }
}

onMounted(loadData)
</script>

<template>
  <div class="locations">
    <NCard :title="t('locations.title')" size="small">
      <template #header-extra>
        <NButton size="small" type="primary" @click="openCountryModal">
          <template #icon><PhPlus weight="bold" /></template>
          {{ t('locations.createCountry') }}
        </NButton>
      </template>

      <NEmpty v-if="countries.length === 0" :description="t('common.noData')" />

      <NCollapse v-else v-model:expanded-names="expandedNames" accordion>
        <NCollapseItem
          v-for="country in countries"
          :key="country._id"
          :name="String(country._id)"
        >
          <template #header>
            <NSpace align="center">
              <PhGlobe :size="20" weight="duotone" class="country-icon" />
              <span class="country-name">{{ country.name }}</span>
              <NTag size="small">{{ country.code }}</NTag>
              <NTag size="tiny" :bordered="false" type="info">
                {{ t('locations.cityCount', { count: getCityCount(country._id) }) }}
              </NTag>
            </NSpace>
          </template>

          <template #header-extra>
            <NPopconfirm @positive-click="deleteCountry(country._id)">
              <template #trigger>
                <NTooltip>
                  <template #trigger>
                    <NButton size="tiny" quaternary type="error" @click.stop>
                      <template #icon><PhTrash weight="regular" /></template>
                    </NButton>
                  </template>
                  {{ t('common.delete') }}
                </NTooltip>
              </template>
              {{ t('locations.confirmDeleteCountry') }}
            </NPopconfirm>
          </template>

          <!-- Cities inside the country -->
          <div class="cities-container">
            <NList v-if="getCitiesForCountry(country._id).length" hoverable size="small">
              <NListItem v-for="city in getCitiesForCountry(country._id)" :key="city._id">
                <NThing>
                  <template #avatar>
                    <PhMapPin :size="16" weight="duotone" class="city-icon" />
                  </template>
                  <template #header>{{ city.name }}</template>
                </NThing>
                <template #suffix>
                  <NPopconfirm @positive-click="deleteCity(city._id)">
                    <template #trigger>
                      <NTooltip>
                        <template #trigger>
                          <NButton size="tiny" quaternary type="error">
                            <template #icon><PhTrash weight="regular" /></template>
                          </NButton>
                        </template>
                        {{ t('common.delete') }}
                      </NTooltip>
                    </template>
                    {{ t('locations.confirmDeleteCity') }}
                  </NPopconfirm>
                </template>
              </NListItem>
            </NList>
            <NEmpty v-else :description="t('locations.noCities')" size="small" style="padding: 16px 0" />

            <NButton
              size="small"
              type="primary"
              dashed
              block
              style="margin-top: 12px"
              @click="openCityModal(country._id)"
            >
              <template #icon><PhPlus weight="bold" /></template>
              {{ t('locations.createCity') }}
            </NButton>
          </div>
        </NCollapseItem>
      </NCollapse>
    </NCard>

    <!-- Country Modal -->
    <NModal v-model:show="showCountryModal" preset="card" :title="t('locations.createCountry')" style="max-width: 400px">
      <NForm>
        <NFormItem :label="t('locations.countryName')">
          <NInput v-model:value="countryForm.name" />
        </NFormItem>
        <NFormItem :label="t('locations.countryCode')">
          <NInput v-model:value="countryForm.code" maxlength="2" placeholder="JP, US, GB..." />
        </NFormItem>
      </NForm>
      <template #footer>
        <NSpace justify="end">
          <NButton @click="showCountryModal = false">{{ t('common.cancel') }}</NButton>
          <NButton type="primary" @click="saveCountry">{{ t('common.save') }}</NButton>
        </NSpace>
      </template>
    </NModal>

    <!-- City Modal -->
    <NModal v-model:show="showCityModal" preset="card" :title="t('locations.createCity')" style="max-width: 400px">
      <NForm>
        <NFormItem :label="t('locations.cityName')">
          <NInput v-model:value="cityForm.name" />
        </NFormItem>
      </NForm>
      <template #footer>
        <NSpace justify="end">
          <NButton @click="showCityModal = false">{{ t('common.cancel') }}</NButton>
          <NButton type="primary" @click="saveCity">{{ t('common.save') }}</NButton>
        </NSpace>
      </template>
    </NModal>
  </div>
</template>

<style scoped>
.locations {
  max-width: 700px;
  margin: 0 auto;
}

.country-icon,
.city-icon {
  color: var(--n-primary-color);
}

.country-name {
  font-weight: 500;
}

.cities-container {
  padding: 8px 0 8px 24px;
}
</style>
