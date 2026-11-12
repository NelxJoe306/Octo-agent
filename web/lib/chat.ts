import { z } from "zod";

export const SessionId = z.string().min(1).brand<"SessionId">();
export type SessionId = z.infer<typeof SessionId>;
export const RequestId = z.string().min(1).brand<"RequestId">();
export type RequestId = z.infer<typeof RequestId>;
export type Message = {
  id: string;
  role: "user" | "assistant";
  text: string;
  status: "complete" | "streaming" | "stopped";
};
export type Session = {
  id: SessionId;
  title: string;
  pinned: boolean;
  messages: Message[];
};
export type ChatState = {
  sessions: Session[];
  activeId: SessionId | null;
  drafts: Partial<Record<string, string>>;
  run:
    | { kind: "idle" }
    | {
        kind: "running";
        requestId: RequestId;
        sessionId: SessionId;
        messageId: string;
        reply: string;
        cursor: number;
      };
};
type Action =
  | { kind: "select"; id: SessionId }
  | { kind: "new" }
  | { kind: "draft"; text: string }
  | { kind: "pin"; id: SessionId }
  | {
      kind: "send";
      requestId: RequestId;
      newSessionId: SessionId;
      reply: string;
    }
  | { kind: "tick"; requestId: RequestId }
  | { kind: "stop" };

export function createPreviewState(): ChatState {
  const examples = [
    [
      "Octo 项目设计",
      "这个项目的会话界面准备怎么做？",
      "先把日常使用的路径做顺：左侧找到会话，中间继续对话，底部保留项目和模式入口。\n\n右侧预留独立窗口，后续再接入对话树。当前版本只验证页面与交互。",
    ],
    [
      "上下文与记忆",
      "把上下文和长期记忆的职责理一下。",
      "上下文负责本次请求需要读到什么；长期记忆负责保存经过提取的稳定事实。\n\n话题标记用于说明当前问题的归属，压缩策略单独控制输入规模。",
    ],
    [
      "用 SQLite 保存会话",
      "存储这一层用什么作为权威？",
      "会话以 SQLite 为唯一权威。JSONL 用于旧历史导入和导出。\n\n这是一条界面示例消息，页面尚未读取本地数据库。",
    ],
    [
      "为 Octo 设计桌面端",
      "桌面端的页面参考怎么分工？",
      "会话主界面参考 Codex 的布局；设置导航、卡片与控件反馈参考 agentes 的样式。",
    ],
    [
      "梳理部署步骤",
      "部署前需要检查哪些内容？",
      "先确认运行环境、配置和存储目录，再验证服务启动与连接。\n\n当前页面仅提供交互预览，未执行系统命令。",
    ],
  ];
  return {
    sessions: examples.map(
      ([title = "", question = "", answer = ""], index) => ({
        id: SessionId.parse(`preview-${index}`),
        title,
        pinned: index < 2,
        messages: [
          {
            id: `user-${index}`,
            role: "user",
            text: question,
            status: "complete",
          },
          {
            id: `assistant-${index}`,
            role: "assistant",
            text: answer,
            status: "complete",
          },
        ],
      }),
    ),
    activeId: null,
    drafts: {},
    run: { kind: "idle" },
  };
}

export function chatReducer(state: ChatState, action: Action): ChatState {
  switch (action.kind) {
    case "select":
      return state.sessions.some((item) => item.id === action.id)
        ? { ...state, activeId: action.id }
        : state;
    case "new":
      return { ...state, activeId: null };
    case "draft":
      return {
        ...state,
        drafts: { ...state.drafts, [state.activeId ?? "new"]: action.text },
      };
    case "pin":
      return {
        ...state,
        sessions: state.sessions.map((item) =>
          item.id === action.id ? { ...item, pinned: !item.pinned } : item,
        ),
      };
    case "send": {
      const key = state.activeId ?? "new";
      const text = state.drafts[key]?.trim();
      if (!text || state.run.kind === "running") return state;
      const id = state.activeId ?? action.newSessionId;
      const messageId = `${action.requestId}-assistant`;
      const messages: Message[] = [
        {
          id: `${action.requestId}-user`,
          role: "user",
          text,
          status: "complete",
        },
        { id: messageId, role: "assistant", text: "", status: "streaming" },
      ];
      const sessions = state.activeId
        ? state.sessions.map((item) =>
            item.id === id
              ? { ...item, messages: [...item.messages, ...messages] }
              : item,
          )
        : [
            { id, title: text.slice(0, 24), pinned: false, messages },
            ...state.sessions,
          ];
      // 请求归属在发送时固定，界面切换不改变流式输出的目的地。
      return {
        ...state,
        activeId: id,
        sessions,
        drafts: { ...state.drafts, [key]: "" },
        run: {
          kind: "running",
          requestId: action.requestId,
          sessionId: id,
          messageId,
          reply: action.reply,
          cursor: 0,
        },
      };
    }
    case "tick": {
      const run = state.run;
      if (run.kind !== "running" || run.requestId !== action.requestId)
        return state;
      const cursor = Math.min(run.reply.length, run.cursor + 4);
      const complete = cursor === run.reply.length;
      return {
        ...state,
        sessions: state.sessions.map((item) =>
          item.id === run.sessionId
            ? {
                ...item,
                messages: item.messages.map((message) =>
                  message.id === run.messageId
                    ? {
                        ...message,
                        text: run.reply.slice(0, cursor),
                        status: complete ? "complete" : "streaming",
                      }
                    : message,
                ),
              }
            : item,
        ),
        run: complete ? { kind: "idle" } : { ...run, cursor },
      };
    }
    case "stop": {
      const run = state.run;
      if (run.kind === "idle") return state;
      return {
        ...state,
        run: { kind: "idle" },
        sessions: state.sessions.map((item) =>
          item.id === run.sessionId
            ? {
                ...item,
                messages: item.messages.map((message) =>
                  message.id === run.messageId
                    ? { ...message, status: "stopped" }
                    : message,
                ),
              }
            : item,
        ),
      };
    }
  }
}
