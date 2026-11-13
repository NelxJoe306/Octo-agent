"use client";

import { useEffect, useId, useRef, useState } from "react";
import type { ReactNode } from "react";
import {
  Check,
  CircleHelp,
  Info,
  Minus,
  Monitor,
  Plus,
  SlidersHorizontal,
  Sparkles,
  X,
} from "lucide-react";
import { OctoMark } from "./mark";
import { LocalModelSettings } from "./local-models";
import type { LocalModels } from "./local-models";

export type Preferences = {
  theme: "light" | "dark" | "system";
  fontSize: number;
  speed: "slow" | "normal" | "fast";
  showHints: boolean;
};
export const defaultPreferences: Preferences = {
  theme: "light",
  fontSize: 14,
  speed: "normal",
  showHints: true,
};
export type SettingsTab = "general" | "models" | "advanced";
const tabs = [
  { id: "general", label: "通用", icon: SlidersHorizontal },
  { id: "models", label: "模型", icon: Sparkles },
  { id: "advanced", label: "高级", icon: Monitor },
] satisfies { id: SettingsTab; label: string; icon: typeof Monitor }[];

function Help({ children }: { children: string }) {
  const id = useId();
  return (
    <span className="help">
      <button
        className="help-button"
        aria-label="查看说明"
        aria-describedby={id}
      >
        <CircleHelp size={13} />
      </button>
      <span id={id} role="tooltip" className="help-copy">
        {children}
      </span>
    </span>
  );
}
function Row({
  label,
  description,
  help,
  children,
  stacked = false,
}: {
  label: string;
  description?: string;
  help?: string;
  children: ReactNode;
  stacked?: boolean;
}) {
  return (
    <div className={`setting-row ${stacked ? "setting-stacked" : ""}`}>
      <div>
        <div className="setting-label">
          {label}
          {help && <Help>{help}</Help>}
        </div>
        {description && (
          <div className="setting-description">{description}</div>
        )}
      </div>
      {children}
    </div>
  );
}
function Segmented<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: { value: T; label: string }[];
  onChange: (value: T) => void;
}) {
  return (
    <div className="segmented" role="group" aria-label={label}>
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          aria-pressed={value === option.value}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

export function Settings({
  preferences,
  onChange,
  onClose,
  models,
  initialTab = "general",
}: {
  preferences: Preferences;
  onChange: (value: Preferences) => void;
  onClose: () => void;
  models: LocalModels;
  initialTab?: SettingsTab;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [tab, setTab] = useState<SettingsTab>(initialTab);
  const [closing, setClosing] = useState(false);
  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    return () => {
      element?.close();
      if (timer.current) clearTimeout(timer.current);
    };
  }, []);
  function close() {
    if (closing) return;
    setClosing(true);
    timer.current = setTimeout(
      onClose,
      window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 140,
    );
  }
  function update(value: Partial<Preferences>) {
    onChange({ ...preferences, ...value });
  }
  return (
    <dialog
      ref={dialog}
      className="settings-dialog"
      aria-labelledby="settings-title"
      data-closing={closing}
      onCancel={(event) => {
        event.preventDefault();
        close();
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget) {
          const rect = event.currentTarget.getBoundingClientRect();
          if (
            event.clientX < rect.left ||
            event.clientX > rect.right ||
            event.clientY < rect.top ||
            event.clientY > rect.bottom
          )
            close();
        }
      }}
    >
      <div className="settings-layout">
        <nav className="settings-nav" aria-label="设置分类">
          <div className="settings-brand">
            <OctoMark />
            <div>
              <strong>Octo</strong>
              <small>你的工作空间</small>
            </div>
          </div>
          <div className="settings-nav-group">偏好设置</div>
          {tabs.map((item) => (
            <button
              key={item.id}
              aria-current={tab === item.id ? "page" : undefined}
              onClick={() => setTab(item.id)}
            >
              <item.icon size={17} />
              {item.label}
            </button>
          ))}
        </nav>
        <div className="settings-content">
          <button
            className="icon-button settings-close"
            aria-label="关闭设置"
            onClick={close}
          >
            <X size={18} />
          </button>
          <h1 id="settings-title">
            {tabs.find((item) => item.id === tab)?.label}
          </h1>
          {tab === "general" && (
            <>
              <p className="settings-description">
                让 Octo 以你习惯的方式呈现。
              </p>
              <div className="settings-section-label">外观</div>
              <div className="settings-card">
                <Row
                  label="主题"
                  description="选择适合当前环境的外观。"
                  stacked
                >
                  <Segmented
                    label="主题"
                    value={preferences.theme}
                    options={[
                      { value: "light", label: "浅色" },
                      { value: "dark", label: "深色" },
                      { value: "system", label: "跟随系统" },
                    ]}
                    onChange={(theme) => update({ theme })}
                  />
                </Row>
                <Row
                  label="对话字号"
                  help="调整中间对话内容的字号，不改变导航与设置布局。"
                >
                  <div className="stepper" role="group" aria-label="对话字号">
                    <button
                      aria-label="减小字号"
                      disabled={preferences.fontSize <= 12}
                      onClick={() =>
                        update({ fontSize: preferences.fontSize - 1 })
                      }
                    >
                      <Minus size={14} />
                    </button>
                    <output aria-live="polite">{preferences.fontSize}</output>
                    <button
                      aria-label="增大字号"
                      disabled={preferences.fontSize >= 18}
                      onClick={() =>
                        update({ fontSize: preferences.fontSize + 1 })
                      }
                    >
                      <Plus size={14} />
                    </button>
                  </div>
                </Row>
              </div>
              <div className="settings-section-label">对话</div>
              <div className="settings-card">
                <Row
                  label="显示输入提示"
                  description="在输入框下方显示快捷键与预览状态。"
                >
                  <button
                    className="switch"
                    role="switch"
                    aria-label="显示输入提示"
                    aria-checked={preferences.showHints}
                    onClick={() =>
                      update({ showHints: !preferences.showHints })
                    }
                  >
                    <span />
                  </button>
                </Row>
              </div>
            </>
          )}
          {tab === "models" && <LocalModelSettings models={models} />}
          {tab === "advanced" && (
            <>
              <p className="settings-description">
                调整这一版预览的行为，方便体验流式对话。
              </p>
              <div className="settings-section-label">交互预览</div>
              <div className="settings-card">
                <Row
                  label="演示回复速度"
                  help="只改变本地演示的文字播放速度，不代表真实模型性能。"
                  stacked
                >
                  <Segmented
                    label="演示回复速度"
                    value={preferences.speed}
                    options={[
                      { value: "slow", label: "慢速" },
                      { value: "normal", label: "标准" },
                      { value: "fast", label: "快速" },
                    ]}
                    onChange={(speed) => update({ speed })}
                  />
                </Row>
                <Row
                  label="恢复默认设置"
                  description="恢复浅色主题、14 号字与标准播放速度。"
                >
                  <button
                    className="button"
                    onClick={() => onChange(defaultPreferences)}
                  >
                    恢复默认
                  </button>
                </Row>
              </div>
              <p className="settings-note">
                <Info size={13} />
                当前偏好仅在本次页面打开期间有效。刷新后恢复默认值。
              </p>
            </>
          )}
          <div className="settings-actions">
            <span className="saved-label" hidden={tab === "models"}>
              <Check size={13} />
              已应用到当前预览
            </span>
            <button className="button primary" onClick={close}>
              完成
            </button>
          </div>
        </div>
      </div>
    </dialog>
  );
}
