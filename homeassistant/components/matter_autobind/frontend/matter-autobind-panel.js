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
    this._render();
    this._attachEventListeners();
    await this._loadDashboardData();
  }

  _attachEventListeners() {
    // Use event delegation on the shadow root
    this.shadowRoot.addEventListener("click", (e) => {
      const target = e.target.closest("[data-action]");
      if (!target) return;

      const action = target.dataset.action;
      const automationId = target.dataset.automationId;

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
          margin-bottom: 16px;
        }
        .detail-section {
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

    if (this._error) {
      html += `<div class="error">Error: ${this._error}</div>`;
    }

    if (this._loading) {
      html += `<div class="loading">Loading automations...</div>`;
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

    this.shadowRoot.innerHTML = html;
  }

  _renderDetailPanel() {
    if (!this._automationDetail) {
      return `<div class="detail-panel"><div class="loading">Loading details...</div></div>`;
    }

    if (this._automationDetail.error) {
      return `<div class="detail-panel"><div class="error">${this._automationDetail.error}</div></div>`;
    }

    const detail = this._automationDetail;
    let html = `
      <div class="detail-panel">
        <div style="display: flex; justify-content: space-between; align-items: center;">
          <div class="detail-title">${this._escapeHtml(detail.friendly_name)}</div>
          <button class="btn btn-secondary" data-action="reconcile-single" data-automation-id="${detail.automation_id}">
            ⚡ Reconcile
          </button>
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
