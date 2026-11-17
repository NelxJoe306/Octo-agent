import assert from "node:assert/strict";
import test from "node:test";
import {
  snapshotSchema,
  visibleTopics,
  layoutRounds,
  turnSchema,
  openTopicTab,
  stateSchema,
  sessionListSchema,
  runSnapshotSchema,
  runEventEnvelopeSchema,
  createRunStreamState,
  applyRunEvent,
} from "../lib/topic-map";

const fixture = {
  session_id: "session",
  title: "session",
  selected_topic_id: "main",
  viewed_topic_id: "main",
  active_topic_id: null,
  running: false,
  messages: [],
  has_more: false,
  before: null,
  turns: [],
  topics: [
    {
      id: "main",
      kind: "trunk",
      branch_label: "主干",
      parent_label: null,
      latest_entry_id: null,
      parent_id: null,
      anchor_entry_id: null,
      title: "主线",
      message_count: 2,
    },
    {
      id: "a",
      kind: "branch",
      branch_label: "分支 1",
      parent_label: "主干",
      latest_entry_id: null,
      parent_id: "main",
      anchor_entry_id: null,
      title: "上下文",
      message_count: 4,
    },
    {
      id: "b",
      kind: "branch",
      branch_label: "分支 1.1",
      parent_label: "分支 1",
      latest_entry_id: null,
      parent_id: "a",
      anchor_entry_id: null,
      title: "压缩",
      message_count: 2,
    },
    {
      id: "c",
      kind: "branch",
      branch_label: "分支 2",
      parent_label: "主干",
      latest_entry_id: null,
      parent_id: "main",
      anchor_entry_id: null,
      title: "工具",
      message_count: 0,
    },
  ],
};

test("折叠只改变导航，搜索仍能定位子话题及其父路径", () => {
  const snapshot = snapshotSchema.parse(fixture);
  const main = snapshot.topics[0];
  assert.ok(main);
  const collapsed = new Set([main.id]);
  assert.deepEqual(
    visibleTopics(snapshot.topics, collapsed, "").map((row) => row.topic.title),
    ["主线"],
  );
  assert.deepEqual(
    visibleTopics(snapshot.topics, collapsed, "压缩").map(
      (row) => row.topic.title,
    ),
    ["主线", "上下文", "压缩"],
  );
  assert.equal(snapshot.selected_topic_id, "main");
});

test("拒绝重复身份、悬空父节点和循环话题", () => {
  assert.equal(
    snapshotSchema.safeParse({
      ...fixture,
      topics: [...fixture.topics, fixture.topics[0]],
    }).success,
    false,
  );
  assert.equal(
    snapshotSchema.safeParse({
      ...fixture,
      topics: fixture.topics.map((topic) =>
        topic.id === "a" ? { ...topic, parent_id: "absent" } : topic,
      ),
    }).success,
    false,
  );
  assert.equal(
    snapshotSchema.safeParse({
      ...fixture,
      topics: fixture.topics.map((topic) =>
        topic.id === "a" ? { ...topic, parent_id: "b" } : topic,
      ),
    }).success,
    false,
  );
});

test("轮次树自上而下展开，分支连接起点轮次而非主干最新轮次", () => {
  const topics = snapshotSchema.parse(fixture).topics;
  const turns = [
    {
      id: "r1",
      topic_id: "main",
      first_entry_id: "u1",
      last_entry_id: "a1",
      first_seq: 1,
      title: "第一问",
      message_count: 2,
      parent_turn_id: null,
      number: 1,
      entry_ids: ["u1", "a1"],
    },
    {
      id: "r2",
      topic_id: "main",
      first_entry_id: "u2",
      last_entry_id: "a2",
      first_seq: 3,
      title: "第二问",
      message_count: 2,
      parent_turn_id: "r1",
      number: 2,
      entry_ids: ["u2", "a2"],
    },
    {
      id: "r3",
      topic_id: "a",
      first_entry_id: "u3",
      last_entry_id: "a3",
      first_seq: 5,
      title: "分支问",
      message_count: 4,
      parent_turn_id: "r1",
      number: 1,
      entry_ids: ["u3", "a3"],
    },
  ].map((turn) => turnSchema.parse(turn));
  const tree = layoutRounds(topics, turns, new Set(), "");
  const child = tree.nodes.find((node) => node.id === "r3");
  assert.equal(child?.parent_id, "r1");
  assert.equal(tree.nodes.filter((node) => node.kind === "turn").length, 3);
  for (const node of tree.nodes) {
    const parent = tree.nodes.find((item) => item.id === node.parent_id);
    if (parent) assert.ok(node.y > parent.y);
  }
});

test("进入分支追加标签，重复进入复用原标签", () => {
  const topics = snapshotSchema.parse(fixture).topics;
  const first = topics[0],
    second = topics[1];
  assert.ok(first && second);
  const tabs = openTopicTab(openTopicTab([], first.id), second.id);
  assert.deepEqual(openTopicTab(tabs, first.id), [first.id, second.id]);
});

test("服务状态、会话列表校验与断连重连快照替换不重复拼接正文", () => {
  const state = stateSchema.parse({
    mode: "service",
    session_id: "sess-1",
    session_title: "测试会话",
    current_topic_id: "main",
    active_run_id: "run-1",
    active_run_topic_id: "main",
    active_run_status: "running",
    running: true,
    model: "deepseek-flash",
    provider_name: "deepseek",
    model_display_name: "DeepSeek Flash",
    notice: null,
    capabilities: {
      chat: true,
      sessions: true,
      topics: true,
      streaming: true,
      cancel: true,
    },
  });
  assert.equal(state.active_run_id, "run-1");

  const sessions = sessionListSchema.parse({
    active_session_id: "sess-1",
    sessions: [
      {
        id: "sess-1",
        title: "测试会话",
        model: "deepseek-flash",
        provider_name: "deepseek",
        created_at: 1000,
        updated_at: 1001,
        is_active: true,
      },
    ],
  });
  assert.equal(sessions.sessions.length, 1);

  const snap1 = runSnapshotSchema.parse({
    run_id: "run-1",
    client_request_id: "req-1",
    session_id: "sess-1",
    topic_id: "main",
    status: "running",
    seq: 3,
    user_text: "hi",
    assistant_text: "Step 1\n\nHel",
    thinking_text: "",
    committed_assistant_text: "Step 1",
    committed_thinking_text: "",
    tool_executions: [],
    error_code: null,
    error_message: null,
    created_at: 1000,
    updated_at: 1001,
    finished_at: null,
  });
  let stream: ReturnType<typeof createRunStreamState> | null =
    createRunStreamState(snap1);

  // 旧序号事件必须被忽略。
  stream = applyRunEvent(
    stream,
    runEventEnvelopeSchema.parse({
      run_id: "run-1",
      session_id: "sess-1",
      topic_id: "main",
      seq: 2,
      kind: "agent_event",
      payload: {
        type: "message_update",
        message: { role: "assistant", content: [{ type: "text", text: "old" }] },
      },
    }),
  );
  assert.equal(stream?.snapshot.assistant_text, "Step 1\n\nHel");

  // 正常增量只替换当前轮，不破坏已完成轮。
  stream = applyRunEvent(
    stream,
    runEventEnvelopeSchema.parse({
      run_id: "run-1",
      session_id: "sess-1",
      topic_id: "main",
      seq: 4,
      kind: "agent_event",
      payload: {
        type: "message_update",
        message: { role: "assistant", content: [{ type: "text", text: "Hello" }] },
      },
    }),
  );
  assert.equal(stream?.snapshot.assistant_text, "Step 1\n\nHello");

  // 重连 snapshot 直接替换整个运行快照，不能累加到原有正文末尾。
  stream = applyRunEvent(
    stream,
    runEventEnvelopeSchema.parse({
      run_id: "run-1",
      session_id: "sess-1",
      topic_id: "main",
      seq: 6,
      kind: "snapshot",
      payload: {
        ...snap1,
        seq: 6,
        assistant_text: "Step 1\n\nHello world",
        status: "completed",
        finished_at: 1002,
      },
    }),
  );
  assert.equal(stream?.snapshot.assistant_text, "Step 1\n\nHello world");
  assert.equal(stream?.snapshot.status, "completed");
});
