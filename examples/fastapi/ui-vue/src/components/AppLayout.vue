<template>
  <n-layout class="app-layout">
    <n-layout-header bordered class="app-header">
      <div class="header-content">
        <router-link to="/" class="logo">
          <PhDatabase :size="24" weight="duotone" />
          <span class="logo-text">SQLer</span>
        </router-link>
        <n-menu mode="horizontal" :options="menuOptions" :value="activeKey" @update:value="handleMenuClick" />
        <div class="header-spacer" />
        <!-- Reset DB Button -->
        <n-tooltip>
          <template #trigger>
            <n-button size="small" type="warning" ghost @click="showResetConfirm = true" :loading="resetting">
              <template #icon><PhArrowsClockwise weight="bold" /></template>
              {{ t('header.resetDb') }}
            </n-button>
          </template>
          {{ t('header.resetDbTooltip') }}
        </n-tooltip>
        <!-- Language Switcher -->
        <n-button-group size="small">
          <n-button
            :type="locale === 'en' ? 'primary' : 'default'"
            :ghost="locale !== 'en'"
            @click="switchLocale('en')"
          >
            EN
          </n-button>
          <n-button
            :type="locale === 'ja' ? 'primary' : 'default'"
            :ghost="locale !== 'ja'"
            @click="switchLocale('ja')"
          >
            JP
          </n-button>
        </n-button-group>
      </div>
    </n-layout-header>
    <n-layout-content class="app-content">
      <slot />
    </n-layout-content>

    <!-- Reset Confirmation Dialog -->
    <n-modal v-model:show="showResetConfirm" preset="dialog" type="warning" :title="t('header.resetConfirmTitle')">
      <template #icon>
        <PhWarning :size="24" weight="fill" />
      </template>
      {{ t('header.resetConfirmMessage') }}
      <template #action>
        <n-space>
          <n-button @click="showResetConfirm = false">{{ t('common.cancel') }}</n-button>
          <n-button type="error" @click="resetDatabase" :loading="resetting">
            {{ t('header.resetConfirmButton') }}
          </n-button>
        </n-space>
      </template>
    </n-modal>
  </n-layout>
</template>

<script setup lang="ts">
import { ref, computed, h } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { NLayout, NLayoutHeader, NLayoutContent, NMenu, NButton, NButtonGroup, NTooltip, NModal, NSpace, useMessage, type MenuOption } from "naive-ui";
import { PhDatabase, PhHouse, PhMapPin, PhPen, PhArticle, PhTable, PhArrowsClockwise, PhWarning } from "@phosphor-icons/vue";
import { setLocale, type Locale } from "@/i18n";
import { fetchApi } from "@/composables/useApi";

const route = useRoute();
const router = useRouter();
const { t, locale } = useI18n();
const message = useMessage();

const showResetConfirm = ref(false);
const resetting = ref(false);

const activeKey = computed(() => {
  if (route.path === "/") return "dashboard";
  if (route.path.startsWith("/locations")) return "locations";
  if (route.path.startsWith("/writers")) return "writers";
  if (route.path.startsWith("/articles")) return "articles";
  if (route.path.startsWith("/schema")) return "schema";
  return "dashboard";
});

const menuOptions = computed<MenuOption[]>(() => [
  {
    label: t("nav.dashboard"),
    key: "dashboard",
    icon: () => h(PhHouse, { weight: "regular" }),
  },
  {
    label: t("nav.locations"),
    key: "locations",
    icon: () => h(PhMapPin, { weight: "regular" }),
  },
  {
    label: t("nav.writers"),
    key: "writers",
    icon: () => h(PhPen, { weight: "regular" }),
  },
  {
    label: t("nav.articles"),
    key: "articles",
    icon: () => h(PhArticle, { weight: "regular" }),
  },
  {
    label: t("nav.schema"),
    key: "schema",
    icon: () => h(PhTable, { weight: "regular" }),
  },
]);

function handleMenuClick(key: string) {
  const routes: Record<string, string> = {
    dashboard: "/",
    locations: "/locations",
    writers: "/writers",
    articles: "/articles",
    schema: "/schema",
  };
  router.push(routes[key] || "/");
}

function switchLocale(newLocale: Locale) {
  setLocale(newLocale);
}

async function resetDatabase() {
  resetting.value = true;
  try {
    await fetchApi('/api/db/seed', { method: 'POST' });
    message.success(t('header.resetSuccess'));
    showResetConfirm.value = false;
    // Refresh current page to show new data
    router.go(0);
  } catch (e) {
    message.error(t('common.error'));
  } finally {
    resetting.value = false;
  }
}
</script>

<style scoped>
.app-layout {
  min-height: 100vh;
}

.app-header {
  padding: 0 1.5rem;
  background: var(--n-card-color);
}

.header-content {
  display: flex;
  align-items: center;
  gap: 2rem;
  height: 56px;
}

.logo {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  color: var(--n-primary-color);
  text-decoration: none;
  transition: opacity 0.15s ease;
}

.logo:hover {
  opacity: 0.85;
}

.logo-text {
  font-family: 'Space Grotesk', sans-serif;
  font-size: 1.25rem;
  font-weight: 600;
  letter-spacing: 0.02em;
}

.app-content {
  padding: 1.5rem;
}

.header-spacer {
  flex: 1;
}
</style>
