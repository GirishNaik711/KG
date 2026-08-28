/**
 * RAPIDS Configuration Loader
 *
 * Loads rapids-config.json and provides utilities for resolving paths,
 * fetching artifacts, and managing auto-load behavior.
 *
 * Usage:
 *   const loader = new RAPIDSLoader();
 *   await loader.init();
 *   const dagData = await loader.loadDAG();
 */

class RAPIDSLoader {
  constructor(configPath = './rapids-config.json') {
    this.configPath = configPath;
    this.config = null;
    this.baseDir = this.getBaseDir(configPath);
  }

  /**
   * Extract base directory from config path
   */
  getBaseDir(path) {
    const parts = path.split('/');
    parts.pop(); // Remove filename
    return parts.join('/') || '.';
  }

  /**
   * Load and parse configuration file
   */
  async init() {
    try {
      const response = await fetch(this.configPath);
      if (!response.ok) {
        throw new Error(`Config not found: ${this.configPath} (${response.status})`);
      }
      this.config = await response.json();
      console.log('[RAPIDS Loader] Config loaded:', this.config.project?.name || 'Unnamed');
      return this.config;
    } catch (err) {
      console.warn('[RAPIDS Loader] Config load failed:', err.message);
      console.warn('[RAPIDS Loader] Falling back to manual file selection mode');
      return null;
    }
  }

  /**
   * Check if config loaded successfully
   */
  isConfigured() {
    return this.config !== null;
  }

  /**
   * Check if upload gate should be shown
   */
  shouldShowUploadGate() {
    if (!this.config) return true;
    return this.config.ui?.show_upload_gate !== false;
  }

  /**
   * Resolve path template with placeholders
   */
  resolvePath(template, extras = {}) {
    if (!this.config) return null;

    let resolved = template;
    const project = this.config.project || {};

    // Replace placeholders
    const replacements = {
      workspace_root: project.workspace_root || '',
      active_workspace: project.active_workspace || '',
      active_project: project.active_project || '',
      ...extras
    };

    Object.entries(replacements).forEach(([key, value]) => {
      const placeholder = `{${key}}`;
      resolved = resolved.replace(new RegExp(placeholder, 'g'), value);
    });

    // Absolute paths (starting with /) resolve from server root
    if (resolved.startsWith('/')) {
      return resolved;
    }

    // Relative paths resolve from config base directory
    if (resolved.startsWith('../') || resolved.startsWith('./')) {
      return `${this.baseDir}/${resolved}`;
    }

    // Plain paths without leading slash or ./ are treated as relative
    return resolved;
  }

  /**
   * Get resolved path for a specific artifact
   */
  getPath(artifactKey, extras = {}) {
    if (!this.config?.paths?.[artifactKey]) return null;

    const pathConfig = this.config.paths[artifactKey];

    // Handle string shorthand
    if (typeof pathConfig === 'string') {
      return this.resolvePath(pathConfig, extras);
    }

    // Handle object with file/dir
    const template = pathConfig.file || pathConfig.dir;
    if (!template) return null;

    return this.resolvePath(template, extras);
  }

  /**
   * Check if artifact should auto-load
   */
  shouldAutoLoad(artifactKey) {
    if (!this.config?.paths?.[artifactKey]) return false;
    const pathConfig = this.config.paths[artifactKey];
    if (typeof pathConfig === 'object') {
      return pathConfig.auto_load === true;
    }
    return false;
  }

  /**
   * Enhanced YAML parser for RAPIDS artifacts
   * Handles nested objects for phase plans
   */
  parseSimpleYAML(text) {
    const lines = text.split('\n');
    const result = {};
    let currentObject = result;
    let stack = [result];
    let prevIndent = 0;

    for (let line of lines) {
      // Skip comments and empty lines
      if (line.trim().startsWith('#') || line.trim() === '') continue;

      const trimmed = line.trim();
      const leadingSpaces = line.search(/\S/);

      // Handle indentation changes
      if (leadingSpaces < prevIndent) {
        // Pop back to correct level
        const levels = Math.floor((prevIndent - leadingSpaces) / 2);
        for (let i = 0; i < levels && stack.length > 1; i++) {
          stack.pop();
        }
        currentObject = stack[stack.length - 1];
      }

      // List item
      if (trimmed.startsWith('- ')) {
        const value = trimmed.substring(2).trim();
        if (!Array.isArray(currentObject)) {
          // Convert to array if needed
          const lastKey = Object.keys(stack[stack.length - 2]).pop();
          if (lastKey) {
            stack[stack.length - 2][lastKey] = [value];
          }
        } else {
          currentObject.push(value);
        }
        prevIndent = leadingSpaces;
        continue;
      }

      // Key-value pair
      if (trimmed.includes(':')) {
        const colonIndex = trimmed.indexOf(':');
        const key = trimmed.substring(0, colonIndex).trim();
        let value = trimmed.substring(colonIndex + 1).trim();

        // Remove quotes if present
        if ((value.startsWith('"') && value.endsWith('"')) ||
            (value.startsWith("'") && value.endsWith("'"))) {
          value = value.slice(1, -1);
        }

        if (value === '' || value === '[]' || value === '{}') {
          // Start of nested object
          currentObject[key] = {};
          stack.push(currentObject[key]);
          currentObject = currentObject[key];
        } else {
          // Simple key-value
          currentObject[key] = value;
        }

        prevIndent = leadingSpaces;
      }
    }

    return result;
  }

  /**
   * Fetch file from resolved path
   */
  async fetchFile(path, parseAsYAML = false) {
    if (!path) {
      throw new Error('Path is null or undefined');
    }

    try {
      const response = await fetch(path);
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }

      if (parseAsYAML || path.endsWith('.yaml') || path.endsWith('.yml')) {
        const text = await response.text();
        return this.parseSimpleYAML(text);
      }

      return await response.json();
    } catch (err) {
      console.error(`[RAPIDS Loader] Failed to fetch ${path}:`, err.message);
      throw err;
    }
  }

  /**
   * Load DAG file
   */
  async loadDAG(wave = null) {
    const path = this.getPath('dag', { wave });
    if (!path) {
      throw new Error('DAG path not configured');
    }
    console.log('[RAPIDS Loader] Loading DAG from:', path);
    return await this.fetchFile(path);
  }

  /**
   * Load waves file
   */
  async loadWaves() {
    const path = this.getPath('waves');
    if (!path) {
      throw new Error('Waves path not configured');
    }
    console.log('[RAPIDS Loader] Loading waves from:', path);
    return await this.fetchFile(path);
  }

  /**
   * Load feature list
   */
  async loadFeatures() {
    const path = this.getPath('features');
    if (!path) {
      throw new Error('Features path not configured');
    }
    console.log('[RAPIDS Loader] Loading features from:', path);
    return await this.fetchFile(path);
  }

  /**
   * Load manifest (YAML)
   */
  async loadManifest() {
    const path = this.getPath('manifest');
    if (!path) {
      throw new Error('Manifest path not configured');
    }
    console.log('[RAPIDS Loader] Loading manifest from:', path);
    return await this.fetchFile(path, true);
  }

  /**
   * Load intake file
   */
  async loadIntake() {
    const path = this.getPath('intake');
    if (!path) {
      throw new Error('Intake path not configured');
    }
    console.log('[RAPIDS Loader] Loading intake from:', path);
    return await this.fetchFile(path);
  }

  /**
   * Load phase plan (YAML)
   */
  async loadPhasePlan() {
    const path = this.getPath('phase_plan');
    if (!path) {
      throw new Error('Phase plan path not configured');
    }
    console.log('[RAPIDS Loader] Loading phase plan from:', path);
    return await this.fetchFile(path, true);
  }

  /**
   * Load progress file
   */
  async loadProgress() {
    const path = this.getPath('progress');
    if (!path) {
      throw new Error('Progress path not configured');
    }
    console.log('[RAPIDS Loader] Loading progress from:', path);
    return await this.fetchFile(path);
  }

  /**
   * Load iteration history
   */
  async loadIterationHistory() {
    const path = this.getPath('iteration_history');
    if (!path) {
      throw new Error('Iteration history path not configured');
    }
    console.log('[RAPIDS Loader] Loading iteration history from:', path);
    return await this.fetchFile(path, true); // Parse as YAML
  }

  /**
   * Load project brief markdown
   */
  async loadProjectBrief() {
    const path = this.getPath('project_brief');
    if (!path) {
      throw new Error('Project brief path not configured');
    }
    console.log('[RAPIDS Loader] Loading project brief from:', path);
    const response = await fetch(path);
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}: ${response.statusText}`);
    }
    return await response.text();
  }

  /**
   * Load subagent learnings (Layer 2 knowledge)
   */
  async loadSubagentLearnings() {
    const path = this.getPath('subagent_learnings');
    if (!path) {
      throw new Error('Subagent learnings path not configured');
    }
    console.log('[RAPIDS Loader] Loading subagent learnings from:', path);
    return await this.fetchFile(path);
  }

  /**
   * Load brownfield artifact by key
   */
  async loadBrownfieldArtifact(artifactKey) {
    if (!this.config?.paths?.brownfield?.[artifactKey]) {
      throw new Error(`Brownfield artifact '${artifactKey}' not configured`);
    }
    const path = this.resolvePath(this.config.paths.brownfield[artifactKey]);
    console.log(`[RAPIDS Loader] Loading brownfield ${artifactKey} from:`, path);
    const response = await fetch(path);
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}: ${response.statusText}`);
    }

    // Parse JSON if file extension is .json
    if (path.endsWith('.json')) {
      return await response.json();
    }
    return await response.text();
  }

  /**
   * Load checkpoint results for a wave
   */
  async loadCheckpointResults(wave = 0) {
    const dir = this.getPath('checkpoint_results');
    if (!dir) {
      throw new Error('Checkpoint results path not configured');
    }
    const path = `${dir}/wave-${wave}-system-checkpoint.json`;
    console.log('[RAPIDS Loader] Loading checkpoint results from:', path);
    return await this.fetchFile(path);
  }

  /**
   * Load system feedback for a wave
   */
  async loadSystemFeedback(wave = 0) {
    const template = this.config?.paths?.feedback?.system;
    if (!template) {
      throw new Error('System feedback path not configured');
    }
    const path = this.resolvePath(template, { wave });
    console.log('[RAPIDS Loader] Loading system feedback from:', path);
    return await this.fetchFile(path);
  }

  /**
   * Load user feedback for a wave
   */
  async loadUserFeedback(wave = 0) {
    const template = this.config?.paths?.feedback?.user;
    if (!template) {
      throw new Error('User feedback path not configured');
    }
    const path = this.resolvePath(template, { wave });
    console.log('[RAPIDS Loader] Loading user feedback from:', path);
    return await this.fetchFile(path);
  }

  /**
   * Auto-load all artifacts marked with auto_load: true
   */
  async autoLoadAll() {
    const results = {};
    const errors = {};

    if (!this.config) {
      console.warn('[RAPIDS Loader] No config loaded, skipping auto-load');
      return { results, errors };
    }

    for (const [key, pathConfig] of Object.entries(this.config.paths || {})) {
      if (this.shouldAutoLoad(key)) {
        try {
          console.log(`[RAPIDS Loader] Auto-loading ${key}...`);
          switch (key) {
            case 'dag':
              results.dag = await this.loadDAG();
              break;
            case 'waves':
              results.waves = await this.loadWaves();
              break;
            case 'features':
              results.features = await this.loadFeatures();
              break;
            case 'progress':
              results.progress = await this.loadProgress();
              break;
            case 'manifest':
              results.manifest = await this.loadManifest();
              break;
            case 'intake':
              results.intake = await this.loadIntake();
              break;
            case 'phase_plan':
              results.phase_plan = await this.loadPhasePlan();
              break;
            case 'project_brief':
              results.project_brief = await this.loadProjectBrief();
              break;
            case 'subagent_learnings':
              results.subagent_learnings = await this.loadSubagentLearnings();
              break;
            case 'iteration_history':
              results.iteration_history = await this.loadIterationHistory();
              break;
            default:
              console.warn(`[RAPIDS Loader] Unknown artifact key: ${key}`);
          }
        } catch (err) {
          console.error(`[RAPIDS Loader] Auto-load failed for ${key}:`, err.message);
          errors[key] = err.message;
        }
      }
    }

    return { results, errors };
  }

  /**
   * Get project metadata from config
   */
  getProjectInfo() {
    if (!this.config) return null;
    return {
      name: this.config.project?.name || 'Unknown Project',
      workspace: this.config.project?.active_workspace || 'Unknown',
      project: this.config.project?.active_project || 'Unknown',
      workspaceRoot: this.config.project?.workspace_root || ''
    };
  }

  /**
   * Get project type from loaded manifest
   * Returns null if manifest not loaded, otherwise 'greenfield' or 'brownfield'
   */
  getProjectType(manifestData) {
    if (!manifestData) return null;
    return manifestData.project_type || null;
  }

  /**
   * Get UI configuration
   */
  getUIConfig() {
    if (!this.config) return {};
    return this.config.ui || {};
  }

  /**
   * Check if a feature is enabled
   */
  isFeatureEnabled(featureKey) {
    if (!this.config?.features?.[featureKey]) return false;
    return this.config.features[featureKey].enabled === true;
  }

  /**
   * Create a status report for debugging
   */
  getStatus() {
    if (!this.config) {
      return {
        configured: false,
        message: 'Config not loaded'
      };
    }

    const autoLoadArtifacts = Object.entries(this.config.paths || {})
      .filter(([key, val]) => typeof val === 'object' && val.auto_load === true)
      .map(([key]) => key);

    return {
      configured: true,
      projectName: this.config.project?.name,
      workspace: this.config.project?.active_workspace,
      project: this.config.project?.active_project,
      autoLoadArtifacts,
      showUploadGate: this.shouldShowUploadGate(),
      theme: this.config.ui?.theme || 'light',
      enabledFeatures: Object.entries(this.config.features || {})
        .filter(([, val]) => val.enabled === true)
        .map(([key]) => key)
    };
  }
}

// Export for use in other scripts
if (typeof module !== 'undefined' && module.exports) {
  module.exports = RAPIDSLoader;
}
