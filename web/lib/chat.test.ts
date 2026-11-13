import assert from "node:assert/strict";
import test from "node:test";
import { chatReducer, createPreviewState, RequestId, SessionId } from "./chat";

test("切换会话保留草稿，生成结果仍归属发送时的会话", () => {
  let state = createPreviewState();
  const [first, second] = state.sessions;
  assert.ok(first && second);
  state = chatReducer(state, { kind: "select", id: first.id });
  state = chatReducer(state, { kind: "draft", text: "检查状态归属" });
  const requestId = RequestId.parse("request-test");
  state = chatReducer(state, {
    kind: "send",
    requestId,
    newSessionId: SessionId.parse("unused"),
    reply: "演示结果",
  });
  state = chatReducer(state, { kind: "select", id: second.id });
  state = chatReducer(state, { kind: "draft", text: "另一份草稿" });
  for (let index = 0; index < 10; index += 1) {
    state = chatReducer(state, { kind: "tick", requestId });
  }
  assert.equal(state.activeId, second.id);
  assert.equal(state.drafts[second.id], "另一份草稿");
  assert.equal(
    state.sessions.find((item) => item.id === first.id)?.messages.at(-1)?.text,
    "演示结果",
  );
  assert.deepEqual(
    state.sessions.find((item) => item.id === second.id)?.messages,
    second.messages,
  );
  assert.equal(state.run.kind, "idle");
});

test("空白消息不创建会话，停止后迟到的流式事件不会继续追加", () => {
  let state = createPreviewState();
  const requestId = RequestId.parse("stopped-request");
  const send = {
    kind: "send",
    requestId,
    newSessionId: SessionId.parse("new-test"),
    reply: "还未生成的回复",
  } satisfies Parameters<typeof chatReducer>[1];
  const count = state.sessions.length;
  state = chatReducer(state, { kind: "draft", text: "  " });
  state = chatReducer(state, send);
  assert.equal(state.sessions.length, count);
  state = chatReducer(state, { kind: "draft", text: "新问题" });
  state = chatReducer(state, send);
  state = chatReducer(state, { kind: "stop" });
  const stopped = state.sessions[0]?.messages;
  state = chatReducer(state, { kind: "tick", requestId });
  assert.deepEqual(state.sessions[0]?.messages, stopped);
  assert.equal(state.run.kind, "idle");
});
