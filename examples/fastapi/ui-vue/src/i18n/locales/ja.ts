/**
 * Japanese translations for SQLer Demo
 * SQLerデモの日本語翻訳
 */
export default {
  // App & Navigation
  app: {
    title: 'SQLer デモ',
    subtitle: 'Python用SQLite ORM',
  },
  nav: {
    dashboard: 'ダッシュボード',
    locations: '地域',
    writers: 'ライター',
    articles: '記事',
  },

  // Common UI
  common: {
    create: '作成',
    edit: '編集',
    delete: '削除',
    save: '保存',
    cancel: 'キャンセル',
    refresh: '更新',
    search: '検索',
    loading: '読み込み中...',
    noData: 'データなし',
    confirm: '確認',
    success: '成功',
    error: 'エラー',
    actions: '操作',
    id: 'ID',
    version: 'バージョン',
    status: 'ステータス',
    active: 'アクティブ',
    deleted: '削除済み',
    all: 'すべて',
    auditLog: '監査ログ',
    viewAuditLog: '履歴を見る',
    noAuditLog: '変更履歴なし',
    fillRequired: '必須項目を入力してください',
  },

  // Dashboard
  dashboard: {
    title: 'ダッシュボード',
    subtitle: 'SQLer機能デモ',
    healthStatus: 'ステータス',
    healthy: '正常',
    unhealthy: '異常',
    latency: 'レイテンシ',
    dbSize: 'DBサイズ',
    walMode: 'ジャーナル',
    tableCount: 'テーブル',
    indexCount: 'インデックス',
    cacheTitle: 'クエリキャッシュ',
    hitRate: 'ヒット率',
    hits: 'ヒット',
    misses: 'ミス',
    maintenance: 'メンテナンス',
    vacuum: 'Vacuum',
    checkpoint: 'チェックポイント',
    vacuumComplete: 'Vacuumが完了しました',
    checkpointComplete: 'チェックポイントが完了しました',
    features: {
      locations: {
        title: '地域',
        desc: '国・都市のRefFieldカスケード',
      },
      writers: {
        title: 'ライター',
        desc: '監査ログ、都市リレーション',
      },
      articles: {
        title: '記事',
        desc: 'FTS5検索とBM25ランキング',
      },
    },
  },

  // Locations View
  locations: {
    title: '地域',
    countries: '国',
    cities: '都市',
    createCountry: '国を追加',
    createCity: '都市を追加',
    editCountry: '国を編集',
    editCity: '都市を編集',
    countryName: '国名',
    countryCode: 'コード',
    cityName: '都市名',
    selectCountry: '国を選択',
    citiesInCountry: '{country}の都市',
    noCities: '都市なし',
    cityCount: '{count}都市',
    confirmDeleteCountry: 'この国を削除しますか？',
    confirmDeleteCity: 'この都市を削除しますか？',
    cannotDelete: '削除できません',
    hasDependencies: '先に依存関係を削除してください',
    messages: {
      countryCreated: '国を作成しました',
      countryDeleted: '国を削除しました',
      cityCreated: '都市を作成しました',
      cityDeleted: '都市を削除しました',
    },
  },

  // Writers View
  writers: {
    title: 'ライター',
    createWriter: 'ライターを追加',
    editWriter: 'ライターを編集',
    writerName: '名前',
    writerBio: '自己紹介',
    selectCity: '都市を選択',
    noCity: '場所なし',
    location: '場所',
    articles: '記事',
    articleCount: '{count}件の記事',
    viewArticles: '記事を見る',
    confirmDelete: 'このライターを削除しますか？',
    hasDependencies: '削除できません - ライターに記事があります',
    messages: {
      created: 'ライターを作成しました',
      updated: 'ライターを更新しました',
      deleted: 'ライターを削除しました',
    },
  },

  // Articles View (FTS)
  articles: {
    title: '記事',
    createArticle: '記事を追加',
    editArticle: '記事を編集',
    rebuildIndex: 'FTS再構築',
    searchPlaceholder: '記事を検索...',
    resultsFound: '「{query}」で{count}件',
    score: 'スコア',
    writer: 'ライター',
    selectWriter: 'ライターを選択',
    noWriter: '著者なし',
    allArticles: 'すべての記事',
    fields: {
      title: 'タイトル',
      content: '本文',
      tags: 'タグ',
    },
    messages: {
      created: '記事を作成しました',
      updated: '記事を更新しました',
      deleted: '記事を削除しました',
      indexRebuilt: 'FTSインデックスを再構築しました',
    },
  },

  // Audit Log
  audit: {
    title: '変更履歴',
    action: '操作',
    timestamp: '日時',
    changes: '変更内容',
    created: '作成',
    updated: '更新',
    deleted: '削除',
    field: 'フィールド',
    oldValue: '変更前',
    newValue: '変更後',
  },
}
