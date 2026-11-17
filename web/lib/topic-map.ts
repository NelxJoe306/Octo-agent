import { z } from "zod";

export const topicId = z.string().min(1).max(128).brand<"TopicId">();
const sessionId = z.string().min(1).brand<"SessionId">();
const entryId = z.string().min(1).brand<"EntryId">();
const topicSchema = z.object({
  id: topicId,
  parent_id: topicId.nullable(),
  anchor_entry_id: entryId.nullable(),
  title: z.string(),
  kind: z.enum(["trunk", "branch"]),
  branch_label: z.string(),
  parent_label: z.string().nullable(),
  latest_entry_id: entryId.nullable(),
  message_count: z.number().int().nonnegative(),
});
export const turnSchema = z.object({
  id: z.string(),
  topic_id: topicId,
  first_entry_id: entryId,
  last_entry_id: entryId,
  first_seq: z.number().int(),
  title: z.string(),
  message_count: z.number().int(),
  parent_turn_id: z.string().nullable(),
  entry_ids: z.array(entryId),
  number: z.number().int().positive(),
});
export type Turn = z.infer<typeof turnSchema>;
export const snapshotSchema = z
  .object({
    session_id: sessionId,
    title: z.string(),
    topics: z.array(topicSchema).min(1),
    turns: z.array(turnSchema),
    selected_topic_id: topicId,
    viewed_topic_id: topicId,
    active_topic_id: topicId.nullable(),
    running: z.boolean(),
    messages: z.array(
      z.object({
        id: entryId,
        seq: z.number().int(),
        role: z.string(),
        text: z.string(),
        timestamp: z.number(),
        request_id: z.string().nullable(),
      }),
    ),
    has_more: z.boolean(),
    before: z.number().nullable(),
    after: z.number().int().nonnegative().nullable().default(null),
    has_newer: z.boolean().default(false),
  })
  .superRefine((snapshot, context) => {
    const ids = new Set(snapshot.topics.map((topic) => topic.id));
    if (
      ids.size !== snapshot.topics.length ||
      !ids.has(snapshot.selected_topic_id) ||
      !ids.has(snapshot.viewed_topic_id)
    ) {
      context.addIssue({ code: "custom", message: "Invalid topic identities" });
    }
    const parents = new Map(
      snapshot.topics.map((topic) => [topic.id, topic.parent_id]),
    );
    for (const topic of snapshot.topics) {
      const visited = new Set<TopicId>();
      let current: TopicId | null = topic.id;
      while (current !== null) {
        if (!ids.has(current) || visited.has(current)) {
          context.addIssue({
            code: "custom",
            message: "Invalid topic hierarchy",
          });
          break;
        }
        visited.add(current);
        current = parents.get(current) ?? null;
      }
    }
  });
export type TopicId = z.infer<typeof topicId>;
export type Snapshot = z.infer<typeof snapshotSchema>;
export type Topic = Snapshot["topics"][number];
export type Message = Snapshot["messages"][number];
export type TopicCommand = {
  session_id: Snapshot["session_id"];
  topic_id: TopicId;
} & (
  | {
      action: "create";
      title: string;
      anchor_entry_id: Message["id"] | null;
      activate: boolean;
    }
  | { action: "rename"; title: string }
  | { action: "select" }
);

export function visibleTopics(
  topics: Topic[],
  collapsed: Set<TopicId>,
  query: string,
) {
  const byParent = new Map<TopicId | null, Topic[]>();
  for (const topic of topics) {
    const siblings = byParent.get(topic.parent_id) ?? [];
    siblings.push(topic);
    byParent.set(topic.parent_id, siblings);
  }
  const included = new Set<TopicId>();
  const byId = new Map(topics.map((topic) => [topic.id, topic]));
  for (const topic of topics) {
    if (
      !topic.title
        .toLocaleLowerCase()
        .includes(query.trim().toLocaleLowerCase())
    )
      continue;
    let current: Topic | undefined = topic;
    while (current && !included.has(current.id)) {
      included.add(current.id);
      current =
        current.parent_id === null ? undefined : byId.get(current.parent_id);
    }
  }
  const rows: { topic: Topic; depth: number; hasChildren: boolean }[] = [];
  const stack = [...(byParent.get(null) ?? [])]
    .reverse()
    .map((topic) => ({ topic, depth: 0 }));
  while (stack.length) {
    const row = stack.pop();
    if (!row || !included.has(row.topic.id)) continue;
    const children = byParent.get(row.topic.id) ?? [];
    rows.push({ ...row, hasChildren: children.length > 0 });
    if (!query && collapsed.has(row.topic.id)) continue;
    for (const topic of [...children].reverse())
      stack.push({ topic, depth: row.depth + 1 });
  }
  return rows;
}

export function layoutRounds(
  topics: Topic[],
  turns: Turn[],
  collapsed: Set<string>,
  query: string,
) {
  type Node = {
    id: string;
    parent_id: string | null;
    topic: Topic;
    title: string;
    entry_id: Message["id"] | null;
    number: number | null;
    message_count: number;
    isLatest: boolean;
    kind: "root" | "turn" | "empty";
  };
  const main = topics.find((topic) => topic.kind === "trunk");
  if (!main)
    return { nodes: [], edges: [], width: 280, height: 180, levels: 0 };
  const byTopic = new Map(topics.map((topic) => [topic.id, topic]));
  const entryTurn = new Map(
    turns.flatMap((turn) => turn.entry_ids.map((entry) => [entry, turn.id])),
  );
  const latest = new Map<TopicId, string>();
  for (const turn of turns) latest.set(turn.topic_id, turn.id);
  const source: Node[] = [
    {
      id: "session-root",
      parent_id: null,
      topic: main,
      title: "会话主干",
      entry_id: null,
      number: null,
      message_count: 0,
      isLatest: false,
      kind: "root",
    },
  ];
  for (const turn of turns) {
    const topic = byTopic.get(turn.topic_id);
    if (!topic) continue;
    source.push({
      id: turn.id,
      parent_id: turn.parent_turn_id ?? "session-root",
      topic,
      title: turn.title,
      entry_id: turn.last_entry_id,
      number: turn.number,
      message_count: turn.message_count,
      isLatest: turn.last_entry_id === topic.latest_entry_id,
      kind: "turn",
    });
  }
  for (const topic of topics) {
    if (topic.kind === "trunk" || latest.has(topic.id)) continue;
    const parent = topic.anchor_entry_id
      ? entryTurn.get(topic.anchor_entry_id)
      : undefined;
    source.push({
      id: "empty-" + topic.id,
      parent_id:
        parent ??
        (topic.parent_id
          ? (latest.get(topic.parent_id) ??
            (byTopic.get(topic.parent_id)?.kind === "trunk"
              ? "session-root"
              : "empty-" + topic.parent_id))
          : "session-root"),
      topic,
      title: topic.title,
      entry_id: null,
      number: null,
      message_count: 0,
      isLatest: true,
      kind: "empty",
    });
  }
  const byId = new Map(source.map((node) => [node.id, node]));
  const included = new Set<string>();
  for (const node of source) {
    if (
      !(node.title + node.topic.title + node.topic.branch_label)
        .toLocaleLowerCase()
        .includes(query.trim().toLocaleLowerCase())
    )
      continue;
    let current: Node | undefined = node;
    while (current && !included.has(current.id)) {
      included.add(current.id);
      current = current.parent_id ? byId.get(current.parent_id) : undefined;
    }
  }
  const children = new Map<string, Node[]>();
  for (const node of source) {
    if (!node.parent_id) continue;
    const siblings = children.get(node.parent_id) ?? [];
    siblings.push(node);
    children.set(node.parent_id, siblings);
  }
  const ordered: (Node & { depth: number; hasChildren: boolean })[] = [];
  const stack = [{ node: source[0], depth: 0 }];
  const visited = new Set<string>();
  while (stack.length) {
    const item = stack.pop();
    if (!item?.node || !included.has(item.node.id) || visited.has(item.node.id))
      continue;
    visited.add(item.node.id);
    const descendants = children.get(item.node.id) ?? [];
    ordered.push({
      ...item.node,
      depth: item.depth,
      hasChildren: descendants.length > 0,
    });
    if (!query && collapsed.has(item.node.id)) continue;
    for (const node of [...descendants].reverse())
      stack.push({ node, depth: item.depth + 1 });
  }
  const positions = new Map<string, number>();
  let leaf = 0;
  for (const node of [...ordered].reverse()) {
    const xs = (children.get(node.id) ?? [])
      .map((child) => positions.get(child.id))
      .filter((x) => x !== undefined);
    const first = xs[0],
      last = xs[xs.length - 1];
    positions.set(
      node.id,
      first !== undefined && last !== undefined
        ? (first + last) / 2
        : 28 + leaf++ * 232,
    );
  }
  const maxX = Math.max(28, ...positions.values());
  const nodes = ordered.map((node) => ({
    ...node,
    x: maxX - (positions.get(node.id) ?? 28) + 28,
    y: 22 + node.depth * 144,
  }));
  const positioned = new Map(nodes.map((node) => [node.id, node]));
  const edges = nodes.flatMap((node) => {
    const parent = node.parent_id ? positioned.get(node.parent_id) : undefined;
    if (!parent) return [];
    const x = parent.x + 95,
      y = parent.y + 92;
    return [
      {
        id: node.id,
        trunk: parent.topic.id === node.topic.id,
        path: `M ${x} ${y} V ${y + 26} H ${node.x + 95} V ${node.y}`,
      },
    ];
  });
  return {
    nodes,
    edges,
    width: maxX + 244,
    height: Math.max(180, ...nodes.map((node) => node.y + 130)),
    levels: Math.max(0, ...nodes.map((node) => node.depth)) + 1,
  };
}

export function openTopicTab(tabs: TopicId[], id: TopicId): TopicId[] {
  return tabs.includes(id) ? tabs : [...tabs, id];
}

export const runStatusSchema = z.enum([
  "starting",
  "running",
  "cancelling",
  "completed",
  "cancelled",
  "failed",
]);
export type RunStatus = z.infer<typeof runStatusSchema>;

export const healthSchema = z.object({
  ok: z.boolean(),
  mode: z.literal("service"),
  session_id: z.string().nullable(),
  busy: z.boolean(),
});

export const stateSchema = z.object({
  mode: z.literal("service"),
  session_id: sessionId,
  session_title: z.string(),
  current_topic_id: topicId,
  active_run_id: z.string().nullable(),
  active_run_topic_id: topicId.nullable(),
  active_run_status: runStatusSchema.nullable(),
  running: z.boolean(),
  model: z.string(),
  provider_name: z.string(),
  model_display_name: z.string(),
  notice: z.string().nullable(),
  capabilities: z.object({
    chat: z.boolean(),
    sessions: z.boolean(),
    topics: z.boolean(),
    streaming: z.boolean(),
    cancel: z.boolean(),
  }),
});
export type ServiceState = z.infer<typeof stateSchema>;

export const sessionSummarySchema = z.object({
  id: sessionId,
  title: z.string(),
  model: z.string(),
  provider_name: z.string().nullable(),
  created_at: z.number(),
  updated_at: z.number(),
  is_active: z.boolean(),
});
export const sessionListSchema = z.object({
  active_session_id: sessionId,
  sessions: z.array(sessionSummarySchema),
});
export type SessionSummary = z.infer<typeof sessionSummarySchema>;

export const sessionActionSchema = z.object({
  state: stateSchema,
  notice: z.string().nullable(),
});

export const toolExecutionSchema = z.object({
  tool_call_id: z.string(),
  tool_name: z.string(),
  status: z.enum(["running", "completed", "error"]),
  args: z.record(z.string(), z.unknown()).default({}),
  partial_text: z.string().default(""),
  result_text: z.string().default(""),
  is_error: z.boolean().default(false),
});
export type ToolExecution = z.infer<typeof toolExecutionSchema>;

export const runSnapshotSchema = z.object({
  run_id: z.string(),
  client_request_id: z.string(),
  session_id: sessionId,
  topic_id: topicId,
  status: runStatusSchema,
  seq: z.number().int().nonnegative(),
  user_text: z.string(),
  assistant_text: z.string(),
  thinking_text: z.string(),
  committed_assistant_text: z.string().default(""),
  committed_thinking_text: z.string().default(""),
  tool_executions: z.array(toolExecutionSchema),
  error_code: z.string().nullable(),
  error_message: z.string().nullable(),
  budget: z.record(z.string(), z.unknown()).nullable().optional(),
  recovery: z.record(z.string(), z.unknown()).nullable().optional(),
  created_at: z.number(),
  updated_at: z.number(),
  finished_at: z.number().nullable(),
});
export type RunSnapshot = z.infer<typeof runSnapshotSchema>;

export const runAcceptedSchema = z.object({
  run_id: z.string(),
  client_request_id: z.string(),
  session_id: sessionId,
  topic_id: topicId,
  status: runStatusSchema,
});

export const runEventEnvelopeSchema = z.object({
  run_id: z.string(),
  session_id: sessionId,
  topic_id: topicId,
  seq: z.number().int().nonnegative(),
  kind: z.enum(["snapshot", "agent_event", "run_status"]),
  payload: z.record(z.string(), z.unknown()),
});
export type RunEventEnvelope = z.infer<typeof runEventEnvelopeSchema>;

export type RunStreamState = {
  snapshot: RunSnapshot;
  baseAssistantText: string;
  baseThinkingText: string;
};

export function isTerminalRunStatus(status: RunStatus): boolean {
  return status === "completed" || status === "cancelled" || status === "failed";
}

function joinParts(prefix: string, current: string): string {
  if (prefix && current) return `${prefix}\n\n${current}`;
  return prefix || current;
}

function extractMessageParts(message: unknown): {
  role: string | null;
  text: string;
  thinking: string;
} {
  if (!message || typeof message !== "object") {
    return { role: null, text: "", thinking: "" };
  }
  const record = message as Record<string, unknown>;
  const role = typeof record.role === "string" ? record.role : null;
  const content = record.content;
  if (typeof content === "string") {
    return { role, text: content, thinking: "" };
  }
  if (!Array.isArray(content)) {
    return { role, text: "", thinking: "" };
  }
  const texts: string[] = [];
  const thinkings: string[] = [];
  for (const item of content) {
    if (!item || typeof item !== "object") continue;
    const block = item as Record<string, unknown>;
    if (block.type === "text" && typeof block.text === "string") {
      texts.push(block.text);
    } else if (block.type === "thinking" && typeof block.thinking === "string") {
      thinkings.push(block.thinking);
    }
  }
  return { role, text: texts.join(""), thinking: thinkings.join("") };
}

function extractResultText(result: unknown): string {
  if (!result || typeof result !== "object") return "";
  const content = (result as Record<string, unknown>).content;
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .map((item) => {
      if (!item || typeof item !== "object") return "";
      const block = item as Record<string, unknown>;
      return block.type === "text" && typeof block.text === "string"
        ? block.text
        : "";
    })
    .join("\n");
}

export function createRunStreamState(snapshot: RunSnapshot): RunStreamState {
  return {
    snapshot,
    baseAssistantText: snapshot.committed_assistant_text,
    baseThinkingText: snapshot.committed_thinking_text,
  };
}

export function applyRunEvent(
  current: RunStreamState | null,
  envelope: RunEventEnvelope,
): RunStreamState | null {
  if (envelope.kind === "snapshot") {
    return createRunStreamState(runSnapshotSchema.parse(envelope.payload));
  }
  if (!current || current.snapshot.run_id !== envelope.run_id) {
    return current;
  }
  if (envelope.seq <= current.snapshot.seq) {
    return current;
  }
  if (envelope.kind === "run_status") {
    const status = runStatusSchema.parse(envelope.payload.status);
    return {
      ...current,
      snapshot: {
        ...current.snapshot,
        seq: envelope.seq,
        status,
        error_code:
          typeof envelope.payload.errorCode === "string"
            ? envelope.payload.errorCode
            : null,
        error_message:
          typeof envelope.payload.errorMessage === "string"
            ? envelope.payload.errorMessage
            : null,
        finished_at:
          typeof envelope.payload.finishedAt === "number"
            ? envelope.payload.finishedAt
            : current.snapshot.finished_at,
      },
    };
  }
  const eventType = envelope.payload.type;
  let nextSnapshot: RunSnapshot = {
    ...current.snapshot,
    seq: envelope.seq,
  };
  let nextBaseAssistant = current.baseAssistantText;
  let nextBaseThinking = current.baseThinkingText;

  if (
    eventType === "message_start" ||
    eventType === "message_update" ||
    eventType === "message_end"
  ) {
    const parts = extractMessageParts(envelope.payload.message);
    if (parts.role === "assistant") {
      if (eventType === "message_start") {
        nextBaseAssistant = current.snapshot.assistant_text;
        nextBaseThinking = current.snapshot.thinking_text;
      }
      const combinedText = joinParts(nextBaseAssistant, parts.text);
      const combinedThinking = joinParts(nextBaseThinking, parts.thinking);
      nextSnapshot = {
        ...nextSnapshot,
        assistant_text: combinedText,
        thinking_text: combinedThinking,
        committed_assistant_text:
          eventType === "message_end"
            ? combinedText
            : current.snapshot.committed_assistant_text,
        committed_thinking_text:
          eventType === "message_end"
            ? combinedThinking
            : current.snapshot.committed_thinking_text,
      };
      if (eventType === "message_end") {
        nextBaseAssistant = combinedText;
        nextBaseThinking = combinedThinking;
      }
    }
  } else if (
    eventType === "tool_execution_start" ||
    eventType === "tool_execution_update" ||
    eventType === "tool_execution_end"
  ) {
    const callId =
      typeof envelope.payload.toolCallId === "string"
        ? envelope.payload.toolCallId
        : "";
    const toolName =
      typeof envelope.payload.toolName === "string"
        ? envelope.payload.toolName
        : "";
    if (callId) {
      const existing = [...nextSnapshot.tool_executions];
      const idx = existing.findIndex((item) => item.tool_call_id === callId);
      const rawArgs =
        envelope.payload.args &&
        typeof envelope.payload.args === "object" &&
        !Array.isArray(envelope.payload.args)
          ? (envelope.payload.args as Record<string, unknown>)
          : {};
      if (eventType === "tool_execution_start") {
        const entry: ToolExecution = {
          tool_call_id: callId,
          tool_name: toolName,
          status: "running",
          args: rawArgs,
          partial_text: "",
          result_text: "",
          is_error: false,
        };
        if (idx >= 0) existing[idx] = entry;
        else existing.push(entry);
      } else if (eventType === "tool_execution_update" && idx >= 0) {
        const currentTool = existing[idx];
        if (currentTool) {
          existing[idx] = {
            ...currentTool,
            partial_text: extractResultText(envelope.payload.partialResult),
          };
        }
      } else if (eventType === "tool_execution_end") {
        const isError = Boolean(envelope.payload.isError);
        const currentTool = idx >= 0 ? existing[idx] : undefined;
        const entry: ToolExecution = {
          tool_call_id: callId,
          tool_name: toolName || currentTool?.tool_name || "",
          status: isError ? "error" : "completed",
          args: currentTool?.args ?? rawArgs,
          partial_text: "",
          result_text: extractResultText(envelope.payload.result),
          is_error: isError,
        };
        if (idx >= 0) existing[idx] = entry;
        else existing.push(entry);
      }
      nextSnapshot = { ...nextSnapshot, tool_executions: existing };
    }
  }
  return {
    snapshot: nextSnapshot,
    baseAssistantText: nextBaseAssistant,
    baseThinkingText: nextBaseThinking,
  };
}
