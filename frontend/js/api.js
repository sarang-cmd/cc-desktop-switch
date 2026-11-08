(function () {
  'use strict';

  const BASE = '';
  const ADMIN_TOKEN_KEY = 'ccdsAdminToken';

  function readAdminToken() {
    const hash = window.location.hash || '';
    if (hash.startsWith('#')) {
      const params = new URLSearchParams(hash.slice(1));
      const token = params.get('token');
      if (token) {
        sessionStorage.setItem(ADMIN_TOKEN_KEY, token);
        params.delete('token');
        const nextHash = params.toString();
        const nextUrl = `${window.location.pathname}${window.location.search}${nextHash ? `#${nextHash}` : ''}`;
        window.history.replaceState(null, '', nextUrl || '/');
        return token;
      }
    }
    return sessionStorage.getItem(ADMIN_TOKEN_KEY) || '';
  }

  function requestHeaders() {
    const headers = { 'X-CCDS-Request': '1' };
    const token = readAdminToken();
    if (token) {
      headers['X-CCDS-Admin-Token'] = token;
    }
    return headers;
  }

  async function api(method, path, body) {
    const opts = { method, headers: requestHeaders() };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    const resp = await fetch(BASE + path, opts);
    const data = await resp.json();
    if (!resp.ok || data.success === false) {
      throw new Error(data.message || `Request failed: ${method} ${path}`);
    }
    return data;
  }

  async function apiResult(method, path, body) {
    const opts = { method, headers: requestHeaders() };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    const resp = await fetch(BASE + path, opts);
    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.message || `Request failed: ${method} ${path}`);
    }
    return data;
  }

  // ── Utilities ──
  const ICON_MAP = {
    deepseek: { logo: 'assets/providers/deepseek.svg' },
    kimi: { logo: 'assets/providers/kimi.svg' },
    moonshot: { logo: 'assets/providers/kimi.svg' },
    xiaomi: { logo: 'assets/providers/xiaomi-mimo.png' },
    mimo: { logo: 'assets/providers/xiaomi-mimo.png' },
    qiniu: { logo: 'assets/providers/qiniu.ico' },
    qnaigc: { logo: 'assets/providers/qiniu.ico' },
    zhipu: { logo: 'assets/providers/zhipu.png' },
    bigmodel: { logo: 'assets/providers/zhipu.png' },
    glm: { logo: 'assets/providers/zhipu.png' },
    siliconflow: { icon: 'bi-diagram-3-fill' },
    bailian: { logo: 'assets/providers/aliyun.ico' },
    dashscope: { logo: 'assets/providers/aliyun.ico' },
    aliyun: { logo: 'assets/providers/aliyun.ico' },
    openai: { logo: 'assets/providers/openai.svg' },
    groq: { logo: 'assets/providers/groq.svg' },
    mistral: { logo: 'assets/providers/mistral.svg' },
    sarvam: { icon: 'bi-translate', iconText: 'SV' },
    openrouter: { logo: 'assets/providers/openrouter.svg' },
    gemini: { logo: 'assets/providers/gemini.svg' },
    generativelanguage: { logo: 'assets/providers/gemini.svg' },
    xai: { icon: 'bi-slash-square-fill', iconText: 'X' },
    grok: { icon: 'bi-slash-square-fill', iconText: 'X' },
    together: { logo: 'assets/providers/together.svg' },
    fireworks: { logo: 'assets/providers/fireworks.svg' },
    cerebras: { logo: 'assets/providers/cerebras.svg' },
    perplexity: { logo: 'assets/providers/perplexity.svg' },
    cohere: { logo: 'assets/providers/cohere.svg' },
    ollama: { logo: 'assets/providers/ollama.svg' },
    anthropic: { logo: 'assets/providers/anthropic.svg' },
  };

  function computeIcon(provider) {
    const id = `${provider.id || ''} ${provider.name || ''} ${provider.baseUrl || ''}`.toLowerCase();
    for (const [key, val] of Object.entries(ICON_MAP)) {
      if (id.includes(key)) return val;
    }
    return { icon: 'bi-plug-fill' };
  }

  function mapProvider(provider, activeId) {
    const models = provider.models || {};
    const knownKeys = new Set(['default', 'opus', 'opus_4_7', 'opus_4_6', 'opus_3', 'sonnet', 'sonnet_4_6', 'sonnet_4_5', 'haiku', 'haiku_4_5']);
    const customMappings = {};
    for (const [key, value] of Object.entries(models)) {
      if (!knownKeys.has(key) && /^claude-[A-Za-z0-9][A-Za-z0-9._-]*$/.test(key) && typeof value === 'string' && value.trim()) {
        customMappings[key] = value.trim();
      }
    }
    return {
      id: provider.id,
      name: provider.name,
      baseUrl: provider.baseUrl,
      apiFormat: ['openai', 'openai_chat'].includes(provider.apiFormat) ? 'openai_chat' : (provider.apiFormat || 'anthropic'),
      authScheme: provider.authScheme || 'bearer',
      hasApiKey: !!provider.hasApiKey,
      extraHeaders: provider.extraHeaders || {},
      modelCapabilities: provider.modelCapabilities || {},
      requestOptions: provider.requestOptions || {},
      compactRequests: provider.compactRequests === true,
      compactSystemPromptLimit: Number(provider.compactSystemPromptLimit) || 4000,
      compactMessageLimit: Number(provider.compactMessageLimit) || 12,
      default: provider.id === activeId,
      isBuiltin: !!provider.isBuiltin,
      mappings: {
        default: models.default || '',
        opus_4_7: models.opus_4_7 || models.opus || '',
        opus_4_6: models.opus_4_6 || '',
        opus_3: models.opus_3 || '',
        sonnet_4_6: models.sonnet_4_6 || models.sonnet || '',
        sonnet_4_5: models.sonnet_4_5 || '',
        haiku_4_5: models.haiku_4_5 || models.haiku || '',
        ...customMappings,
      },
      ...computeIcon(provider),
    };
  }

  function providerBody(payload, includeModels = true) {
    const body = {
      name: payload.name,
      baseUrl: payload.baseUrl,
      authScheme: payload.authScheme || 'bearer',
      apiFormat: ['OpenAI', 'openai', 'openai_chat'].includes(payload.apiFormat) ? 'openai_chat' : 'anthropic',
      extraHeaders: payload.extraHeaders || {},
      modelCapabilities: payload.modelCapabilities || {},
      requestOptions: payload.requestOptions || {},
      compactRequests: payload.compactRequests === true,
      compactSystemPromptLimit: Number(payload.compactSystemPromptLimit) || 4000,
      compactMessageLimit: Number(payload.compactMessageLimit) || 12,
    };
    if (payload.apiKey) {
      body.apiKey = payload.apiKey;
    }
    if (includeModels) {
      body.models = payload.models || {};
    }
    return body;
  }

  function mapLog(log) {
    return {
      at: log.time,
      level: log.level.toLowerCase(),
      message: log.message,
    };
  }

  // ── Public API ──
  window.CCApi = {
    async getStatus() {
      const data = await api('GET', '/api/status');
      const active = data.activeProvider;
      return {
        desktopConfigured: !!data.desktopConfigured,
        proxyRunning: !!data.proxyRunning,
        proxyPort: data.proxyPort || 18080,
        activeProvider: active ? { name: active.name, id: active.id } : { name: '-', id: null },
        activeProviderId: data.activeProviderId,
        desktopHealth: data.desktopHealth || { needsApply: false, issues: [] },
        exposeAllProviderModels: !!data.exposeAllProviderModels,
      };
    },

    async getProviders() {
      const data = await api('GET', '/api/providers');
      return (data.providers || []).map(p => mapProvider(p, data.activeId));
    },

    async getProviderSecret(id) {
      return api('GET', `/api/providers/${encodeURIComponent(id)}/secret`);
    },

    async getPresets() {
      const data = await api('GET', '/api/presets');
      return (data.presets || []).map(p => ({
        id: p.id,
        name: p.name,
        baseUrl: p.baseUrl,
        apiFormat: ['openai', 'openai_chat'].includes(p.apiFormat) ? 'OpenAI' : 'Anthropic',
        authScheme: p.authScheme || 'bearer',
        models: p.models || {},
        modelOptions: p.modelOptions || {},
        baseUrlOptions: p.baseUrlOptions || [],
        baseUrlHint: p.baseUrlHint || '',
        requestOptionPresets: p.requestOptionPresets || {},
        extraHeaders: p.extraHeaders || {},
        modelCapabilities: p.modelCapabilities || {},
        requestOptions: p.requestOptions || {},
        ...computeIcon(p),
      }));
    },

    async addProvider(payload) {
      const data = await api('POST', '/api/providers', providerBody(payload));
      return data.provider || data;
    },

    async updateProvider(id, payload) {
      const data = await api('PUT', `/api/providers/${encodeURIComponent(id)}`, providerBody(payload));
      return data.provider || data;
    },

    async deleteProvider(id) {
      return api('DELETE', `/api/providers/${encodeURIComponent(id)}`);
    },

    async detectApiFormat(baseUrl, apiKey) {
      return api('POST', '/api/providers/detect-format', { baseUrl, apiKey });
    },

    async setDefaultProvider(id) {
      return api('PUT', `/api/providers/${encodeURIComponent(id)}/default`);
    },

    async reorderProviders(providerIds) {
      return api('PUT', '/api/providers/reorder', { providerIds });
    },

    async testProvider(id) {
      return api('POST', `/api/providers/${encodeURIComponent(id)}/test`);
    },

    async queryProviderUsage(id) {
      return api('POST', `/api/providers/${encodeURIComponent(id)}/usage`);
    },

    async getProviderCompatibility() {
      return api('GET', '/api/providers/compatibility');
    },

    async testProviderPayload(payload) {
      return api('POST', '/api/providers/test', providerBody(payload, false));
    },

    async saveModelMappings(id, mappings) {
      return api('PUT', `/api/providers/${encodeURIComponent(id)}/models`, { models: mappings });
    },

    async fetchProviderModels(id) {
      return api('GET', `/api/providers/${encodeURIComponent(id)}/models/available`);
    },

    async fetchProviderModelsPayload(payload) {
      return api('POST', '/api/providers/models/available', providerBody(payload, false));
    },

    async autofillProviderModels(id) {
      return api('POST', `/api/providers/${encodeURIComponent(id)}/models/autofill`);
    },

    async getDesktopStatus() {
      const data = await api('GET', '/api/desktop/status');
      const registryConfig = data.keys || {};
      return {
        configured: !!data.configured,
        message: data.message || '',
        sources: data.sources || {},
        activeSource: data.activeSource || '',
        keySources: data.keySources || {},
        exists: data.exists,
        health: data.health || { needsApply: false, issues: [] },
        config: {
          inferenceProvider: registryConfig.inferenceProvider || '',
          inferenceGatewayBaseUrl: registryConfig.inferenceGatewayBaseUrl || '',
          inferenceGatewayApiKey: registryConfig.inferenceGatewayApiKey ? '******' : '',
          inferenceGatewayAuthScheme: registryConfig.inferenceGatewayAuthScheme || '',
          inferenceModels: registryConfig.inferenceModels || '',
        },
      };
    },

    async configureDesktop() {
      return apiResult('POST', '/api/desktop/configure');
    },

    async clearDesktop() {
      await api('POST', '/api/desktop/clear');
      return this.getDesktopStatus();
    },

    async startProxy(port) {
      if (port) {
        await this.saveSettings({ proxyPort: Number(port) });
      }
      await api('POST', '/api/proxy/start', port ? { port: Number(port) } : undefined);
      const status = await api('GET', '/api/status');
      return {
        running: !!status.proxyRunning,
        port: status.proxyPort || port || 18080,
      };
    },

    async stopProxy() {
      await api('POST', '/api/proxy/stop');
      return { running: false };
    },

    async getProxyLogs() {
      const data = await api('GET', '/api/proxy/logs');
      return (data.logs || []).map(mapLog);
    },

    async getProxyStatus() {
      const data = await api('GET', '/api/proxy/status');
      return {
        running: !!data.running,
        port: data.port || 18080,
        stats: data.stats || { total: 0, success: 0, failed: 0, today: 0 },
      };
    },

    async clearLogs() {
      return api('POST', '/api/proxy/logs/clear');
    },

    async getDiagnosticsSummary() {
      return api('GET', '/api/diagnostics/summary');
    },

    async exportDiagnostics() {
      return api('POST', '/api/diagnostics/export');
    },

    async checkDiagnostics() {
      return api('POST', '/api/diagnostics/check');
    },

    async getSettings() {
      return api('GET', '/api/settings');
    },

    async saveSettings(settings) {
      const data = await api('PUT', '/api/settings', settings);
      return data.settings || data;
    },

    async detectLocalProxy() {
      const data = await api('GET', '/api/proxy/detect');
      return data.detected || '';
    },

    async checkModelAvailability(providerId, model) {
      const data = await api('POST', `/api/providers/${encodeURIComponent(providerId)}/models/${encodeURIComponent(model)}/check`);
      return data;
    },

    async checkUpdate(updateUrl) {
      const params = new URLSearchParams();
      if (updateUrl) params.set('url', updateUrl);
      return api('GET', `/api/update/check?${params.toString()}`);
    },

    async installUpdate(updateUrl) {
      return api('POST', '/api/update/install', updateUrl ? { url: updateUrl } : {});
    },

    async getUpdateProgress() {
      return api('GET', '/api/update/progress');
    },

    async createBackup() {
      return api('POST', '/api/config/backup');
    },

    async listBackups() {
      const data = await api('GET', '/api/config/backups');
      return data.backups || [];
    },

    async exportConfig() {
      return api('GET', '/api/config/export');
    },

    async importConfig(configData) {
      return api('POST', '/api/config/import', configData);
    },

    async getCcSwitchStatus() {
      return api('GET', '/api/ccswitch/status');
    },

    async getCcSwitchProviders() {
      return api('GET', '/api/ccswitch/providers');
    },

    async importCcSwitchProviders(ids, setDefault = false) {
      return api('POST', '/api/ccswitch/import', { ids, setDefault });
    },

    async getActivities() {
      const data = await api('GET', '/api/proxy/logs');
      const logs = data.logs || [];
      return logs.slice(-5).reverse().map(log => ({
        time: log.time,
        text: log.message,
      }));
    },
  };
})();
