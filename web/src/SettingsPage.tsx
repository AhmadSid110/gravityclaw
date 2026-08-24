import { useState } from "react";
import { THEMES, FONTS, FONT_SIZES, DENSITIES, useAppearance } from "./theme";
import type { ControlState } from "./types";

export function SettingsPage({ state }: { state: ControlState }) {
  const [activeTab, setActiveTab] = useState<"appearance" | "general" | "models" | "system">("appearance");
  const {
    theme,
    setTheme,
    font,
    setFont,
    fontSize,
    setFontSize,
    density,
    setDensity,
    currentThemeConfig,
    currentFontConfig,
    currentFontSizeConfig,
    currentDensityConfig,
  } = useAppearance();

  return (
    <div className="page fade-in settings-page">
      <div className="page-heading">
        <div>
          <div className="eyebrow">CONFIGURATION</div>
          <h1>Settings</h1>
          <p className="muted">Customize GravityClaw appearance, interface density, and system parameters.</p>
        </div>
      </div>

      <div className="studio-tabs settings-tabs">
        <button
          className={activeTab === "appearance" ? "active" : ""}
          onClick={() => setActiveTab("appearance")}
        >
          🎨 Appearance
        </button>
        <button
          className={activeTab === "general" ? "active" : ""}
          onClick={() => setActiveTab("general")}
        >
          ⚙ General
        </button>
        <button
          className={activeTab === "models" ? "active" : ""}
          onClick={() => setActiveTab("models")}
        >
          🤖 Models
        </button>
        <button
          className={activeTab === "system" ? "active" : ""}
          onClick={() => setActiveTab("system")}
        >
          💻 System Diagnostics
        </button>
      </div>

      {activeTab === "appearance" && (
        <div className="settings-section fade-in">
          {/* THEME PICKER */}
          <section className="panel settings-panel">
            <div className="panel-header">
              <div>
                <h2>Color Theme</h2>
                <small className="muted-text">Choose the color atmosphere for the control center.</small>
              </div>
              <span className="count-pill">{currentThemeConfig.name}</span>
            </div>

            <div className="theme-grid">
              {THEMES.map((item) => {
                const isSelected = theme === item.id;
                return (
                  <button
                    key={item.id}
                    className={`theme-card ${isSelected ? "selected" : ""}`}
                    onClick={() => setTheme(item.id)}
                  >
                    <div className="theme-card-left">
                      <span className="theme-accent-pip" style={{ background: item.accent }} />
                    </div>
                    <div className="theme-card-info">
                      <div className="theme-card-head">
                        <strong>{item.name}</strong>
                        {isSelected && <span className="selected-check">✓</span>}
                      </div>
                      <small>{item.description}</small>
                    </div>
                  </button>
                );
              })}
            </div>
          </section>

          {/* TYPOGRAPHY PICKER */}
          <section className="panel settings-panel">
            <div className="panel-header">
              <div>
                <h2>Typography Preset</h2>
                <small className="muted-text">Select font pairing for headings, interface labels, and body text.</small>
              </div>
              <span className="count-pill">{currentFontConfig.name}</span>
            </div>

            <div className="font-grid">
              {FONTS.map((item) => {
                const isSelected = font === item.id;
                return (
                  <button
                    key={item.id}
                    className={`font-card ${isSelected ? "selected" : ""}`}
                    onClick={() => setFont(item.id)}
                  >
                    <div className="font-card-head">
                      <div className="font-title-row">
                        <strong>{item.name}</strong>
                        {item.id === "system" && <span className="subtle-tag">Zero Load</span>}
                      </div>
                      {isSelected && <span className="selected-check">✓</span>}
                    </div>
                    <p className="font-description">{item.description}</p>
                    <div className="font-sample-box">
                      <span>{item.sample}</span>
                    </div>
                  </button>
                );
              })}
            </div>
          </section>

                    {/* FONT SIZE SCALING */}
          <section className="panel settings-panel">
            <div className="panel-header">
              <div>
                <h2>Font Size Scaling</h2>
                <small className="muted-text">Adjust text scaling across chat messages, headers, and UI elements.</small>
              </div>
              <span className="count-pill">{currentFontSizeConfig.name} ({currentFontSizeConfig.label})</span>
            </div>

            <div className="font-size-grid">
              {FONT_SIZES.map((item) => {
                const isSelected = fontSize === item.id;
                return (
                  <button
                    key={item.id}
                    className={`font-size-card ${isSelected ? "selected" : ""}`}
                    onClick={() => setFontSize(item.id)}
                  >
                    <div className="font-size-card-head">
                      <span className="font-size-glyph">Aa</span>
                      <div className="font-size-card-titles">
                        <strong>{item.name}</strong>
                        <span className="font-size-badge">{item.label}</span>
                      </div>
                      {isSelected && <span className="selected-check">✓</span>}
                    </div>
                    <small className="font-size-desc">{item.description}</small>
                  </button>
                );
              })}
            </div>
          </section>

          {/* DENSITY & SIZING */}
          <section className="panel settings-panel">
            <div className="panel-header">
              <div>
                <h2>Interface Density</h2>
                <small className="muted-text">Control padding, control heights, and information density.</small>
              </div>
              <span className="count-pill">{currentDensityConfig.name}</span>
            </div>

            <div className="density-grid">
              {DENSITIES.map((item) => {
                const isSelected = density === item.id;
                return (
                  <button
                    key={item.id}
                    className={`density-card ${isSelected ? "selected" : ""}`}
                    onClick={() => setDensity(item.id)}
                  >
                    <div className="density-card-head">
                      <strong>{item.name}</strong>
                      {isSelected && <span className="selected-check">✓</span>}
                    </div>
                    <small className="muted-text">{item.description}</small>
                  </button>
                );
              })}
            </div>
          </section>
        </div>
      )}

      {activeTab === "general" && (
        <div className="settings-section fade-in">
          <section className="panel settings-panel">
            <div className="panel-header">
              <h2>Workspace & Identity</h2>
            </div>
            <div className="settings-row">
              <div>
                <strong>Primary Workspace</strong>
                <small className="muted-text">The default directory mapped to conversations and agent runs.</small>
              </div>
              <code>gravityclaw</code>
            </div>
            <div className="settings-row">
              <div>
                <strong>Personal Agent Name</strong>
                <small className="muted-text">The name GravityClaw uses in conversation headers and notifications.</small>
              </div>
              <span className="badge">GravityClaw</span>
            </div>
          </section>
        </div>
      )}

      {activeTab === "models" && (
        <div className="settings-section fade-in">
          <section className="panel settings-panel">
            <div className="panel-header">
              <h2>Model Defaults & Reasoning</h2>
            </div>
            <div className="settings-row">
              <div>
                <strong>Default Model</strong>
                <small className="muted-text">Active model applied to new conversations and tasks.</small>
              </div>
              <span className="badge">AGY Default (Auto)</span>
            </div>
            <div className="settings-row">
              <div>
                <strong>Effort Levels Supported</strong>
                <small className="muted-text">Reasoning budget allocation levels.</small>
              </div>
              <span>low, medium, high</span>
            </div>
          </section>
        </div>
      )}

      {activeTab === "system" && (
        <div className="settings-section fade-in">
          <section className="panel settings-panel">
            <div className="panel-header">
              <h2>System Diagnostics & State</h2>
            </div>
            <div className="settings-row">
              <div>
                <strong>Connection State</strong>
                <small className="muted-text">WebSocket live feed status to backend control plane.</small>
              </div>
              <span className="status-badge running">{state.connection}</span>
            </div>
            <div className="settings-row">
              <div>
                <strong>SQLite Schema Version</strong>
                <small className="muted-text">Durable local database state.</small>
              </div>
              <code>Schema v17</code>
            </div>
            <div className="settings-row">
              <div>
                <strong>Active Scheduled Triggers</strong>
                <small className="muted-text">Heartbeat, hourly autonomous checks, and cron jobs.</small>
              </div>
              <span>{state.snapshot?.next_schedules?.length ?? 0} active triggers</span>
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
