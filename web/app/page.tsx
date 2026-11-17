"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { z } from "zod";
import { mergeMessagePage } from "@/lib/message-window";
import type { PageDirection } from "@/lib/message-window";
import {
  ArrowUpRight,
  Check,
  ChevronDown,
  ChevronRight,
  Copy,
  GitBranch,
  Minus,
  Moon,
  Plus,
  RefreshCw,
  Search,
  Send,
  Square,
  Sun,
  Terminal,
  Wrench,
  X,
} from "lucide-react";
import { MessageContent } from "@/components/message-content";
import { OctoMark } from "@/components/mark";
import { ResizeDivider } from "@/components/resize-divider";
import {
  applyRunEvent,
  createRunStreamState,
  healthSchema,
  isTerminalRunStatus,
  layoutRounds,
  openTopicTab,
  runAcceptedSchema,
  runEventEnvelopeSchema,
  runSnapshotSchema,
  sessionActionSchema,
  sessionListSchema,
  snapshotSchema,
  stateSchema,
  topicId,
} from "@/lib/topic-map";
import type {
  Message,
  RunSnapshot,
  RunStreamState,
  ServiceState,
  SessionSummary,
  Snapshot,
  TopicCommand,
  TopicId,
} from "@/lib/topic-map";

type TransportMode = "detecting" | "service" | "map";
type Connection =
  | { kind: "connecting" }
  | { kind: "connected" }
  | { kind: "disconnected"; reason: string };
type Editor =
  | { kind: "closed" }
  | {
      kind: "rename";
      session: Snapshot["session_id"];
      topic: TopicId;
      title: string;
    };

type MessageView = {
  session?: Snapshot["session_id"];
  topic?: TopicId;
  before?: number;
  after?: number;
};
type ScrollTarget = { kind: "entry"; id: string } | { kind: "latest" };

function generateClientRequestId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `req-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export default function Home() {
  const [transportMode, setTransportMode] =
    useState<TransportMode>("detecting");
  const modeRef = useRef<TransportMode>("detecting");
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [serviceState, setServiceState] = useState<ServiceState | null>(null);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [runStream, setRunStream] = useState<RunStreamState | null>(null);
  const runStreamRef = useRef<RunStreamState | null>(null);
  const [subscribedRunId, setSubscribedRunId] = useState<string | null>(null);
  const sendBaselineSeq = useRef<number | null>(null);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [connection, setConnection] = useState<Connection>({
    kind: "connecting",
  });
  const [tabs, setTabs] = useState<TopicId[]>([]);
  const [query, setQuery] = useState("");
  const [collapsed, setCollapsed] = useState(new Set<string>());
  const [zoom, setZoom] = useState(1);
  const [treeWidth, setTreeWidth] = useState(360);
  const [width, setWidth] = useState(0);
  const [mobile, setMobile] = useState<"tree" | "records">("tree");
  const [dragging, setDragging] = useState(false);
  const [dark, setDark] = useState(false);
  const [editor, setEditor] = useState<Editor>({ kind: "closed" });
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [modelNotice, setModelNotice] = useState<string | null>(null);
  const [scrollTarget, setScrollTarget] = useState<ScrollTarget | null>(null);
  const highlight = scrollTarget?.kind === "entry" ? scrollTarget.id : null;
  const [loadingPage, setLoadingPage] = useState(false);
  const snapshotRef = useRef<Snapshot | null>(null);
  const loadingRevision = useRef(0);
  const pendingRefresh = useRef(false);
  const recordsPane = useRef<HTMLDivElement>(null);
  const newerSentinel = useRef<HTMLDivElement>(null);
  const prependScroll = useRef<{ height: number; top: number } | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const frame = useRef<HTMLDivElement>(null);
  const token = useRef("");
  const serial = useRef(0);
  const view = useRef<MessageView>({});
  const dialog = useRef<HTMLDialogElement>(null);
  const topicMenu = useRef<HTMLDivElement>(null);
  const sessionMenu = useRef<HTMLDivElement>(null);
  const tabBar = useRef<HTMLDivElement>(null);

  function updateRunStream(next: RunStreamState | null) {
    runStreamRef.current = next;
    setRunStream(next);
  }

  async function apiRequest(
    path: string,
    options?: { method?: "GET" | "POST"; body?: unknown },
  ) {
    const isMap = modeRef.current === "map";
    const method = options?.method ?? (options?.body !== undefined ? "POST" : "GET");
    const headers: Record<string, string> = {};
    if (isMap && token.current) {
      headers["X-Octo-Token"] = token.current;
    }
    if (method === "POST") {
      headers["Content-Type"] = "application/json";
    }
    const response = await fetch(path, {
      method,
      cache: "no-store",
      credentials: isMap ? "omit" : "same-origin",
      redirect: "error",
      headers,
      body:
        method === "POST"
          ? JSON.stringify(options?.body ?? {})
          : undefined,
    });
    if (!response.ok) {
      const payload: unknown = await response.json().catch(() => null);
      const parsed = z
        .object({
          code: z.string().optional(),
          message: z.string().optional(),
          error: z.string().optional(),
        })
        .safeParse(payload);
      const fallback = isMap
        ? "无法连接当前终端，请在终端输入 /map"
        : "服务请求失败，请检查服务状态后重试";
      const message =
        parsed.success && (parsed.data.message || parsed.data.error)
          ? (parsed.data.message ?? parsed.data.error ?? fallback)
          : fallback;
      const err = new Error(message) as Error & {
        code?: string;
        status?: number;
      };
      err.code = parsed.success ? parsed.data.code : undefined;
      err.status = response.status;
      throw err;
    }
    const payload: unknown = await response.json();
    return payload;
  }

  async function request(path: string, command?: TopicCommand) {
    return apiRequest(path, command ? { method: "POST", body: command } : undefined);
  }

  async function refreshServiceMeta() {
    if (modeRef.current !== "service") return null;
    const [rawState, rawSessions] = await Promise.all([
      apiRequest("/api/state"),
      apiRequest("/api/sessions"),
    ]);
    const nextState = stateSchema.parse(rawState);
    const nextSessions = sessionListSchema.parse(rawSessions);
    setServiceState(nextState);
    setSessions(nextSessions.sessions);
    if (nextState.notice) {
      setModelNotice(nextState.notice);
    }
    if (nextState.active_run_id) {
      const current = runStreamRef.current?.snapshot;
      if (
        !current ||
        current.run_id !== nextState.active_run_id ||
        !isTerminalRunStatus(current.status)
      ) {
        setSubscribedRunId((prev) => prev ?? nextState.active_run_id);
      }
    }
    return nextState;
  }

  async function load(
    next?: MessageView,
    openTab = false,
    direction: PageDirection = "replace",
  ) {
    const refresh = next === undefined;
    if ((refresh || direction !== "replace") && loadingRevision.current) {
      if (refresh) pendingRefresh.current = true;
      return;
    }
    const isService = modeRef.current === "service";
    const previous = snapshotRef.current;
    const target = next ?? {
      session: view.current.session,
      topic: view.current.topic,
      after:
        previous?.viewed_topic_id === view.current.topic
          ? (previous?.after ?? undefined)
          : undefined,
    };
    const mode = refresh && previous ? "newer" : direction;
    const oldSession = view.current.session;
    if (mode === "replace") view.current = target;
    const revision = ++serial.current;
    loadingRevision.current = revision;
    setLoadingPage(true);
    try {
      const buildSnapshotUrl = (opts: {
        session?: string;
        topic?: string;
        before?: number;
        after?: number;
      }) => {
        const params = new URLSearchParams();
        if (isService && opts.session) params.set("session_id", opts.session);
        if (opts.topic) params.set("topic_id", opts.topic);
        if (opts.before !== undefined) params.set("before", String(opts.before));
        if (opts.after !== undefined) params.set("after", String(opts.after));
        const qs = params.toString();
        return qs ? `/api/snapshot?${qs}` : "/api/snapshot";
      };

      let data: unknown;
      try {
        data = await request(buildSnapshotUrl(target));
      } catch (cause) {
        const apiErr = cause as Error & { code?: string; status?: number };
        if (
          apiErr.code === "session_not_active" ||
          apiErr.code === "session_not_found" ||
          apiErr.status === 409
        ) {
          data = await request("/api/snapshot");
        } else if (
          cause instanceof Error &&
          cause.message === "Unknown topic"
        ) {
          data = await request(
            buildSnapshotUrl({ session: target.session }),
          );
        } else {
          throw cause;
        }
      }
      let parsed = snapshotSchema.parse(data);
      const replaced =
        oldSession !== undefined && oldSession !== parsed.session_id;
      if (replaced && (target.topic || target.before !== undefined || target.after !== undefined)) {
        parsed = snapshotSchema.parse(await request("/api/snapshot"));
      }
      if (revision !== serial.current) return;
      const current = snapshotRef.current;
      const sameView =
        current?.session_id === parsed.session_id &&
        current?.viewed_topic_id === parsed.viewed_topic_id;
      if (mode === "older" && sameView && recordsPane.current) {
        prependScroll.current = {
          height: recordsPane.current.scrollHeight,
          top: recordsPane.current.scrollTop,
        };
      } else if (mode === "replace" || !sameView) prependScroll.current = null;
      const merged = mergeMessagePage(
        current,
        parsed,
        replaced ? "replace" : mode,
      );
      view.current = {
        session: merged.session_id,
        topic: merged.viewed_topic_id,
      };
      if (replaced) {
        setCollapsed(new Set());
        setEditor({ kind: "closed" });
        setScrollTarget(null);
        if (
          runStreamRef.current &&
          runStreamRef.current.snapshot.session_id !== merged.session_id
        ) {
          updateRunStream(null);
          setSubscribedRunId(null);
        }
      }
      setTabs((previous) => {
        const existing = replaced ? [] : previous;
        return openTab
          ? openTopicTab(existing, parsed.viewed_topic_id)
          : existing;
      });
      snapshotRef.current = merged;
      setSnapshot(merged);
      setConnection({ kind: "connected" });
      return merged;
    } catch (cause) {
      if (revision === serial.current)
        setConnection({
          kind: "disconnected",
          reason: cause instanceof Error ? cause.message : "连接失败",
        });
    } finally {
      if (loadingRevision.current === revision) {
        loadingRevision.current = 0;
        setLoadingPage(false);
        if (pendingRefresh.current) {
          pendingRefresh.current = false;
          void load();
        }
      }
    }
  }

  async function jumpToLatest() {
    const loaded = await load({
      session: snapshotRef.current?.session_id,
      topic: snapshotRef.current?.viewed_topic_id,
    });
    if (loaded) setScrollTarget({ kind: "latest" });
  }

  async function syncAfterRunSettled(finalRun: RunSnapshot) {
    try {
      await refreshServiceMeta();
      await load();
      if (
        finalRun.status === "completed" &&
        runStreamRef.current?.snapshot.run_id === finalRun.run_id
      ) {
        updateRunStream(null);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "同步运行结果失败");
    } finally {
      setSubscribedRunId((prev) => (prev === finalRun.run_id ? null : prev));
      sendBaselineSeq.current = null;
    }
  }

  useEffect(() => {
    let disposed = false;
    let mapStream: EventSource | null = null;

    async function bootstrap() {
      const fragment = new URLSearchParams(location.hash.slice(1));
      const passed = fragment.get("token");
      if (passed) {
        sessionStorage.setItem("octo-map-token", passed);
        history.replaceState(null, "", location.pathname);
        token.current = passed;
        modeRef.current = "map";
        setTransportMode("map");
      } else {
        let detectedService = false;
        try {
          const response = await fetch("/api/health", {
            cache: "no-store",
            credentials: "same-origin",
            redirect: "error",
          });
          if (response.ok) {
            const parsed = healthSchema.safeParse(await response.json());
            if (parsed.success && parsed.data.ok) {
              detectedService = true;
            }
          }
        } catch {
          detectedService = false;
        }
        if (disposed) return;

        if (detectedService) {
          modeRef.current = "service";
          setTransportMode("service");
        } else {
          const stored = sessionStorage.getItem("octo-map-token") ?? "";
          if (!stored) {
            modeRef.current = "map";
            setTransportMode("map");
            setConnection({
              kind: "disconnected",
              reason: "无法连接 Octo 服务，或在运行中的终端输入 /map 连接当前会话。",
            });
            return;
          }
          token.current = stored;
          modeRef.current = "map";
          setTransportMode("map");
        }
      }

      if (disposed) return;
      const isService = modeRef.current === "service";
      mapStream = new EventSource(
        isService
          ? "/api/events"
          : `/api/events?token=${encodeURIComponent(token.current)}`,
      );
      mapStream.onmessage = () => {
        if (disposed) return;
        if (isService) {
          void refreshServiceMeta().catch(() => {});
        }
        void load();
      };
      mapStream.onerror = () => {
        if (!disposed) {
          setConnection({
            kind: "disconnected",
            reason: isService
              ? "连接中断，服务端可能仍在运行"
              : "终端连接已断开，正在重新连接…",
          });
        }
      };

      if (isService) {
        await refreshServiceMeta().catch(() => {});
      }
      await load();
    }

    void bootstrap();
    return () => {
      disposed = true;
      ++serial.current;
      loadingRevision.current = 0;
      pendingRefresh.current = false;
      mapStream?.close();
    };
  }, []);

  useEffect(() => {
    if (transportMode !== "service" || !subscribedRunId) return;
    let disposed = false;
    const runId = subscribedRunId;
    const stream = new EventSource(
      `/api/runs/${encodeURIComponent(runId)}/events`,
    );

    const handleRunEvent = (event: MessageEvent<string>) => {
      if (disposed) return;
      let envelope;
      try {
        envelope = runEventEnvelopeSchema.parse(JSON.parse(event.data));
      } catch {
        return;
      }
      const next = applyRunEvent(runStreamRef.current, envelope);
      if (!next) return;
      updateRunStream(next);
      setConnection({ kind: "connected" });
      if (
        envelope.kind === "run_status" &&
        isTerminalRunStatus(next.snapshot.status)
      ) {
        stream.close();
        void syncAfterRunSettled(next.snapshot);
      }
    };

    stream.addEventListener("run_event", handleRunEvent as EventListener);
    stream.onerror = () => {
      if (disposed) return;
      setConnection({
        kind: "disconnected",
        reason: "连接中断，服务端可能仍在运行",
      });
      void fetch(`/api/runs/${encodeURIComponent(runId)}`, {
        cache: "no-store",
        credentials: "same-origin",
      })
        .then(async (res) => {
          if (disposed) return;
          if (res.status === 404) {
            stream.close();
            updateRunStream(null);
            setSubscribedRunId(null);
            setError("服务已重启，运行状态未保留，已刷新历史记录核对结果。");
            await refreshServiceMeta().catch(() => {});
            await load();
            return;
          }
          if (res.ok) {
            const snap = runSnapshotSchema.parse(await res.json());
            updateRunStream(createRunStreamState(snap));
            if (isTerminalRunStatus(snap.status)) {
              stream.close();
              await syncAfterRunSettled(snap);
            }
          }
        })
        .catch(() => {});
    };

    return () => {
      disposed = true;
      stream.removeEventListener("run_event", handleRunEvent as EventListener);
      stream.close();
    };
  }, [transportMode, subscribedRunId]);

  useEffect(() => {
    if (!frame.current) return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry) setWidth(entry.contentRect.width);
    });
    observer.observe(frame.current);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (editor.kind === "closed") dialog.current?.close();
    else if (!dialog.current?.open) dialog.current?.showModal();
  }, [editor]);

  useLayoutEffect(() => {
    if (scrollTarget?.kind === "entry")
      document
        .getElementById("entry-" + scrollTarget.id)
        ?.scrollIntoView({ block: "center", behavior: "instant" });
    else if (scrollTarget?.kind === "latest" && recordsPane.current)
      recordsPane.current.scrollTo({
        top: recordsPane.current.scrollHeight,
        behavior: "instant",
      });
  }, [scrollTarget]);

  useLayoutEffect(() => {
    const previous = prependScroll.current;
    if (previous && recordsPane.current) {
      recordsPane.current.scrollTo({
        top: previous.top + recordsPane.current.scrollHeight - previous.height,
        behavior: "instant",
      });
      prependScroll.current = null;
    }
  }, [snapshot?.messages]);

  useEffect(() => {
    if (
      connection.kind !== "connected" ||
      !snapshot?.has_newer ||
      snapshot.after === null ||
      !newerSentinel.current ||
      !recordsPane.current ||
      typeof IntersectionObserver === "undefined"
    )
      return;
    const topic = snapshot.viewed_topic_id,
      after = snapshot.after,
      sess = snapshot.session_id;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting))
          void load({ session: sess, topic, after }, false, "newer");
      },
      { root: recordsPane.current, rootMargin: "0px 0px 160px 0px" },
    );
    observer.observe(newerSentinel.current);
    return () => observer.disconnect();
  }, [
    snapshot?.session_id,
    snapshot?.viewed_topic_id,
    snapshot?.after,
    snapshot?.has_newer,
    mobile,
    loadingPage,
    connection.kind,
  ]);

  useEffect(() => {
    tabBar.current
      ?.querySelector('[role="tab"][aria-selected="true"]')
      ?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [tabs, mobile, snapshot?.viewed_topic_id]);

  async function locate(entry: Message["id"]) {
    try {
      const params = new URLSearchParams({ entry_id: entry });
      if (modeRef.current === "service" && snapshotRef.current?.session_id) {
        params.set("session_id", snapshotRef.current.session_id);
      }
      const anchor = z
        .object({
          topic_id: topicId,
          seq: z.number().int(),
          entry_id: z.string(),
        })
        .parse(await request("/api/anchor?" + params.toString()));
      setMobile("records");
      setScrollTarget(null);
      const loaded = await load(
        {
          session: snapshotRef.current?.session_id,
          topic: anchor.topic_id,
          before: anchor.seq + 1,
        },
        true,
      );
      if (loaded) setScrollTarget({ kind: "entry", id: anchor.entry_id });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法定位原文");
    }
  }

  const rows = snapshot ? snapshot.topics : [];
  const tree = layoutRounds(
    snapshot?.topics ?? [],
    snapshot?.turns ?? [],
    collapsed,
    query,
  );
  const selected = snapshot?.topics.find(
    (topic) => topic.id === snapshot.viewed_topic_id,
  );
  const current = snapshot?.topics.find(
    (topic) => topic.id === snapshot.selected_topic_id,
  );
  const activeRun = runStream?.snapshot ?? null;
  const activeRunTopic = snapshot?.topics.find(
    (topic) =>
      topic.id === (activeRun?.topic_id ?? snapshot.active_topic_id),
  );
  const isServiceMode = transportMode === "service";
  const isRunRunning = Boolean(
    (activeRun && !isTerminalRunStatus(activeRun.status)) ||
      snapshot?.running ||
      serviceState?.running,
  );
  const showLiveCard = Boolean(
    isServiceMode &&
      activeRun &&
      snapshot &&
      activeRun.session_id === snapshot.session_id &&
      activeRun.topic_id === selected?.id,
  );
  const showProvisionalUser = Boolean(
    showLiveCard &&
      activeRun &&
      sendBaselineSeq.current !== null &&
      !snapshot?.messages.some(
        (message) =>
          message.seq > (sendBaselineSeq.current ?? 0) &&
          message.role === "user" &&
          message.text === activeRun.user_text,
      ),
  );
  const split = width >= 780;
  const maxWidth = Math.max(260, Math.min(640, width - 390));
  const panelWidth = Math.max(260, Math.min(treeWidth, maxWidth));
  const connected = connection.kind === "connected";

  async function command(body: TopicCommand, target?: TopicId) {
    setBusy(true);
    setError("");
    try {
      const result = z
        .object({ topic_id: topicId })
        .parse(await request("/api/topics", body));
      setEditor({ kind: "closed" });
      setMobile("records");
      await load(
        { session: body.session_id, topic: target ?? result.topic_id },
        true,
      );
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "操作失败，请刷新检查结果",
      );
    } finally {
      setBusy(false);
    }
  }

  async function activateTopic(
    id: TopicId,
    entry: Message["id"] | null = null,
  ) {
    if (!snapshot || busy) return;
    setScrollTarget(null);
    await command({
      action: "select",
      session_id: snapshot.session_id,
      topic_id: id,
    });
    if (entry) await locate(entry);
    else setScrollTarget({ kind: "latest" });
  }

  function create(anchor: Message["id"] | null = null) {
    if (!selected || !snapshot) return;
    const number = snapshot.topics.length;
    let name = `新分支 ${number}`;
    for (
      let suffix = number;
      snapshot.topics.some((topic) => topic.title === name);
      suffix++
    )
      name = `新分支 ${suffix + 1}`;
    void command({
      action: "create",
      session_id: snapshot.session_id,
      topic_id: selected.id,
      title: name,
      anchor_entry_id: anchor,
      activate: true,
    });
  }

  async function switchSession(targetSessionId: Snapshot["session_id"]) {
    if (!isServiceMode || busy || isRunRunning) return;
    setBusy(true);
    setError("");
    try {
      const result = sessionActionSchema.parse(
        await apiRequest(
          `/api/sessions/${encodeURIComponent(targetSessionId)}/activate`,
          { method: "POST", body: {} },
        ),
      );
      setServiceState(result.state);
      setModelNotice(result.notice);
      updateRunStream(null);
      setSubscribedRunId(null);
      setTabs([]);
      setCollapsed(new Set());
      setScrollTarget(null);
      await refreshServiceMeta();
      await load({ session: result.state.session_id });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "切换会话失败");
    } finally {
      setBusy(false);
    }
  }

  async function createNewSession() {
    if (!isServiceMode || busy || isRunRunning) return;
    setBusy(true);
    setError("");
    try {
      const result = sessionActionSchema.parse(
        await apiRequest("/api/sessions", { method: "POST", body: {} }),
      );
      setServiceState(result.state);
      setModelNotice(result.notice);
      updateRunStream(null);
      setSubscribedRunId(null);
      setTabs([]);
      setCollapsed(new Set());
      setScrollTarget(null);
      await refreshServiceMeta();
      await load({ session: result.state.session_id });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "新建会话失败");
    } finally {
      setBusy(false);
    }
  }

  async function sendMessage() {
    if (!isServiceMode || !snapshot || !selected || isRunRunning || sending) {
      return;
    }
    const text = draft;
    if (!text.trim()) return;
    setSending(true);
    setError("");
    const clientRequestId = generateClientRequestId();
    try {
      const accepted = runAcceptedSchema.parse(
        await apiRequest(
          `/api/sessions/${encodeURIComponent(snapshot.session_id)}/runs`,
          {
            method: "POST",
            body: {
              client_request_id: clientRequestId,
              topic_id: selected.id,
              text,
            },
          },
        ),
      );
      setDraft("");
      const nowSeconds = Date.now() / 1000;
      const initialRun: RunSnapshot = {
        run_id: accepted.run_id,
        client_request_id: accepted.client_request_id,
        session_id: accepted.session_id,
        topic_id: accepted.topic_id,
        status: accepted.status,
        seq: 0,
        user_text: text,
        assistant_text: "",
        thinking_text: "",
        committed_assistant_text: "",
        committed_thinking_text: "",
        tool_executions: [],
        error_code: null,
        error_message: null,
        created_at: nowSeconds,
        updated_at: nowSeconds,
        finished_at: null,
      };
      updateRunStream(createRunStreamState(initialRun));
      sendBaselineSeq.current = snapshot.after ?? 0;
      setSubscribedRunId(accepted.run_id);
      if (snapshot.has_newer || highlight) {
        await jumpToLatest();
      } else {
        setScrollTarget({ kind: "latest" });
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "发送失败");
    } finally {
      setSending(false);
    }
  }

  async function cancelActiveRun() {
    const runId = activeRun?.run_id ?? serviceState?.active_run_id;
    if (!isServiceMode || !runId || cancelling) return;
    setCancelling(true);
    setError("");
    try {
      const snap = runSnapshotSchema.parse(
        await apiRequest(`/api/runs/${encodeURIComponent(runId)}/cancel`, {
          method: "POST",
          body: {},
        }),
      );
      updateRunStream(createRunStreamState(snap));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "停止生成失败");
    } finally {
      setCancelling(false);
    }
  }

  async function copy(message: Message) {
    try {
      await navigator.clipboard.writeText(message.text);
      setCopied(message.id);
    } catch {
      setError("复制失败，可选中消息正文手动复制。");
    }
  }

  async function refreshAll() {
    if (modeRef.current === "service") {
      await refreshServiceMeta().catch(() => {});
    }
    await load();
  }

  return (
    <main
      className={`map-app ${dragging ? "is-dragging" : ""}`}
      data-theme={dark ? "dark" : "light"}
    >
      <header className="topbar">
        <div className="brand">
          <OctoMark />
          <strong>Octo</strong>
        </div>
        <div className="session-strip" title={snapshot?.title}>
          <Terminal size={17} />
          {isServiceMode ? (
            <>
              <button
                className="session-picker-trigger"
                aria-label="切换或新建会话"
                aria-haspopup="menu"
                popoverTarget="session-picker"
                disabled={!connected || busy || !snapshot}
              >
                <strong>{snapshot?.title ?? "加载会话中"}</strong>
                <ChevronDown size={13} />
              </button>
              <div
                ref={sessionMenu}
                id="session-picker"
                className="topic-options session-options"
                popover="auto"
                role="menu"
                aria-label="会话列表"
              >
                <button
                  className="session-create-item"
                  disabled={busy || isRunRunning || !connected}
                  onClick={() => {
                    sessionMenu.current?.hidePopover();
                    void createNewSession();
                  }}
                >
                  <span>
                    <Plus size={14} /> 新建会话
                  </span>
                </button>
                {sessions.map((item) => (
                  <button
                    key={item.id}
                    role="menuitemradio"
                    aria-checked={item.id === snapshot?.session_id}
                    disabled={
                      busy ||
                      (isRunRunning && item.id !== snapshot?.session_id)
                    }
                    onClick={() => {
                      sessionMenu.current?.hidePopover();
                      if (item.id !== snapshot?.session_id) {
                        void switchSession(item.id);
                      }
                    }}
                  >
                    <span>
                      {item.title} · <small>{item.model}</small>
                    </span>
                    {item.id === snapshot?.session_id && <Check size={14} />}
                  </button>
                ))}
              </div>
              {serviceState && (
                <span className="model-pill" title={serviceState.provider_name}>
                  {serviceState.model_display_name}
                </span>
              )}
              <span>当前话题：{current?.title ?? "—"}</span>
            </>
          ) : (
            <>
              <strong>{snapshot?.title ?? "连接你的终端会话"}</strong>
              <span>终端当前：{current?.title ?? "—"}</span>
            </>
          )}
          {isRunRunning && (
            <span className="running">
              <i />
              正在生成
            </span>
          )}
        </div>
        <div className="top-actions">
          <span className={`connection ${connected ? "online" : ""}`}>
            <i />
            {connected
              ? isServiceMode
                ? "已连接服务"
                : "已连接终端"
              : connection.kind === "connecting"
                ? "正在连接"
                : "连接中断"}
          </span>
          <button
            className="icon-button"
            aria-label="刷新话题"
            onClick={() => void refreshAll()}
          >
            <RefreshCw size={17} />
          </button>
          <button
            className="icon-button"
            aria-label={dark ? "切换浅色主题" : "切换深色主题"}
            onClick={() => setDark(!dark)}
          >
            {dark ? <Sun size={18} /> : <Moon size={18} />}
          </button>
        </div>
      </header>
      {connection.kind === "disconnected" && (
        <div className="notice" role="status">
          {connection.reason}
          <button onClick={() => void refreshAll()}>重新连接</button>
        </div>
      )}
      {modelNotice && (
        <div className="notice" role="status">
          <span>{modelNotice}</span>
          <button aria-label="关闭模型提示" onClick={() => setModelNotice(null)}>
            <X size={16} />
          </button>
        </div>
      )}
      {error && editor.kind === "closed" && (
        <div className="notice error" role="alert">
          {error}
          <button aria-label="关闭提示" onClick={() => setError("")}>
            <X size={16} />
          </button>
        </div>
      )}
      <div className="branch-toolbar">
        <div
          className="branch-tabs"
          ref={tabBar}
          role="tablist"
          aria-label="已打开的话题"
          onKeyDown={(event) => {
            if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key))
              return;
            const buttons = Array.from(
              event.currentTarget.querySelectorAll<HTMLButtonElement>(
                "button[role=tab]",
              ),
            );
            const current = buttons.findIndex(
              (button) => button === document.activeElement,
            );
            if (current < 0) return;
            event.preventDefault();
            const next =
              event.key === "Home"
                ? 0
                : event.key === "End"
                  ? buttons.length - 1
                  : (current +
                      (event.key === "ArrowRight" ? 1 : -1) +
                      buttons.length) %
                    buttons.length;
            buttons[next]?.focus();
            buttons[next]?.click();
          }}
        >
          <button
            role="tab"
            aria-selected={mobile === "tree"}
            onClick={() => setMobile("tree")}
          >
            <GitBranch size={15} />
            话题地图
          </button>
          {tabs.map((id) => {
            const topic = snapshot?.topics.find((item) => item.id === id);
            if (!topic) return null;
            return (
              <div
                className={`branch-tab ${mobile === "records" && selected?.id === id ? "active" : ""}`}
                key={id}
              >
                <button
                  role="tab"
                  disabled={busy || !connected}
                  aria-selected={mobile === "records" && selected?.id === id}
                  title={topic.title}
                  onClick={() => {
                    setMobile("records");
                    void activateTopic(id);
                  }}
                >
                  {topic.title}
                </button>
                <button
                  className="close-tab"
                  aria-label={`关闭${topic.title}标签`}
                  onClick={() => {
                    const index = tabs.indexOf(id);
                    const remaining = tabs.filter((item) => item !== id);
                    setTabs(remaining);
                    if (selected?.id === id) {
                      const neighbor = remaining[Math.max(0, index - 1)];
                      if (neighbor) void activateTopic(neighbor);
                      else setMobile("tree");
                    }
                  }}
                >
                  <X size={12} />
                </button>
              </div>
            );
          })}
        </div>
        <div className="branch-controls">
          <button
            className="topic-switcher"
            aria-label="切换当前分支"
            aria-haspopup="menu"
            popoverTarget="topic-picker"
            disabled={!connected || busy || !snapshot}
          >
            <span>{current?.title ?? "等待连接"}</span>
            <ChevronDown size={13} />
          </button>
          <div
            ref={topicMenu}
            id="topic-picker"
            className="topic-options"
            popover="auto"
            role="menu"
            aria-label="选择分支"
          >
            {snapshot?.topics.map((topic) => (
              <button
                key={topic.id}
                role="menuitemradio"
                aria-checked={topic.id === snapshot.selected_topic_id}
                disabled={busy || !connected}
                onClick={() => {
                  topicMenu.current?.hidePopover();
                  void activateTopic(topic.id);
                }}
              >
                <span>
                  {topic.kind === "trunk" ? "主干" : topic.branch_label} ·{" "}
                  {topic.title}
                </span>
                {topic.id === snapshot.selected_topic_id && <Check size={14} />}
              </button>
            ))}
          </div>
          <button
            className="new-branch"
            disabled={!connected || busy || !selected}
            onClick={() => create()}
            title="从正在查看的话题创建并切换"
          >
            <Plus size={14} />
            新建分支
          </button>
        </div>
      </div>
      <div
        ref={frame}
        className={`workspace ${split ? "split" : "compact"} ${mobile === "tree" ? "map-surface" : "topic-surface"}`}
      >
        <section
          className="tree-panel"
          style={
            mobile === "tree"
              ? { width: "100%" }
              : split
                ? { width: panelWidth }
                : { display: "none" }
          }
          aria-label="话题导航"
        >
          <div className="panel-heading">
            <div>
              <h1>对话轮次</h1>
            </div>
            <button
              className="icon-button"
              aria-label="新建并切换分支"
              disabled={!connected || !selected || busy}
              onClick={() => create()}
            >
              <Plus size={19} />
            </button>
          </div>
          <label className="search">
            <Search size={16} />
            <input
              aria-label="搜索话题"
              placeholder="查找一个话题…"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
          <nav className="tree-scroll" aria-label="话题树">
            {rows.length ? (
              <div
                className="tree-space"
                style={{
                  width: tree.width * zoom,
                  height: tree.height * zoom,
                }}
              >
                <div
                  className="tree-nodes"
                  style={{
                    transform: `scale(${zoom})`,
                    width: tree.width,
                    height: tree.height,
                  }}
                >
                  <svg
                    className="tree-connections"
                    width={tree.width}
                    height={tree.height}
                    aria-hidden="true"
                  >
                    {tree.edges.map((edge) => (
                      <path
                        key={edge.id}
                        d={edge.path}
                        className={edge.trunk ? "trunk-edge" : "branch-edge"}
                      />
                    ))}
                  </svg>
                  {tree.nodes.map(
                    ({
                      topic,
                      depth,
                      hasChildren,
                      x,
                      y,
                      id,
                      title: turnTitle,
                      entry_id,
                      number,
                      message_count,
                      kind,
                      isLatest,
                    }) => (
                      <div
                        className={`topic-row node-${kind} depth-${depth} ${snapshot?.viewed_topic_id === topic.id && (highlight ? highlight === entry_id : isLatest) ? "viewed" : ""}`}
                        key={id}
                        style={{ left: x, top: y }}
                      >
                        {hasChildren ? (
                          <button
                            className="collapse"
                            aria-label={`${collapsed.has(id) ? "展开" : "折叠"}${topic.title}`}
                            aria-expanded={!collapsed.has(id)}
                            onClick={() =>
                              setCollapsed((previous) => {
                                const next = new Set(previous);
                                if (next.has(id)) next.delete(id);
                                else next.add(id);
                                return next;
                              })
                            }
                          >
                            {collapsed.has(id) ? (
                              <ChevronRight size={15} />
                            ) : (
                              <ChevronDown size={15} />
                            )}
                          </button>
                        ) : (
                          <span className="node-dot" />
                        )}
                        <button
                          className="topic-card"
                          disabled={busy || !connected}
                          aria-current={
                            snapshot?.viewed_topic_id === topic.id
                              ? "true"
                              : undefined
                          }
                          onClick={() => {
                            void activateTopic(topic.id, entry_id);
                          }}
                        >
                          <span className={`branch-identity ${topic.kind}`}>
                            {topic.branch_label}
                            <small>
                              {topic.parent_label
                                ? `属 ${topic.parent_label}`
                                : "会话根"}
                            </small>
                          </span>
                          <span className="round-label">
                            {kind === "root"
                              ? "会话起点"
                              : kind === "empty"
                                ? "尚未开始"
                                : `第 ${number} 轮`}
                          </span>
                          <span className="topic-title" title={turnTitle}>
                            {turnTitle}
                          </span>
                          <span className="node-meta">
                            {kind === "turn"
                              ? `${message_count} 条消息`
                              : kind === "empty"
                                ? "未产生对话"
                                : "共享会话"}
                            {snapshot?.selected_topic_id === topic.id &&
                              isLatest && (
                                <em>
                                  {isServiceMode ? "当前选中" : "终端当前"}
                                </em>
                              )}
                            {isRunRunning &&
                              (activeRun?.topic_id ??
                                snapshot?.active_topic_id) === topic.id && (
                                <em className="running">
                                  <i />
                                  生成中
                                </em>
                              )}
                          </span>
                        </button>
                      </div>
                    ),
                  )}
                </div>
              </div>
            ) : (
              <div className="empty-small">
                {snapshot
                  ? "没有找到这个话题"
                  : isServiceMode
                    ? "正在加载会话话题…"
                    : "在终端输入 /map，查看真实话题。"}
              </div>
            )}
          </nav>
          <footer className="tree-footer">
            <span>
              {snapshot?.turns.length ?? 0} 轮对话 ·{" "}
              {snapshot?.topics.length ?? 0} 条线
            </span>
            <div>
              <button
                className="fit-map"
                onClick={() =>
                  setZoom(
                    Math.max(0.35, Math.min(1, (width - 48) / tree.width)),
                  )
                }
              >
                适应窗口
              </button>
              <button
                className="icon-button"
                aria-label="缩小话题树"
                disabled={zoom <= 0.35}
                onClick={() =>
                  setZoom(Math.max(0.35, +(zoom - 0.1).toFixed(2)))
                }
              >
                <Minus size={15} />
              </button>
              <button
                className="zoom-value"
                aria-label="重置缩放"
                onClick={() => setZoom(1)}
              >
                {Math.round(zoom * 100)}%
              </button>
              <button
                className="icon-button"
                aria-label="放大话题树"
                disabled={zoom >= 1.5}
                onClick={() => setZoom(Math.min(1.5, +(zoom + 0.1).toFixed(1)))}
              >
                <Plus size={15} />
              </button>
            </div>
          </footer>
        </section>
        {split && mobile === "records" && (
          <ResizeDivider
            side="left"
            value={panelWidth}
            min={260}
            max={maxWidth}
            onChange={setTreeWidth}
            onDragging={setDragging}
            label="调整话题树宽度"
            resetValue={360}
          />
        )}
        <section
          className="records-panel"
          style={mobile === "tree" ? { display: "none" } : undefined}
          aria-label="话题消息记录"
        >
          <div className="record-heading">
            <div>
              <span className="eyebrow">
                {selected?.branch_label ?? "话题记录"}
              </span>
              <h2>{selected?.title ?? "每条想法，都有来处"}</h2>
            </div>
            {selected && snapshot && (
              <div className="record-actions">
                <button
                  disabled={!connected || busy || selected.kind === "trunk"}
                  onClick={() => {
                    setTitle(selected.title);
                    setError("");
                    setEditor({
                      kind: "rename",
                      session: snapshot.session_id,
                      topic: selected.id,
                      title: selected.title,
                    });
                  }}
                >
                  重命名
                </button>
                <button
                  className="primary"
                  disabled={
                    !connected ||
                    busy ||
                    selected.id === snapshot.selected_topic_id
                  }
                  onClick={() =>
                    void command({
                      action: "select",
                      session_id: snapshot.session_id,
                      topic_id: selected.id,
                    })
                  }
                >
                  <ArrowUpRight size={16} />
                  {selected.id === snapshot.selected_topic_id
                    ? isServiceMode
                      ? "当前话题"
                      : "终端当前话题"
                    : "在此继续"}
                </button>
              </div>
            )}
          </div>
          <div
            className="records-scroll"
            ref={recordsPane}
            key={`${snapshot?.session_id}:${selected?.id}`}
          >
            {selected?.anchor_entry_id && (
              <div className="anchor-note">
                <GitBranch size={16} />
                <span>
                  从父话题中的消息建立 ·{" "}
                  <code>{selected.anchor_entry_id.slice(0, 10)}</code>
                </span>
                <button
                  onClick={() => {
                    if (selected.anchor_entry_id)
                      void locate(selected.anchor_entry_id);
                  }}
                >
                  定位原文
                </button>
              </div>
            )}
            {snapshot?.has_more && snapshot.before !== null && (
              <button
                className="page-button"
                onClick={() =>
                  void load(
                    {
                      session: snapshot.session_id,
                      topic: snapshot.viewed_topic_id,
                      before: snapshot.before ?? undefined,
                    },
                    false,
                    "older",
                  )
                }
              >
                加载更早的记录
              </button>
            )}
            {(snapshot?.has_newer || highlight) && (
              <button
                className="page-button"
                onClick={() => void jumpToLatest()}
              >
                回到最新记录
              </button>
            )}
            {snapshot?.messages.length ? (
              snapshot.messages.map((message) => (
                <article
                  className={`message role-${message.role} ${highlight === message.id ? "highlighted" : ""}`}
                  id={"entry-" + message.id}
                  key={message.id}
                >
                  <div className="message-meta">
                    <strong>
                      {message.role === "user"
                        ? "你"
                        : message.role === "assistant"
                          ? "Octo"
                          : message.role === "toolResult"
                            ? "工具结果"
                            : "会话记录"}
                    </strong>
                    <time>
                      {new Date(message.timestamp * 1000).toLocaleString(
                        "zh-CN",
                        { hour12: false },
                      )}
                    </time>
                    <code>{message.id.slice(0, 8)}</code>
                  </div>
                  <MessageContent
                    role={message.role}
                    text={message.text || "（本条包含工具调用或非文本内容）"}
                  />
                  <div className="message-actions">
                    <button
                      aria-label="复制消息"
                      onClick={() => void copy(message)}
                    >
                      {copied === message.id ? (
                        <Check size={14} />
                      ) : (
                        <Copy size={14} />
                      )}
                      复制
                    </button>
                    <button
                      disabled={!connected || busy}
                      onClick={() => create(message.id)}
                    >
                      <GitBranch size={14} />
                      从此建立子话题
                    </button>
                  </div>
                </article>
              ))
            ) : !showLiveCard ? (
              <div className="empty-records">
                <OctoMark />
                <h3>
                  {selected
                    ? "这里，留给新的想法"
                    : "用地图整理思路，在此继续对话"}
                </h3>
                <p>
                  {isServiceMode
                    ? "在下方输入问题，立即沿当前话题开始对话。"
                    : selected
                      ? "回到终端，直接沿当前主线提问。"
                      : "模型、工具和配置都留在终端。"}
                </p>
              </div>
            ) : null}
            {showProvisionalUser && activeRun && (
              <article className="message role-user">
                <div className="message-meta">
                  <strong>你</strong>
                  <span>发送中</span>
                </div>
                <div className="message-text">{activeRun.user_text}</div>
              </article>
            )}
            {showLiveCard && activeRun && (
              <article
                className={`message live-run status-${activeRun.status}`}
                aria-live="polite"
              >
                <div className="message-meta">
                  <strong>Octo</strong>
                  <span className="live-status-pill">
                    {activeRun.status === "starting"
                      ? "正在准备…"
                      : activeRun.status === "running"
                        ? "正在生成…"
                        : activeRun.status === "cancelling"
                          ? "正在停止…"
                          : activeRun.status === "cancelled"
                            ? "已停止生成"
                            : activeRun.status === "failed"
                              ? "生成失败"
                              : "已完成"}
                  </span>
                  <code>{activeRun.run_id.slice(0, 8)}</code>
                  {isTerminalRunStatus(activeRun.status) && (
                    <button
                      className="dismiss-live"
                      aria-label="关闭运行状态"
                      onClick={() => updateRunStream(null)}
                    >
                      <X size={14} />
                    </button>
                  )}
                </div>
                {activeRun.thinking_text && (
                  <details className="live-details">
                    <summary>思考过程</summary>
                    <pre className="live-pre">{activeRun.thinking_text}</pre>
                  </details>
                )}
                {activeRun.tool_executions.length > 0 && (
                  <div className="live-tools">
                    {activeRun.tool_executions.map((tool) => (
                      <details
                        className={`live-tool tool-${tool.status}`}
                        key={tool.tool_call_id}
                      >
                        <summary>
                          <Wrench size={13} />
                          <span>{tool.tool_name}</span>
                          <em>
                            {tool.status === "running"
                              ? "执行中"
                              : tool.status === "error"
                                ? "出错"
                                : "完成"}
                          </em>
                        </summary>
                        <div className="live-tool-body">
                          <pre className="live-pre">
                            {JSON.stringify(tool.args, null, 2)}
                          </pre>
                          {(tool.result_text || tool.partial_text) && (
                            <pre className="live-pre">
                              {tool.result_text || tool.partial_text}
                            </pre>
                          )}
                        </div>
                      </details>
                    ))}
                  </div>
                )}
                {activeRun.assistant_text ? (
                  <MessageContent
                    role="assistant"
                    text={activeRun.assistant_text}
                  />
                ) : !isTerminalRunStatus(activeRun.status) ? (
                  <p className="live-placeholder">等待模型响应…</p>
                ) : null}
                {activeRun.error_message && (
                  <p className="form-error" role="alert">
                    {activeRun.error_message}
                  </p>
                )}
              </article>
            )}
            {snapshot?.has_newer && snapshot.after !== null && (
              <div ref={newerSentinel} className="newer-page">
                <button
                  className="page-button"
                  disabled={loadingPage}
                  onClick={() =>
                    void load(
                      {
                        session: snapshot.session_id,
                        topic: snapshot.viewed_topic_id,
                        after: snapshot.after ?? undefined,
                      },
                      false,
                      "newer",
                    )
                  }
                >
                  {loadingPage ? "正在加载后续记录…" : "加载后续记录"}
                </button>
              </div>
            )}
          </div>
          {isServiceMode ? (
            <footer className="composer">
              <div className="composer-meta">
                <span>
                  发送目标：{selected?.branch_label ?? "主干"} ·{" "}
                  <strong>{selected?.title ?? "主线"}</strong>
                </span>
                {isRunRunning &&
                  activeRunTopic &&
                  activeRunTopic.id !== selected?.id && (
                    <span className="other-topic-run">
                      正在「{activeRunTopic.title}」生成中
                      <button
                        type="button"
                        onClick={() => void activateTopic(activeRunTopic.id)}
                      >
                        切回查看
                      </button>
                    </span>
                  )}
                <span className="shared-note">同一会话共享历史</span>
              </div>
              <div className="composer-row">
                <textarea
                  aria-label="输入消息"
                  rows={2}
                  value={draft}
                  onChange={(event) => setDraft(event.target.value)}
                  onKeyDown={(event) => {
                    if (
                      event.key === "Enter" &&
                      !event.shiftKey &&
                      !event.nativeEvent.isComposing
                    ) {
                      event.preventDefault();
                      void sendMessage();
                    }
                  }}
                  placeholder={
                    selected
                      ? `向「${selected.title}」提问（Enter 发送，Shift+Enter 换行）…`
                      : "等待加载话题…"
                  }
                  disabled={!connected || !selected}
                />
                {isRunRunning ? (
                  <button
                    type="button"
                    className="stop-button"
                    disabled={cancelling || !(activeRun?.run_id ?? serviceState?.active_run_id)}
                    onClick={() => void cancelActiveRun()}
                  >
                    <Square size={14} />
                    {cancelling ? "正在停止" : "停止"}
                  </button>
                ) : (
                  <button
                    type="button"
                    className="primary send-button"
                    disabled={
                      !connected || !selected || sending || !draft.trim()
                    }
                    onClick={() => void sendMessage()}
                  >
                    <Send size={14} />
                    {sending ? "发送中" : "发送"}
                  </button>
                )}
              </div>
            </footer>
          ) : (
            <footer className="record-footer">
              <Terminal size={15} />
              <span>在终端提问与查看生成过程</span>
              <span className="shared-note">同一会话共享历史</span>
            </footer>
          )}
        </section>
      </div>
      <dialog
        ref={dialog}
        onCancel={(event) => {
          if (busy) event.preventDefault();
          else setEditor({ kind: "closed" });
        }}
        onClose={() => {
          if (!busy) setEditor({ kind: "closed" });
        }}
      >
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (!snapshot || editor.kind === "closed") return;
            void command({
              action: "rename",
              session_id: editor.session,
              topic_id: editor.topic,
              title,
            });
          }}
        >
          <h2>重命名话题</h2>
          <p>只改变话题名称，保留消息与归属。</p>
          <label>
            话题名称
            <input
              autoFocus
              required
              maxLength={120}
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              placeholder="这个想法叫什么？"
            />
          </label>
          {error && (
            <p className="form-error" role="alert">
              {error}
            </p>
          )}
          <div className="dialog-actions">
            <button
              type="button"
              disabled={busy}
              onClick={() => setEditor({ kind: "closed" })}
            >
              取消
            </button>
            <button
              type="submit"
              className="primary"
              disabled={busy || !connected || !title.trim()}
            >
              {busy ? "正在保存…" : "保存"}
            </button>
          </div>
        </form>
      </dialog>
    </main>
  );
}
