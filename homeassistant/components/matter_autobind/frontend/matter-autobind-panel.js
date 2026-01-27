// frontend/matter-autobind-panel.js

class MatterAutoBindPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._automations = [];
    this._loading = true;
    this._error = null;
    this._selectedAutomation = null;
    this._automationDetail = null;
    // Tab state: "automations" or "debug"
    this._activeTab = "automations";
    // Debug panel state
    this._debugEnabled = false;
    this._matterDevices = [];
    this._selectedDevice = null;
    this._deviceRawData = null;
    this._deviceLoading = false;
    // Confirmation dialog state
    this._confirmDialog = null;
  }

  set hass(hass) {
    const firstLoad = this._hass === null;
    this._hass = hass;

    if (firstLoad) {
      this._initialize();
    }
  }

  get hass() {
    return this._hass;
  }

  async _initialize() {
    await this._loadDebugConfig();
    this._render();
    this._attachEventListeners();
    await this._loadDashboardData();
  }

  async _loadDebugConfig() {
    try {
      const result = await this._callService(
        "matter_autobind/get_debug_config",
        {},
      );
      this._debugEnabled = result.debug_enabled || false;
    } catch {
      this._debugEnabled = false;
    }
  }

  _attachEventListeners() {
    // Use event delegation on the shadow root
    this.shadowRoot.addEventListener("click", (e) => {
      const target = e.target.closest("[data-action]");
      if (!target) return;

      const action = target.dataset.action;
      const automationId = target.dataset.automationId;
      const nodeId = target.dataset.nodeId
        ? parseInt(target.dataset.nodeId, 10)
        : null;

      switch (action) {
        case "refresh":
          this._loadDashboardData();
          break;
        case "reconcile-all":
          this._forceReconcile();
          break;
        case "reconcile-single":
          this._forceReconcile(automationId);
          break;
        case "select-automation":
          this._toggleAutomationDetail(automationId);
          break;
        case "tab-automations":
          this._activeTab = "automations";
          this._render();
          break;
        case "tab-debug":
          this._activeTab = "debug";
          this._loadMatterDevices();
          break;
        case "select-device":
          if (nodeId !== null) {
            this._loadDeviceRawData(nodeId);
          }
          break;
        case "refresh-devices":
          this._loadMatterDevices();
          break;
        case "refresh-device-data":
          if (this._selectedDevice) {
            this._loadDeviceRawData(this._selectedDevice);
          }
          break;
        case "delete-acl":
          this._showDeleteConfirmation("acl", {
            node_id: nodeId,
            acl_index: parseInt(target.dataset.index, 10),
          });
          break;
        case "delete-binding":
          this._showDeleteConfirmation("binding", {
            node_id: nodeId,
            endpoint: parseInt(target.dataset.endpoint, 10),
            binding_index: parseInt(target.dataset.index, 10),
          });
          break;
        case "delete-group":
          this._showDeleteConfirmation("group", {
            node_id: nodeId,
            endpoint: parseInt(target.dataset.endpoint, 10),
            group_id: parseInt(target.dataset.groupId, 10),
          });
          break;
        case "delete-gkm":
          this._showDeleteConfirmation("group_key_map", {
            node_id: nodeId,
            entry_index: parseInt(target.dataset.index, 10),
          });
          break;
        case "delete-gks":
          this._showDeleteConfirmation("group_key_set", {
            node_id: nodeId,
            key_set_id: parseInt(target.dataset.keySetId, 10),
          });
          break;
        case "confirm-delete":
          this._executeDelete();
          break;
        case "cancel-delete":
          this._confirmDialog = null;
          this._render();
          break;
      }
    });

    // Handle preference dropdown changes
    this.shadowRoot.addEventListener("change", (e) => {
      if (e.target.id === "binding-preference") {
        const automationId = e.target.dataset.automationId;
        const preference = e.target.value;
        this._setBindingPreference(automationId, preference);
      }
    });
  }

  _toggleAutomationDetail(automationId) {
    // If already selected, deselect (collapse)
    if (this._selectedAutomation === automationId) {
      this._selectedAutomation = null;
      this._automationDetail = null;
      this._render();
    } else {
      // Otherwise, load the detail
      this._loadAutomationDetail(automationId);
    }
  }

  async _loadDashboardData() {
    this._loading = true;
    this._error = null;
    this._render();

    try {
      const result = await this._callService(
        "matter_autobind/get_dashboard_data",
        {},
      );
      this._automations = result.automations || [];
      this._loading = false;
    } catch (err) {
      this._error = err.message || "Failed to load data";
      this._loading = false;
    }
    this._render();
  }

  async _loadAutomationDetail(automationId) {
    this._selectedAutomation = automationId;
    this._automationDetail = null;
    this._render();

    try {
      const result = await this._callService(
        "matter_autobind/get_automation_detail",
        {
          automation_id: automationId,
        },
      );
      this._automationDetail = result;
    } catch (err) {
      this._automationDetail = { error: err.message };
    }
    this._render();
  }

  async _forceReconcile(automationId = null) {
    try {
      const params = automationId ? { automation_id: automationId } : {};
      await this._callService("matter_autobind/force_reconcile", params);
      // Refresh data after reconcile
      await this._loadDashboardData();
      if (automationId && this._selectedAutomation === automationId) {
        await this._loadAutomationDetail(automationId);
      }
    } catch (err) {
      alert("Reconcile failed: " + err.message);
    }
  }

  async _setBindingPreference(automationId, preference) {
    try {
      await this._callService("matter_autobind/set_binding_preference", {
        automation_id: automationId,
        preference: preference,
      });
      // Refresh data after preference change (reconciliation happens automatically)
      await this._loadDashboardData();
      if (this._selectedAutomation === automationId) {
        await this._loadAutomationDetail(automationId);
      }
    } catch (err) {
      alert("Failed to set binding preference: " + err.message);
    }
  }

  async _loadMatterDevices() {
    this._matterDevices = [];
    this._selectedDevice = null;
    this._deviceRawData = null;
    this._render();

    try {
      const result = await this._callService(
        "matter_autobind/get_matter_devices",
        {},
      );
      this._matterDevices = result.devices || [];
    } catch (err) {
      console.error("Failed to load Matter devices:", err);
    }
    this._render();
  }

  async _loadDeviceRawData(nodeId, forceRefresh = false) {
    this._selectedDevice = nodeId;
    this._deviceRawData = null;
    this._deviceLoading = true;
    this._render();

    try {
      const result = await this._callService(
        "matter_autobind/get_device_raw_data",
        { node_id: nodeId, force_refresh: forceRefresh },
      );
      this._deviceRawData = result;
      this._deviceLoading = false;
      // Show warning if we got cached data when refresh was requested
      if (forceRefresh && result.from_cache) {
        console.warn(`Node ${nodeId} may be offline - showing cached data`);
      }
    } catch (err) {
      this._deviceRawData = {
        error: err.message || "Failed to load device data",
      };
      this._deviceLoading = false;
    }
    this._render();
  }

  _showDeleteConfirmation(type, params) {
    let message = "";
    switch (type) {
      case "acl":
        message = `Delete ACL entry at index ${params.acl_index} from node ${params.node_id}?`;
        break;
      case "binding":
        message = `Delete binding at index ${params.binding_index} (endpoint ${params.endpoint}) from node ${params.node_id}?`;
        break;
      case "group":
        message = `Remove group ${params.group_id} (endpoint ${params.endpoint}) from node ${params.node_id}?`;
        break;
      case "group_key_map":
        message = `Delete GroupKeyMap entry at index ${params.entry_index} from node ${params.node_id}?`;
        break;
      case "group_key_set":
        message = `Delete GroupKeySet ${params.key_set_id} from node ${params.node_id}?`;
        break;
    }
    this._confirmDialog = { type, params, message };
    this._render();
  }

  async _executeDelete() {
    if (!this._confirmDialog) return;

    const { type, params } = this._confirmDialog;
    this._confirmDialog = null;
    this._render();

    try {
      let serviceType = "";
      switch (type) {
        case "acl":
          serviceType = "matter_autobind/delete_acl_entry";
          break;
        case "binding":
          serviceType = "matter_autobind/delete_binding_entry";
          break;
        case "group":
          serviceType = "matter_autobind/delete_group_entry";
          break;
        case "group_key_map":
          serviceType = "matter_autobind/delete_group_key_map_entry";
          break;
        case "group_key_set":
          serviceType = "matter_autobind/delete_group_key_set";
          break;
      }
      await this._callService(serviceType, params);
      // Refresh device data with force_refresh to get live data after delete
      if (this._selectedDevice) {
        await this._loadDeviceRawData(this._selectedDevice, true);
      }
    } catch (err) {
      alert("Delete failed: " + err.message);
    }
  }

  async _callService(type, data) {
    // Use Home Assistant's built-in WebSocket message sending
    try {
      const result = await this._hass.connection.sendMessagePromise({
        type: type,
        ...data,
      });
      return result;
    } catch (err) {
      console.error("WebSocket call failed:", type, err);
      throw err;
    }
  }

  _getStatusIcon(status) {
    switch (status) {
      case "bound":
        return "✅";
      case "eligible":
        return "🔵";
      case "ineligible":
        return "⚪";
      case "not_scanned":
        return "❓";
      default:
        return "❔";
    }
  }

  _getStatusColor(status) {
    switch (status) {
      case "bound":
        return "var(--success-color, #4caf50)";
      case "eligible":
        return "var(--info-color, #2196f3)";
      case "ineligible":
        return "var(--secondary-text-color)";
      default:
        return "var(--secondary-text-color)";
    }
  }

  _getModeLabel(mode) {
    switch (mode) {
      case "group":
        return "Group";
      case "unicast":
        return "Unicast";
      default:
        return "-";
    }
  }

  _render() {
    let html = `
      <style>
        :host {
          display: block;
          padding: 16px;
          max-width: 1200px;
          margin: 0 auto;
        }
        .panel-header {
          display: flex;
          justify-content: space-between;
          align-items: center;
          margin-bottom: 16px;
        }
        .panel-title {
          font-size: 24px;
          font-weight: 500;
        }
        .automation-list {
          display: grid;
          gap: 8px;
        }
        .automation-item {
          display: flex;
          align-items: center;
          padding: 12px 16px;
          background: var(--card-background-color);
          border-radius: 8px;
          cursor: pointer;
          transition: background 0.2s;
        }
        .automation-item:hover {
          background: var(--secondary-background-color);
        }
        .automation-item.selected {
          outline: 2px solid var(--primary-color);
        }
        .automation-icon {
          font-size: 20px;
          margin-right: 12px;
        }
        .automation-info {
          flex: 1;
        }
        .automation-name {
          font-weight: 500;
        }
        .automation-status {
          font-size: 0.85em;
          color: var(--secondary-text-color);
        }
        .automation-mode {
          padding: 4px 8px;
          border-radius: 4px;
          font-size: 0.8em;
          background: var(--secondary-background-color);
          color: var(--secondary-text-color);
        }
        .detail-panel {
          margin-top: 24px;
          padding: 16px;
          background: var(--card-background-color);
          border-radius: 8px;
        }
        .detail-title {
          font-size: 18px;
          font-weight: 500;
        }
        .preference-selector {
          display: flex;
          align-items: center;
        }
        .preference-selector select {
          cursor: pointer;
        }
        .preference-selector select:focus {
          outline: 2px solid var(--primary-color);
          outline-offset: 1px;
        }
        .detail-section {
          margin-top: 16px;
          margin-bottom: 16px;
        }
        .detail-section-title {
          font-size: 14px;
          font-weight: 500;
          color: var(--secondary-text-color);
          margin-bottom: 8px;
          text-transform: uppercase;
        }
        .device-list {
          display: grid;
          gap: 4px;
        }
        .device-item {
          padding: 8px 12px;
          background: var(--secondary-background-color);
          border-radius: 4px;
          font-size: 0.9em;
        }
        .resource-list {
          font-family: monospace;
          font-size: 0.85em;
          background: var(--secondary-background-color);
          padding: 12px;
          border-radius: 4px;
          overflow-x: auto;
        }
        .loading {
          text-align: center;
          padding: 32px;
          color: var(--secondary-text-color);
        }
        .error {
          padding: 16px;
          background: var(--error-color);
          color: white;
          border-radius: 8px;
          margin-bottom: 16px;
        }
        .btn {
          padding: 8px 16px;
          border: none;
          border-radius: 4px;
          cursor: pointer;
          font-size: 14px;
          background: var(--primary-color);
          color: var(--text-primary-color);
        }
        .btn:hover {
          opacity: 0.9;
        }
        .btn-secondary {
          background: var(--secondary-background-color);
          color: var(--primary-text-color);
        }
        .stats {
          display: flex;
          gap: 16px;
          margin-bottom: 16px;
        }
        .stat {
          padding: 12px 16px;
          background: var(--card-background-color);
          border-radius: 8px;
          text-align: center;
        }
        .stat-value {
          font-size: 24px;
          font-weight: 500;
        }
        .stat-label {
          font-size: 12px;
          color: var(--secondary-text-color);
          text-transform: uppercase;
        }
        .tabs {
          display: flex;
          gap: 8px;
          margin-bottom: 16px;
          border-bottom: 1px solid var(--divider-color);
          padding-bottom: 8px;
        }
        .tab {
          padding: 8px 16px;
          border: none;
          background: transparent;
          cursor: pointer;
          font-size: 14px;
          color: var(--secondary-text-color);
          border-radius: 4px 4px 0 0;
        }
        .tab:hover {
          background: var(--secondary-background-color);
        }
        .tab.active {
          color: var(--primary-color);
          border-bottom: 2px solid var(--primary-color);
          font-weight: 500;
        }
        .debug-layout {
          display: grid;
          grid-template-columns: 280px 1fr;
          gap: 16px;
          min-height: 400px;
        }
        .device-sidebar {
          background: var(--card-background-color);
          border-radius: 8px;
          padding: 12px;
          overflow-y: auto;
          max-height: 600px;
        }
        .device-sidebar-title {
          font-weight: 500;
          margin-bottom: 12px;
          display: flex;
          justify-content: space-between;
          align-items: center;
        }
        .device-sidebar-list {
          display: grid;
          gap: 4px;
        }
        .device-sidebar-item {
          padding: 10px 12px;
          background: var(--secondary-background-color);
          border-radius: 4px;
          cursor: pointer;
          font-size: 0.9em;
        }
        .device-sidebar-item:hover {
          background: var(--primary-color);
          color: var(--text-primary-color);
        }
        .device-sidebar-item.selected {
          background: var(--primary-color);
          color: var(--text-primary-color);
        }
        .device-sidebar-item-name {
          font-weight: 500;
        }
        .device-sidebar-item-info {
          font-size: 0.85em;
          opacity: 0.8;
        }
        .data-panel {
          background: var(--card-background-color);
          border-radius: 8px;
          padding: 16px;
          overflow-y: auto;
          max-height: 600px;
        }
        .data-panel-header {
          display: flex;
          justify-content: space-between;
          align-items: center;
          margin-bottom: 16px;
        }
        .data-section {
          margin-bottom: 24px;
        }
        .data-section-title {
          font-size: 14px;
          font-weight: 500;
          color: var(--secondary-text-color);
          margin-bottom: 8px;
          text-transform: uppercase;
        }
        .data-table {
          width: 100%;
          border-collapse: collapse;
          font-size: 0.85em;
        }
        .data-table th, .data-table td {
          padding: 8px 12px;
          text-align: left;
          border-bottom: 1px solid var(--divider-color);
        }
        .data-table th {
          background: var(--secondary-background-color);
          font-weight: 500;
        }
        .data-table tr:hover {
          background: var(--secondary-background-color);
        }
        .btn-danger {
          background: var(--error-color, #f44336);
          color: white;
        }
        .btn-small {
          padding: 4px 8px;
          font-size: 12px;
        }
        .dialog-overlay {
          position: fixed;
          top: 0;
          left: 0;
          right: 0;
          bottom: 0;
          background: rgba(0, 0, 0, 0.5);
          display: flex;
          align-items: center;
          justify-content: center;
          z-index: 1000;
        }
        .dialog {
          background: var(--card-background-color);
          border-radius: 8px;
          padding: 24px;
          max-width: 400px;
          width: 90%;
        }
        .dialog-title {
          font-size: 18px;
          font-weight: 500;
          margin-bottom: 12px;
        }
        .dialog-message {
          margin-bottom: 20px;
          color: var(--secondary-text-color);
        }
        .dialog-warning {
          color: var(--error-color, #f44336);
          font-weight: 500;
        }
        .dialog-actions {
          display: flex;
          gap: 12px;
          justify-content: flex-end;
        }
        .mono {
          font-family: monospace;
          font-size: 0.9em;
        }
        .empty-state {
          text-align: center;
          padding: 32px;
          color: var(--secondary-text-color);
        }
        .cache-warning {
          background-color: var(--warning-color, #ff9800);
          color: white;
          padding: 8px 12px;
          border-radius: 4px;
          margin-top: 16px;
          font-size: 0.9em;
        }
      </style>

      <div class="panel-header">
        <div class="panel-title">🔗 Matter AutoBind</div>
        <div>
          <button class="btn btn-secondary" data-action="refresh">
            ↻ Refresh
          </button>
          <button class="btn" data-action="reconcile-all">
            ⚡ Reconcile All
          </button>
        </div>
      </div>
    `;

    // Tabs (only show if debug is enabled)
    if (this._debugEnabled) {
      html += `
        <div class="tabs">
          <button class="tab ${this._activeTab === "automations" ? "active" : ""}" data-action="tab-automations">
            Automations
          </button>
          <button class="tab ${this._activeTab === "debug" ? "active" : ""}" data-action="tab-debug">
            🔧 Debug Inspector
          </button>
        </div>
      `;
    }

    if (this._error) {
      html += `<div class="error">Error: ${this._error}</div>`;
    }

    // Tab content
    if (this._activeTab === "debug" && this._debugEnabled) {
      html += this._renderDebugTab();
    } else {
      html += this._renderAutomationsTab();
    }

    // Confirmation dialog
    if (this._confirmDialog) {
      html += this._renderConfirmDialog();
    }

    this.shadowRoot.innerHTML = html;
  }

  _renderAutomationsTab() {
    let html = "";

    if (this._loading) {
      return `<div class="loading">Loading automations...</div>`;
    } else {
      // Stats
      const bound = this._automations.filter(
        (a) => a.binding_status === "bound",
      ).length;
      const eligible = this._automations.filter(
        (a) => a.binding_status === "eligible",
      ).length;
      const total = this._automations.length;

      html += `
        <div class="stats">
          <div class="stat">
            <div class="stat-value">${bound}</div>
            <div class="stat-label">Bound</div>
          </div>
          <div class="stat">
            <div class="stat-value">${eligible}</div>
            <div class="stat-label">Eligible</div>
          </div>
          <div class="stat">
            <div class="stat-value">${total}</div>
            <div class="stat-label">Total</div>
          </div>
        </div>
      `;

      // Automation list
      html += `<div class="automation-list">`;

      for (const auto of this._automations) {
        const isSelected = this._selectedAutomation === auto.automation_id;
        html += `
          <div class="automation-item ${isSelected ? "selected" : ""}" 
               data-action="select-automation"
               data-automation-id="${auto.automation_id}">
            <div class="automation-icon">${this._getStatusIcon(auto.binding_status)}</div>
            <div class="automation-info">
              <div class="automation-name">${this._escapeHtml(auto.friendly_name)}</div>
              <div class="automation-status" style="color: ${this._getStatusColor(auto.binding_status)}">
                ${auto.status_reason}
                ${!auto.is_enabled ? " (disabled)" : ""}
              </div>
            </div>
            <div class="automation-mode">${this._getModeLabel(auto.binding_mode)}</div>
          </div>
        `;
      }

      html += `</div>`;

      // Detail panel
      if (this._selectedAutomation) {
        html += this._renderDetailPanel();
      }
    }

    return html;
  }

  _renderDebugTab() {
    let html = `<div class="debug-layout">`;

    // Device sidebar
    html += `
      <div class="device-sidebar">
        <div class="device-sidebar-title">
          <span>Matter Devices</span>
          <button class="btn btn-small btn-secondary" data-action="refresh-devices">↻</button>
        </div>
        <div class="device-sidebar-list">
    `;

    if (this._matterDevices.length === 0) {
      html += `<div class="empty-state">No Matter devices found</div>`;
    } else {
      for (const device of this._matterDevices) {
        // Skip devices without a valid node_id
        if (device.node_id === null || device.node_id === undefined) {
          continue;
        }
        const isSelected = this._selectedDevice === device.node_id;
        html += `
          <div class="device-sidebar-item ${isSelected ? "selected" : ""}" 
               data-action="select-device"
               data-node-id="${device.node_id}">
            <div class="device-sidebar-item-name">${this._escapeHtml(device.device_name)}</div>
            <div class="device-sidebar-item-info">Node ${device.node_id} · ${this._escapeHtml(device.model || "Unknown")}</div>
          </div>
        `;
      }
    }

    html += `</div></div>`;

    // Data panel
    html += `<div class="data-panel">`;

    if (!this._selectedDevice) {
      html += `<div class="empty-state">Select a device to view its Matter resources</div>`;
    } else if (this._deviceLoading) {
      html += `<div class="loading">Loading device data...</div>`;
    } else if (this._deviceRawData) {
      html += this._renderDeviceRawData();
    }

    html += `</div></div>`;

    return html;
  }

  _renderDeviceRawData() {
    const data = this._deviceRawData;
    const nodeId = this._selectedDevice;

    if (data.error) {
      return `<div class="error">${data.error}</div>`;
    }

    let html = `
      <div class="data-panel-header">
        <div class="detail-title">Node ${nodeId}</div>
        <button class="btn btn-small btn-secondary" data-action="refresh-device-data">↻ Refresh</button>
      </div>
    `;

    // ACLs
    html += `<div class="data-section">
      <div class="data-section-title">ACLs (${data.acls?.length || 0})</div>`;

    if (data.acls && data.acls.length > 0) {
      html += `<table class="data-table">
        <tr>
          <th>Index</th>
          <th>Privilege</th>
          <th>Auth Mode</th>
          <th>Subjects</th>
          <th>Targets</th>
          <th></th>
        </tr>`;

      for (const acl of data.acls) {
        const subjects = acl.subjects?.join(", ") || "-";
        const targets =
          acl.targets
            ?.map((t) => `ep${t.endpoint || "*"}:c${t.cluster || "*"}`)
            .join(", ") || "All";
        html += `<tr>
          <td>${acl.index}</td>
          <td>${this._escapeHtml(acl.privilege_name || acl.privilege)}</td>
          <td>${this._escapeHtml(acl.auth_mode_name || acl.auth_mode)}</td>
          <td class="mono">${this._escapeHtml(subjects)}</td>
          <td class="mono">${this._escapeHtml(targets)}</td>
          <td>
            <button class="btn btn-small btn-danger" 
                    data-action="delete-acl"
                    data-node-id="${nodeId}"
                    data-index="${acl.index}">Delete</button>
          </td>
        </tr>`;
      }
      html += `</table>`;
    } else {
      html += `<div class="empty-state">No ACLs found</div>`;
    }
    html += `</div>`;

    // Bindings
    html += `<div class="data-section">
      <div class="data-section-title">Bindings (${data.bindings?.length || 0})</div>`;

    if (data.bindings && data.bindings.length > 0) {
      html += `<table class="data-table">
        <tr>
          <th>Index</th>
          <th>Source EP</th>
          <th>Target</th>
          <th>Target EP</th>
          <th>Cluster</th>
          <th></th>
        </tr>`;

      for (const binding of data.bindings) {
        const target = binding.group_id
          ? `Group ${binding.group_id}`
          : `Node ${binding.node_id || "?"}`;
        html += `<tr>
          <td>${binding.index}</td>
          <td>${binding.source_endpoint}</td>
          <td class="mono">${this._escapeHtml(target)}</td>
          <td>${binding.endpoint || "-"}</td>
          <td class="mono">${binding.cluster || "-"}</td>
          <td>
            <button class="btn btn-small btn-danger" 
                    data-action="delete-binding"
                    data-node-id="${nodeId}"
                    data-endpoint="${binding.source_endpoint}"
                    data-index="${binding.index}">Delete</button>
          </td>
        </tr>`;
      }
      html += `</table>`;
    } else {
      html += `<div class="empty-state">No bindings found</div>`;
    }
    html += `</div>`;

    // Groups
    html += `<div class="data-section">
      <div class="data-section-title">Groups (${data.groups?.length || 0})</div>`;

    if (data.groups && data.groups.length > 0) {
      html += `<table class="data-table">
        <tr>
          <th>Group ID</th>
          <th>Endpoint</th>
          <th>Name</th>
          <th></th>
        </tr>`;

      for (const group of data.groups) {
        html += `<tr>
          <td>${group.group_id}</td>
          <td>${group.endpoint}</td>
          <td>${this._escapeHtml(group.name || "-")}</td>
          <td>
            <button class="btn btn-small btn-danger" 
                    data-action="delete-group"
                    data-node-id="${nodeId}"
                    data-endpoint="${group.endpoint}"
                    data-group-id="${group.group_id}">Remove</button>
          </td>
        </tr>`;
      }
      html += `</table>`;
    } else {
      html += `<div class="empty-state">No group memberships found</div>`;
    }
    html += `</div>`;

    // GroupKeyMap
    html += `<div class="data-section">
      <div class="data-section-title">GroupKeyMap (${data.group_key_map?.length || 0})</div>`;

    if (data.group_key_map && data.group_key_map.length > 0) {
      html += `<table class="data-table">
        <tr>
          <th>Index</th>
          <th>Group ID</th>
          <th>KeySet Index</th>
          <th></th>
        </tr>`;

      for (const entry of data.group_key_map) {
        html += `<tr>
          <td>${entry.index}</td>
          <td>${entry.group_id}</td>
          <td>${entry.group_key_set_id}</td>
          <td>
            <button class="btn btn-small btn-danger" 
                    data-action="delete-gkm"
                    data-node-id="${nodeId}"
                    data-index="${entry.index}">Delete</button>
          </td>
        </tr>`;
      }
      html += `</table>`;
    } else {
      html += `<div class="empty-state">No GroupKeyMap entries found</div>`;
    }
    html += `</div>`;

    // GroupKeySets
    html += `<div class="data-section">
      <div class="data-section-title">GroupKeySets (${data.group_key_sets?.length || 0})</div>`;

    if (data.group_key_sets && data.group_key_sets.length > 0) {
      html += `<table class="data-table">
        <tr>
          <th>KeySet ID</th>
          <th>Security Policy</th>
          <th>Epochs</th>
          <th></th>
        </tr>`;

      for (const keySet of data.group_key_sets) {
        const policy = keySet.group_key_security_policy || "-";
        const epochInfo = keySet.epoch_keys?.length
          ? `${keySet.epoch_keys.length} key(s)`
          : "-";
        html += `<tr>
          <td>${keySet.group_key_set_id}</td>
          <td>${this._escapeHtml(policy)}</td>
          <td>${this._escapeHtml(epochInfo)}</td>
          <td>
            <button class="btn btn-small btn-danger" 
                    data-action="delete-gks"
                    data-node-id="${nodeId}"
                    data-key-set-id="${keySet.group_key_set_id}">Delete</button>
          </td>
        </tr>`;
      }
      html += `</table>`;
    } else {
      html += `<div class="empty-state">No GroupKeySets found</div>`;
    }
    html += `</div>`;

    // Show cache status indicator
    if (data.from_cache) {
      html += `<div class="cache-warning">⚠️ Showing cached data - node may be offline</div>`;
    }

    return html;
  }

  _renderConfirmDialog() {
    const { message } = this._confirmDialog;
    return `
      <div class="dialog-overlay">
        <div class="dialog">
          <div class="dialog-title">⚠️ Confirm Delete</div>
          <div class="dialog-message">${this._escapeHtml(message)}</div>
          <div class="dialog-warning">This action cannot be undone. The resource will be removed from the device.</div>
          <div class="dialog-actions">
            <button class="btn btn-secondary" data-action="cancel-delete">Cancel</button>
            <button class="btn btn-danger" data-action="confirm-delete">Delete</button>
          </div>
        </div>
      </div>
    `;
  }

  _renderDetailPanel() {
    if (!this._automationDetail) {
      return `<div class="detail-panel"><div class="loading">Loading details...</div></div>`;
    }

    if (this._automationDetail.error) {
      return `<div class="detail-panel"><div class="error">${this._automationDetail.error}</div></div>`;
    }

    const detail = this._automationDetail;
    // Build preference options from available_preferences
    const availablePrefs = detail.available_preferences || [
      { value: "auto", label: "Auto" },
      { value: "unicast", label: "Unicast" },
    ];
    const preferenceOptions = availablePrefs
      .map(
        (pref) =>
          `<option value="${pref.value}" ${detail.binding_preference === pref.value ? "selected" : ""}>${this._escapeHtml(pref.label)}</option>`,
      )
      .join("");

    let html = `
      <div class="detail-panel">
        <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px;">
          <div class="detail-title">${this._escapeHtml(detail.friendly_name)}</div>
          <div style="display: flex; align-items: center; gap: 12px;">
            <div class="preference-selector">
              <label for="binding-preference" style="margin-right: 8px; font-size: 14px;">Binding Mode:</label>
              <select id="binding-preference" 
                      data-automation-id="${detail.automation_id}"
                      style="padding: 6px 10px; border-radius: 4px; border: 1px solid var(--divider-color); background: var(--card-background-color); color: var(--primary-text-color);">
                ${preferenceOptions}
              </select>
            </div>
            <button class="btn btn-secondary" data-action="reconcile-single" data-automation-id="${detail.automation_id}">
              ⚡ Reconcile
            </button>
          </div>
        </div>
    `;

    // Trigger devices
    html += `
      <div class="detail-section">
        <div class="detail-section-title">Trigger Devices (${detail.trigger_devices.length})</div>
        <div class="device-list">
    `;
    for (const device of detail.trigger_devices) {
      html += `
        <div class="device-item">
          ${this._escapeHtml(device.device_name)} 
          <span style="color: var(--secondary-text-color)">(Node ${device.node_id || "?"})</span>
        </div>
      `;
    }
    html += `</div></div>`;

    // Action devices
    html += `
      <div class="detail-section">
        <div class="detail-section-title">Action Devices (${detail.action_devices.length})</div>
        <div class="device-list">
    `;
    for (const device of detail.action_devices) {
      html += `
        <div class="device-item">
          ${this._escapeHtml(device.device_name)} 
          <span style="color: var(--secondary-text-color)">(Node ${device.node_id || "?"})</span>
        </div>
      `;
    }
    html += `</div></div>`;

    // Resources
    if (detail.acls.length || detail.bindings.length || detail.groups.length) {
      html += `
        <div class="detail-section">
          <div class="detail-section-title">Resources</div>
          <div class="resource-list">
      `;

      for (const acl of detail.acls) {
        html += `ACL: Node ${acl.target_node_id} ← Node ${acl.source_node_id} (refs: ${acl.ref_count})\n`;
      }
      for (const binding of detail.bindings) {
        const target = binding.target_group_id
          ? `Group ${binding.target_group_id}`
          : `Node ${binding.target_node_id}`;
        html += `Binding: Node ${binding.source_node_id}:${binding.source_endpoint} → ${target}:${binding.target_endpoint} (refs: ${binding.ref_count})\n`;
      }
      for (const group of detail.groups) {
        html += `Group: ${group.group_id} "${group.group_name}" (${group.member_count} members, refs: ${group.ref_count})\n`;
      }

      html += `</div></div>`;
    }

    html += `</div>`;
    return html;
  }

  _escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }
}

customElements.define("matter-autobind-panel", MatterAutoBindPanel);
