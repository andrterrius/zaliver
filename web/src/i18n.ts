export type Locale = "ru" | "en";

const LOCALE_KEY = "zaliver_locale";

const dict = {
  ru: {
    loginTitle: "Вход",
    username: "Логин",
    password: "Пароль",
    signIn: "Войти",
    signOut: "Выйти",
    files: "Файлы на сервере",
    language: "Язык",
    account: "Аккаунт",
    settings: "Настройки",
    save: "Сохранить",
    saved: "Настройки сохранены.",
    processing: "Обработка видео",
    processingHint: "Файлы обрабатываются в 1 поток на пользователя.",
    browsersHint: "Не более 5 окон браузера одновременно на пользователя.",
    maxBrowsers: "Макс. параллельных браузеров",
    choosePlatform: "Выбор платформы",
    backPlatform: "← Выбор платформы",
    navUniquify: "Уникализация",
    navSlicing: "Нарезка",
    navStitching: "Склейка",
    navReady: "Готовые видео",
    navUploaded: "Залитые видео",
    navProfiles: "Профили",
    navChannels: "Редактирование каналов",
    navAi: "ИИ",
    navSettings: "Настройки",
    loginError: "Неверный логин или пароль",
    sessionExpired: "Сессия истекла — войдите снова",
    createUser: "Создать пользователя",
    deleteUser: "Удалить",
    deleteUserConfirm: "Удалить пользователя",
    users: "Пользователи",
    newPassword: "Новый пароль",
    localeRu: "Русский",
    localeEn: "English",
  },
  en: {
    loginTitle: "Sign in",
    username: "Username",
    password: "Password",
    signIn: "Sign in",
    signOut: "Sign out",
    files: "Server files",
    language: "Language",
    account: "Account",
    settings: "Settings",
    save: "Save",
    saved: "Settings saved.",
    processing: "Video processing",
    processingHint: "Files are processed in 1 thread per user.",
    browsersHint: "At most 5 browser windows per user at once.",
    maxBrowsers: "Max parallel browsers",
    choosePlatform: "Choose platform",
    backPlatform: "← Choose platform",
    navUniquify: "Uniquify",
    navSlicing: "Slicing",
    navStitching: "Stitching",
    navReady: "Ready videos",
    navUploaded: "Uploaded",
    navProfiles: "Profiles",
    navChannels: "Channel edit",
    navAi: "AI",
    navSettings: "Settings",
    loginError: "Invalid username or password",
    sessionExpired: "Session expired — sign in again",
    createUser: "Create user",
    deleteUser: "Delete",
    deleteUserConfirm: "Delete user",
    users: "Users",
    newPassword: "New password",
    localeRu: "Русский",
    localeEn: "English",
  },
} as const;

export type I18nKey = keyof typeof dict.ru;

export function getStoredLocale(): Locale {
  const raw = (localStorage.getItem(LOCALE_KEY) || "").toLowerCase();
  return raw === "en" ? "en" : "ru";
}

export function setStoredLocale(locale: Locale): void {
  localStorage.setItem(LOCALE_KEY, locale);
}

export function t(key: I18nKey, locale: Locale = getStoredLocale()): string {
  return dict[locale][key] || dict.ru[key] || key;
}
