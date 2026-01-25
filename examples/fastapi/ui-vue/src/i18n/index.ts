/**
 * i18n configuration for SQLer Demo
 * Supports English (en) and Japanese (ja)
 */
import { createI18n } from 'vue-i18n'
import en from './locales/en'
import ja from './locales/ja'

// Get saved locale or default to browser preference
function getDefaultLocale(): 'en' | 'ja' {
  const saved = localStorage.getItem('sqler-demo-locale')
  if (saved === 'en' || saved === 'ja') return saved

  // Check browser language
  const browserLang = navigator.language.toLowerCase()
  if (browserLang.startsWith('ja')) return 'ja'
  return 'en'
}

export const i18n = createI18n({
  legacy: false, // Use Composition API mode
  locale: getDefaultLocale(),
  fallbackLocale: 'en',
  messages: {
    en,
    ja,
  },
})

// Helper to save locale preference
export function setLocale(locale: 'en' | 'ja') {
  i18n.global.locale.value = locale
  localStorage.setItem('sqler-demo-locale', locale)
  document.documentElement.lang = locale
}

// Export type for locale
export type Locale = 'en' | 'ja'

export default i18n
