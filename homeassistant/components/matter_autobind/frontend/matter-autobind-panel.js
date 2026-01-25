// frontend/matter-autobind-panel.js
// Minimal "Hello World" panel for Matter AutoBind

class MatterAutoBindPanel extends HTMLElement {
  constructor() {
    super();
    this._hass = null;
    this._content = null;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._content) {
      this._content = document.createElement("div");
      this._content.style.padding = "16px";
      this._content.innerHTML = `
        <ha-card header="Matter AutoBind">
          <div class="card-content">
            <p>🔗 <strong>Matter AutoBind Panel</strong></p>
            <p>This panel will show your automations and their Matter binding status.</p>
            <hr style="margin: 16px 0; border: none; border-top: 1px solid var(--divider-color);">
            <p style="color: var(--secondary-text-color); font-size: 0.9em;">
              <em>Phase 1 Complete - Hello World!</em>
            </p>
            <p style="color: var(--secondary-text-color); font-size: 0.9em;">
              Next: WebSocket API integration
            </p>
          </div>
        </ha-card>
      `;
      this.appendChild(this._content);
    }
  }

  get hass() {
    return this._hass;
  }
}

customElements.define("matter-autobind-panel", MatterAutoBindPanel);
