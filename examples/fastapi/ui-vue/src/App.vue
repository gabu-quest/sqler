<template>
  <n-config-provider :theme="darkTheme" :theme-overrides="themeOverrides">
    <n-global-style />
    <n-message-provider>
      <n-notification-provider>
        <n-dialog-provider>
          <AppLayout>
            <router-view />
          </AppLayout>
        </n-dialog-provider>
      </n-notification-provider>
    </n-message-provider>
  </n-config-provider>
</template>

<script setup lang="ts">
import { darkTheme, NConfigProvider, NGlobalStyle, NMessageProvider, NNotificationProvider, NDialogProvider } from "naive-ui";
import { createDsNaiveThemeOverrides } from "@/design/naive-theme";
import { providePhosphorDefaults } from "@/design/icons";
import AppLayout from "@/components/AppLayout.vue";

providePhosphorDefaults();

const themeOverrides = createDsNaiveThemeOverrides("dark");
</script>
