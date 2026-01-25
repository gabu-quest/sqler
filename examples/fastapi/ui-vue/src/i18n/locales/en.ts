/**
 * English translations for SQLer Demo
 */
export default {
  // App & Navigation
  app: {
    title: 'SQLer Demo',
    subtitle: 'SQLite ORM for Python',
  },
  nav: {
    dashboard: 'Dashboard',
    locations: 'Locations',
    writers: 'Writers',
    articles: 'Articles',
  },

  // Common UI
  common: {
    create: 'Create',
    edit: 'Edit',
    delete: 'Delete',
    save: 'Save',
    cancel: 'Cancel',
    refresh: 'Refresh',
    search: 'Search',
    loading: 'Loading...',
    noData: 'No data',
    confirm: 'Confirm',
    success: 'Success',
    error: 'Error',
    actions: 'Actions',
    id: 'ID',
    version: 'Version',
    status: 'Status',
    active: 'Active',
    deleted: 'Deleted',
    all: 'All',
    auditLog: 'Audit Log',
    viewAuditLog: 'View History',
    noAuditLog: 'No changes recorded',
    fillRequired: 'Please fill in all required fields',
  },

  // Dashboard
  dashboard: {
    title: 'Dashboard',
    subtitle: 'SQLer Feature Demo',
    healthStatus: 'Status',
    healthy: 'Healthy',
    unhealthy: 'Unhealthy',
    latency: 'Latency',
    dbSize: 'DB Size',
    walMode: 'Journal',
    tableCount: 'Tables',
    indexCount: 'Indexes',
    cacheTitle: 'Query Cache',
    hitRate: 'Hit Rate',
    hits: 'Hits',
    misses: 'Misses',
    maintenance: 'Maintenance',
    vacuum: 'Vacuum',
    checkpoint: 'Checkpoint',
    vacuumComplete: 'Vacuum completed',
    checkpointComplete: 'Checkpoint completed',
    features: {
      locations: {
        title: 'Locations',
        desc: 'Countries & Cities with RefField cascade',
      },
      writers: {
        title: 'Writers',
        desc: 'Audit logging, City relationships',
      },
      articles: {
        title: 'Articles',
        desc: 'FTS5 search with BM25 ranking',
      },
    },
  },

  // Locations View
  locations: {
    title: 'Locations',
    countries: 'Countries',
    cities: 'Cities',
    createCountry: 'Add Country',
    createCity: 'Add City',
    editCountry: 'Edit Country',
    editCity: 'Edit City',
    countryName: 'Country Name',
    countryCode: 'Code',
    cityName: 'City Name',
    selectCountry: 'Select Country',
    citiesInCountry: 'Cities in {country}',
    noCities: 'No cities',
    cityCount: '{count} cities',
    confirmDeleteCountry: 'Delete this country?',
    confirmDeleteCity: 'Delete this city?',
    cannotDelete: 'Cannot delete',
    hasDependencies: 'Has dependencies that must be removed first',
    messages: {
      countryCreated: 'Country created',
      countryDeleted: 'Country deleted',
      cityCreated: 'City created',
      cityDeleted: 'City deleted',
    },
  },

  // Writers View
  writers: {
    title: 'Writers',
    createWriter: 'Add Writer',
    editWriter: 'Edit Writer',
    writerName: 'Name',
    writerBio: 'Bio',
    selectCity: 'Select City',
    noCity: 'No location',
    location: 'Location',
    articles: 'Articles',
    articleCount: '{count} articles',
    viewArticles: 'View Articles',
    confirmDelete: 'Delete this writer?',
    hasDependencies: 'Cannot delete - writer has articles',
    messages: {
      created: 'Writer created',
      updated: 'Writer updated',
      deleted: 'Writer deleted',
    },
  },

  // Articles View (FTS)
  articles: {
    title: 'Articles',
    createArticle: 'Add Article',
    editArticle: 'Edit Article',
    rebuildIndex: 'Rebuild FTS',
    searchPlaceholder: 'Search articles...',
    resultsFound: '{count} results for "{query}"',
    score: 'Score',
    writer: 'Writer',
    selectWriter: 'Select Writer',
    noWriter: 'No author',
    allArticles: 'All Articles',
    fields: {
      title: 'Title',
      content: 'Content',
      tags: 'Tags',
    },
    messages: {
      created: 'Article created',
      updated: 'Article updated',
      deleted: 'Article deleted',
      indexRebuilt: 'FTS index rebuilt',
    },
  },

  // Audit Log
  audit: {
    title: 'Change History',
    action: 'Action',
    timestamp: 'Time',
    changes: 'Changes',
    created: 'Created',
    updated: 'Updated',
    deleted: 'Deleted',
    field: 'Field',
    oldValue: 'Old',
    newValue: 'New',
  },
}
