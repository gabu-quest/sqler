<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  NGrid,
  NGridItem,
  NCard,
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
  NSelect,
  NTag,
  NPopconfirm,
  useMessage
} from 'naive-ui'
import { PhPlus, PhTrash, PhGlobe, PhMapPin } from '@phosphor-icons/vue'
import { fetchApi, ApiError } from '@/composables/useApi'

const { t } = useI18n()
const message = useMessage()

interface Country {
  id: number
  version: number
  name: string
  code: string
}

interface City {
  id: number
  version: number
  name: string
  country: { _id: number; name: string; code: string } | null
}

const countries = ref<Country[]>([])
const cities = ref<City[]>([])
const selectedCountryId = ref<number | null>(null)
const loading = ref(false)

// Modals
const showCountryModal = ref(false)
const showCityModal = ref(false)
const countryForm = ref({ name: '', code: '' })
const cityForm = ref({ name: '', country_id: null as number | null })

const selectedCountry = computed(() =>
  countries.value.find((c) => c.id === selectedCountryId.value)
)

const filteredCities = computed(() =>
  selectedCountryId.value
    ? cities.value.filter((c) => c.country?._id === selectedCountryId.value)
    : cities.value
)

const countryOptions = computed(() =>
  countries.value.map((c) => ({ label: `${c.name} (${c.code})`, value: c.id }))
)

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

function selectCountry(id: number) {
  selectedCountryId.value = selectedCountryId.value === id ? null : id
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
      body: JSON.stringify(countryForm.value)
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
    if (selectedCountryId.value === id) {
      selectedCountryId.value = null
    }
    loadData()
  } catch (e: unknown) {
    if (e instanceof ApiError && e.status === 409) {
      message.error(t('locations.hasDependencies'))
    } else {
      message.error(t('common.error'))
    }
  }
}

function openCityModal() {
  cityForm.value = {
    name: '',
    country_id: selectedCountryId.value
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
      body: JSON.stringify(cityForm.value)
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

function getCityCount(countryId: number): number {
  return cities.value.filter((c) => c.country?._id === countryId).length
}

onMounted(loadData)
</script>

<template>
  <div class="locations">
    <NGrid :cols="2" :x-gap="16" :y-gap="16" responsive="screen" :item-responsive="true">
      <!-- Countries Panel -->
      <NGridItem :span="24" :md="12">
        <NCard :title="t('locations.countries')" size="small">
          <template #header-extra>
            <NButton size="small" type="primary" @click="openCountryModal">
              <template #icon><PhPlus weight="bold" /></template>
              {{ t('locations.createCountry') }}
            </NButton>
          </template>

          <NList v-if="countries.length" hoverable clickable>
            <NListItem
              v-for="country in countries"
              :key="country.id"
              :class="{ selected: selectedCountryId === country.id }"
              @click="selectCountry(country.id)"
            >
              <NThing>
                <template #avatar>
                  <PhGlobe :size="24" weight="duotone" class="country-icon" />
                </template>
                <template #header>
                  {{ country.name }}
                  <NTag size="small" style="margin-left: 8px">{{ country.code }}</NTag>
                </template>
                <template #description>
                  {{ t('locations.cityCount', { count: getCityCount(country.id) }) }}
                </template>
              </NThing>
              <template #suffix>
                <NPopconfirm @positive-click="deleteCountry(country.id)">
                  <template #trigger>
                    <NButton size="tiny" quaternary type="error" @click.stop>
                      <template #icon><PhTrash weight="regular" /></template>
                    </NButton>
                  </template>
                  {{ t('locations.confirmDeleteCountry') }}
                </NPopconfirm>
              </template>
            </NListItem>
          </NList>
          <NEmpty v-else :description="t('common.noData')" />
        </NCard>
      </NGridItem>

      <!-- Cities Panel -->
      <NGridItem :span="24" :md="12">
        <NCard
          :title="selectedCountry ? t('locations.citiesInCountry', { country: selectedCountry.name }) : t('locations.cities')"
          size="small"
        >
          <template #header-extra>
            <NButton size="small" type="primary" @click="openCityModal">
              <template #icon><PhPlus weight="bold" /></template>
              {{ t('locations.createCity') }}
            </NButton>
          </template>

          <NList v-if="filteredCities.length" hoverable>
            <NListItem v-for="city in filteredCities" :key="city.id">
              <NThing>
                <template #avatar>
                  <PhMapPin :size="20" weight="duotone" class="city-icon" />
                </template>
                <template #header>{{ city.name }}</template>
                <template #description v-if="!selectedCountryId && city.country">
                  {{ city.country.name }}
                </template>
              </NThing>
              <template #suffix>
                <NPopconfirm @positive-click="deleteCity(city.id)">
                  <template #trigger>
                    <NButton size="tiny" quaternary type="error">
                      <template #icon><PhTrash weight="regular" /></template>
                    </NButton>
                  </template>
                  {{ t('locations.confirmDeleteCity') }}
                </NPopconfirm>
              </template>
            </NListItem>
          </NList>
          <NEmpty v-else :description="t('locations.noCities')" />
        </NCard>
      </NGridItem>
    </NGrid>

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
        <NFormItem :label="t('locations.selectCountry')">
          <NSelect
            v-model:value="cityForm.country_id"
            :options="countryOptions"
            :placeholder="t('locations.selectCountry')"
          />
        </NFormItem>
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
  max-width: 1000px;
  margin: 0 auto;
}

.selected {
  background: var(--n-item-color-active);
}

.country-icon,
.city-icon {
  color: var(--n-primary-color);
}
</style>
