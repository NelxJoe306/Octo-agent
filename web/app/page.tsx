"use client";

import { useEffect, useReducer, useRef, useState } from "react";
import type { CSSProperties } from "react";
import {
  ArrowUp,
  Check,
  Copy,
  GitBranch,
  Menu,
  PanelRight,
  PencilLine,
  Pin,
  PinOff,
  Plus,
  Search,
  Settings2,
  ShieldCheck,
  Square,
  X,
} from "lucide-react";
import {
  chatReducer,
  createPreviewState,
  RequestId,
  SessionId,
} from "@/lib/chat";
import type { Session } from "@/lib/chat";
import { OctoMark } from "@/components/mark";
import { defaultPreferences, Settings } from "@/components/settings";
import type { SettingsTab } from "@/components/settings";
import { useLocalModels } from "@/components/local-models";
import { ModelPicker } from "@/components/model-picker";
import { ResizeDivider } from "@/components/resize-divider";
import { resolveSelection } from "@/lib/models";
import type { ModelSelection } from "@/lib/models";
import { fitWorkspace } from "@/lib/layout";

type LayoutStyle = CSSProperties & {
  "--sidebar-width": string;
  "--inspector-width": string;
  "--drawer-width": string;
};

export default function Home() {
  const [state, dispatch] = useReducer(
    chatReducer,
    undefined,
    createPreviewState,
  );
  const [preferences, setPreferences] = useState(defaultPreferences);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [treeOpen, setTreeOpen] = useState(true);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [plan, setPlan] = useState(false);
  const models = useLocalModels();
  const [chosen, setChosen] = useState<ModelSelection>({
    modelId: null,
    effort: null,
  });
  const selection = resolveSelection(models.config, chosen);
  const model = models.config.models.find(
    (item) => item.id === selection.modelId,
  );
  const [settingsTab, setSettingsTab] = useState<SettingsTab>("general");
  const [leftWidth, setLeftWidth] = useState(248);
  const [rightWidth, setRightWidth] = useState(276);
  const [resizing, setResizing] = useState(false);
  const [windowWidth, setWindowWidth] = useState(0);
  const appRef = useRef<HTMLElement>(null);
  const layout = fitWorkspace({
    width: windowWidth,
    left: leftWidth,
    right: rightWidth,
    treeOpen,
  });
  const leftMax = layout.leftMax;
  const rightMax = layout.rightMax;
  const drawerOpen = layout.kind !== "wide" && sidebarOpen;
  const treeOnly = layout.kind === "narrow" && treeOpen;
  const layoutStyle: LayoutStyle = {
    "--sidebar-width": `${layout.left}px`,
    "--inspector-width": `${layout.right}px`,
    "--drawer-width": `${layout.drawerWidth}px`,
  };
  const [toast, setToast] = useState("");
  const [attachment, setAttachment] = useState("");
  const [unread, setUnread] = useState(false);
  const followingRef = useRef(true);
  const searchRef = useRef<HTMLInputElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const messagesRef = useRef<HTMLDivElement>(null);
  const active = state.sessions.find((item) => item.id === state.activeId);
  const draft = state.drafts[state.activeId ?? "new"] ?? "";
  const requestId = state.run.kind === "running" ? state.run.requestId : null;

  useEffect(() => {
    const element = appRef.current;
    if (!element) return;
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (entry) setWindowWidth(entry.contentRect.width);
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (windowWidth > 0 && windowWidth < 600) setTreeOpen(false);
  }, [windowWidth]);
  useEffect(() => {
    if (!drawerOpen) return;
    const prior = document.activeElement;
    appRef.current?.querySelector<HTMLButtonElement>(".new-chat")?.focus();
    return () => {
      if (prior instanceof HTMLElement) prior.focus();
    };
  }, [drawerOpen]);

  useEffect(() => {
    if (!requestId) return;
    const timer = setInterval(
      () => dispatch({ kind: "tick", requestId }),
      { slow: 90, normal: 35, fast: 12 }[preferences.speed],
    );
    return () => clearInterval(timer);
  }, [requestId, preferences.speed]);
  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = () => {
      document.documentElement.dataset.theme =
        preferences.theme === "system"
          ? media.matches
            ? "dark"
            : "light"
          : preferences.theme;
    };
    apply();
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }, [preferences.theme]);
  useEffect(() => {
    document.documentElement.style.setProperty(
      "--chat-font-size",
      `${preferences.fontSize}px`,
    );
  }, [preferences.fontSize]);
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(""), 2600);
    return () => clearTimeout(timer);
  }, [toast]);
  useEffect(() => {
    if (searchOpen) searchRef.current?.focus();
  }, [searchOpen, drawerOpen]);
  useEffect(() => {
    function key(event: KeyboardEvent) {
      if (!(event.ctrlKey || event.metaKey) || settingsOpen) return;
      if (event.key.toLowerCase() === "k") {
        event.preventDefault();
        setSearchOpen(true);
        if (layout.kind !== "wide") setSidebarOpen(true);
      }
      if (event.key.toLowerCase() === "n") {
        event.preventDefault();
        newChat();
      }
    }
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [settingsOpen, layout.kind]);
  useEffect(() => {
    followingRef.current = true;
    setUnread(false);
  }, [state.activeId]);
  useEffect(() => {
    const list = messagesRef.current;
    if (!list) return;
    if (followingRef.current) list.scrollTop = list.scrollHeight;
    else setUnread(true);
  }, [state.activeId, active?.messages.at(-1)?.text]);

  function newChat() {
    dispatch({ kind: "new" });
    setSidebarOpen(false);
    if (layout.kind === "narrow") setTreeOpen(false);
    requestAnimationFrame(() => inputRef.current?.focus());
  }
  function send() {
    if (!draft.trim() || state.run.kind === "running") return;
    followingRef.current = true;
    setUnread(false);
    dispatch({
      kind: "send",
      requestId: RequestId.parse(crypto.randomUUID()),
      newSessionId: SessionId.parse(crypto.randomUUID()),
      reply: `这是一条本地演示回复。\n\n已收到：${draft.trim().slice(0, 180)}\n\n你可以在生成过程中切换到其他会话，再回来查看结果；回复会保留在发送时的会话中。也可以点击停止按钮中断输出。\n\n${plan ? "规划模式" : "执行模式"} · ${model?.name ?? "未选择模型"}${selection.effort ? ` · 推理强度：${selection.effort}` : ""}\n\n这一版用于体验布局与交互，尚未连接 Octo 后端，也没有执行工具或系统命令。`,
    });
  }
  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setToast("已复制回复");
    } catch {
      setToast("浏览器未允许复制，请选择文字复制");
    }
  }
  function sessionRow(session: Session) {
    return (
      <div
        key={session.id}
        className={`session-row ${state.activeId === session.id ? "selected" : ""}`}
      >
        <button
          className="session-select"
          aria-current={state.activeId === session.id ? "page" : undefined}
          title={session.title}
          onClick={() => {
            dispatch({ kind: "select", id: session.id });
            setSidebarOpen(false);
            if (layout.kind === "narrow") setTreeOpen(false);
          }}
        >
          <span>{session.title}</span>
          {state.run.kind === "running" &&
            state.run.sessionId === session.id && (
              <i className="running-dot" aria-label="正在生成" />
            )}
        </button>
        <button
          className="icon-button session-pin"
          aria-label={`${session.pinned ? "取消置顶" : "置顶"} ${session.title}`}
          onClick={() => dispatch({ kind: "pin", id: session.id })}
        >
          {session.pinned ? <PinOff size={12} /> : <Pin size={12} />}
        </button>
      </div>
    );
  }
  const filtered = state.sessions.filter((item) =>
    item.title.toLocaleLowerCase().includes(query.toLocaleLowerCase()),
  );

  return (
    <main
      ref={appRef}
      className="app"
      data-sidebar={sidebarOpen}
      data-layout={layout.kind}
      data-resizing={resizing}
      style={layoutStyle}
    >
      {drawerOpen && (
        <button
          className="sidebar-backdrop"
          aria-label="关闭会话导航遮罩"
          onClick={() => setSidebarOpen(false)}
        />
      )}
      <aside
        className="sidebar"
        aria-label="会话列表"
        role={layout.kind !== "wide" ? "dialog" : undefined}
        aria-modal={drawerOpen ? true : undefined}
        inert={layout.kind !== "wide" && !drawerOpen}
        onKeyDown={(event) => {
          if (!drawerOpen) return;
          if (event.key === "Escape") {
            event.preventDefault();
            setSidebarOpen(false);
          }
          if (event.key === "Tab") {
            const controls = Array.from(
              event.currentTarget.querySelectorAll<HTMLElement>(
                'button:not([disabled]),input:not([disabled]),[tabindex="0"]',
              ),
            ).filter((item) => item.getClientRects().length > 0);
            const first = controls[0],
              last = controls.at(-1);
            if (event.shiftKey && document.activeElement === first) {
              event.preventDefault();
              last?.focus();
            }
            if (!event.shiftKey && document.activeElement === last) {
              event.preventDefault();
              first?.focus();
            }
          }
        }}
      >
        <div className="brand-row">
          <div className="brand">
            <OctoMark />
            <span className="wordmark">Octo</span>
          </div>
          <button
            className="icon-button"
            aria-label="搜索会话"
            aria-expanded={searchOpen}
            onClick={() => {
              if (searchOpen) setQuery("");
              setSearchOpen((value) => !value);
            }}
          >
            <Search size={18} />
          </button>
          <button
            className="icon-button mobile-menu"
            aria-label="关闭会话列表"
            onClick={() => setSidebarOpen(false)}
          >
            <X size={17} />
          </button>
        </div>
        <button className="new-chat" onClick={newChat}>
          <PencilLine size={18} />
          <span>新对话</span>
          <kbd>Ctrl N</kbd>
        </button>
        {searchOpen && (
          <div className="search-box">
            <Search size={13} />
            <input
              ref={searchRef}
              aria-label="搜索会话标题"
              placeholder="搜索会话…"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
            <button
              className="icon-button"
              aria-label="清除搜索"
              onClick={() => {
                setQuery("");
                setSearchOpen(false);
              }}
            >
              <X size={12} />
            </button>
          </div>
        )}
        <nav className="session-nav" aria-label="会话导航">
          <section className="nav-section">
            <h2 className="section-heading">已置顶</h2>
            {filtered.filter((item) => item.pinned).map(sessionRow)}
          </section>
          <section className="nav-section">
            <h2 className="section-heading">最近对话</h2>
            {filtered.filter((item) => !item.pinned).map(sessionRow)}
            {!filtered.length && (
              <p className="section-heading">没有匹配的会话</p>
            )}
          </section>
        </nav>
        <div className="sidebar-footer">
          <div className="profile-dot">O</div>
          <div className="profile-meta">
            <strong>个人助手</strong>
            <span>界面预览</span>
          </div>
          <button
            className="icon-button"
            aria-label="打开设置"
            onClick={() => {
              setSettingsTab("general");
              setSettingsOpen(true);
            }}
          >
            <Settings2 size={17} />
          </button>
        </div>
        <ResizeDivider
          side="left"
          value={Math.min(leftWidth, leftMax)}
          min={192}
          max={leftMax}
          onChange={setLeftWidth}
          onDragging={setResizing}
        />
      </aside>
      <section className="workspace" inert={drawerOpen}>
        <header className="workspace-header">
          <button
            className="icon-button mobile-menu"
            aria-label="打开会话列表"
            onClick={() => setSidebarOpen(true)}
          >
            <Menu size={18} />
          </button>
          <span className="header-title">
            {treeOnly ? "对话树" : (active?.title ?? "新对话")}
          </span>
          <span className="header-spacer" />
          <span className="preview-badge">
            <span className="status-dot" />
            界面预览
          </span>
          <button
            className="icon-button"
            title={
              treeOnly ? "返回对话" : treeOpen ? "收起右侧窗口" : "展开右侧窗口"
            }
            aria-label="切换右侧窗口"
            aria-pressed={treeOpen}
            aria-controls="tree-panel"
            onClick={() => setTreeOpen((value) => !value)}
          >
            <PanelRight size={17} />
            {treeOnly && <span>返回对话</span>}
          </button>
        </header>
        <div className="workarea" data-tree={treeOpen}>
          <section
            className="conversation"
            aria-label="对话内容"
            hidden={treeOnly}
            inert={treeOnly}
          >
            {!active ? (
              <div className="empty-state">
                <OctoMark />
                <h1>今天，我们一起做点什么？</h1>
                <p>聊聊你的想法，或继续一件还没做完的事。</p>
              </div>
            ) : (
              <div
                className="messages"
                ref={messagesRef}
                onScroll={(event) => {
                  const list = event.currentTarget;
                  followingRef.current =
                    list.scrollHeight - list.scrollTop - list.clientHeight < 48;
                  if (followingRef.current) setUnread(false);
                }}
              >
                {active.messages.map((message) => (
                  <article
                    key={message.id}
                    className={`message ${message.role}`}
                  >
                    {message.role === "assistant" && (
                      <div className="assistant-label">
                        <OctoMark />
                        Octo<span className="message-status">演示</span>
                      </div>
                    )}
                    <div className="message-text">
                      {message.text ||
                        (message.status === "stopped" ? "已停止生成。" : "")}
                      {message.status === "streaming" && (
                        <span className="stream-cursor" aria-label="正在生成" />
                      )}
                    </div>
                    {message.role === "assistant" && (
                      <div className="message-toolbar">
                        <button
                          className="icon-button"
                          aria-label="复制回复"
                          onClick={() => copy(message.text)}
                          disabled={!message.text}
                        >
                          <Copy size={13} />
                        </button>
                        <span className="message-status">
                          {message.status === "stopped"
                            ? "已停止"
                            : message.status === "streaming"
                              ? "正在生成…"
                              : "本地示例"}
                        </span>
                      </div>
                    )}
                  </article>
                ))}
              </div>
            )}
            {unread && (
              <button
                className="new-content button"
                onClick={() => {
                  followingRef.current = true;
                  setUnread(false);
                  const list = messagesRef.current;
                  if (list) list.scrollTop = list.scrollHeight;
                }}
              >
                查看新内容 ↓
              </button>
            )}
            <div className="composer-area">
              <div className="composer-wrap">
                <div className="composer">
                  <textarea
                    ref={inputRef}
                    aria-label="输入消息"
                    placeholder="给 Octo 发消息…"
                    rows={2}
                    value={draft}
                    onChange={(event) =>
                      dispatch({ kind: "draft", text: event.target.value })
                    }
                    onKeyDown={(event) => {
                      if (
                        event.key === "Enter" &&
                        !event.shiftKey &&
                        !event.nativeEvent.isComposing
                      ) {
                        event.preventDefault();
                        send();
                      }
                    }}
                  />
                  <div className="composer-toolbar">
                    <button
                      className="icon-button"
                      title="选择附件（仅预览文件名）"
                      aria-label="选择附件"
                      onClick={() => fileRef.current?.click()}
                    >
                      <Plus size={21} />
                    </button>
                    <input
                      ref={fileRef}
                      hidden
                      type="file"
                      onChange={(event) => {
                        setAttachment(event.target.files?.[0]?.name ?? "");
                        event.target.value = "";
                      }}
                    />
                    <button
                      className="composer-mode"
                      aria-pressed={plan}
                      title="模式切换仅用于界面预览"
                      onClick={() => setPlan((value) => !value)}
                    >
                      <ShieldCheck size={14} />
                      {plan ? "规划模式" : "执行模式"}
                    </button>
                    <span className="header-spacer" />
                    <ModelPicker
                      config={models.config}
                      selection={selection}
                      onChange={setChosen}
                      onConfigure={() => {
                        setSettingsTab("models");
                        setSettingsOpen(true);
                      }}
                    />
                    <button
                      className="send-button"
                      aria-label={requestId ? "停止生成" : "发送消息"}
                      disabled={!requestId && !draft.trim()}
                      onClick={() =>
                        requestId ? dispatch({ kind: "stop" }) : send()
                      }
                    >
                      {requestId ? (
                        <Square size={12} fill="currentColor" />
                      ) : (
                        <ArrowUp size={17} />
                      )}
                    </button>
                  </div>
                  {attachment && (
                    <div className="attachment-chip">
                      <span>{attachment}</span>
                      <small>仅本地预览 · 不会发送</small>
                      <button
                        className="icon-button"
                        aria-label="移除附件"
                        onClick={() => setAttachment("")}
                      >
                        <X size={12} />
                      </button>
                    </div>
                  )}
                </div>
                {preferences.showHints && (
                  <p className="composer-note">
                    Enter 发送 · Shift + Enter 换行 ·
                    本地演示回复，尚未连接模型服务
                  </p>
                )}
              </div>
            </div>
          </section>
          <aside
            id="tree-panel"
            className="inspector"
            aria-label="预留对话树窗口"
            aria-hidden={!treeOpen}
            inert={!treeOpen}
          >
            <ResizeDivider
              side="right"
              value={Math.min(rightWidth, rightMax)}
              min={220}
              max={Math.max(220, rightMax)}
              onChange={setRightWidth}
              onDragging={setResizing}
            />
            <div className="inspector-inner">
              <div className="inspector-header">
                <GitBranch size={15} />
                对话树<span className="subtle-badge">预留窗口</span>
              </div>
              <div className="tree-placeholder">
                <div className="placeholder-icon">
                  <GitBranch size={23} />
                </div>
                <h2>留给之后的思路</h2>
                <p>
                  对话树将在这里呈现。
                  <br />
                  这一版先专注于对话体验。
                </p>
              </div>
              <div className="inspector-footer">
                <PanelRight size={12} />
                点击右上角图标可收起窗口
              </div>
            </div>
          </aside>
        </div>
      </section>
      {settingsOpen && (
        <Settings
          preferences={preferences}
          onChange={setPreferences}
          onClose={() => setSettingsOpen(false)}
          initialTab={settingsTab}
          models={models}
        />
      )}
      {toast && (
        <div className="toast" role="status">
          <Check size={14} />
          {toast}
        </div>
      )}
    </main>
  );
}
