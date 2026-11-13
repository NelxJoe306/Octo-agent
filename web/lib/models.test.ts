import test from "node:test";
import assert from "node:assert/strict";
import { parseModelConfig, resolveSelection } from "./models";

test("本地配置保留真实名称，推理选项仅来自该模型声明", () => {
  const config = parseModelConfig(
    JSON.stringify({
      version: 1,
      defaultModelId: "plain",
      models: [
        { id: "plain", name: "我的对话模型", provider: "本地" },
        {
          id: "reasoner",
          name: "我的推理模型",
          provider: "自定义",
          reasoning: {
            options: [
              { value: "low", label: "低" },
              { value: "high", label: "高" },
            ],
            default: "high",
          },
        },
      ],
    }),
  );
  assert.equal(config.models[0]?.name, "我的对话模型");
  assert.deepEqual(
    resolveSelection(config, { modelId: "reasoner", effort: null }),
    { modelId: "reasoner", effort: "high" },
  );
  assert.deepEqual(
    resolveSelection(config, { modelId: "plain", effort: "high" }),
    { modelId: "plain", effort: null },
  );
  assert.deepEqual(
    resolveSelection(config, { modelId: "removed", effort: "high" }),
    { modelId: "plain", effort: null },
  );
  assert.throws(() =>
    parseModelConfig(
      '{"version":1,"models":[{"id":"a","name":"A","provider":"本地"},{"id":"a","name":"B","provider":"本地"}]}',
    ),
  );
  assert.throws(() =>
    parseModelConfig(
      '{"version":1,"models":[{"id":"a","name":"A","provider":"本地","reasoning":{"options":[{"value":"low","label":"低"}],"default":"high"}}]}',
    ),
  );
});
