import { z } from "zod";

const text = z.string().trim().min(1).max(160);
const reasoning = z
  .object({
    options: z
      .array(z.object({ value: text, label: text }).strict())
      .min(1)
      .max(8),
    default: text,
  })
  .strict()
  .superRefine((value, context) => {
    const values = value.options.map((option) => option.value);
    if (
      new Set(values).size !== values.length ||
      !values.includes(value.default)
    )
      context.addIssue({
        code: "custom",
        message: "推理强度值不能重复，默认值必须位于选项中",
      });
  });
const model = z
  .object({
    id: text,
    name: text,
    provider: text,
    baseUrl: z
      .url()
      .refine((value) => /^https?:\/\//.test(value), "地址必须为 HTTP 或 HTTPS")
      .optional(),
    reasoning: reasoning.optional(),
  })
  .strict();
const schema = z
  .object({
    version: z.literal(1),
    defaultModelId: text.nullable().optional(),
    models: z.array(model).max(100),
  })
  .strict()
  .superRefine((value, context) => {
    const ids = value.models.map((item) => item.id);
    if (new Set(ids).size !== ids.length)
      context.addIssue({ code: "custom", message: "模型 ID 不能重复" });
    if (value.defaultModelId && !ids.includes(value.defaultModelId))
      context.addIssue({
        code: "custom",
        message: "默认模型必须存在于模型列表中",
      });
  });
export type ModelConfig = z.infer<typeof schema>;
export type ModelDefinition = z.infer<typeof model>;
export type ModelSelection = { modelId: string | null; effort: string | null };
export const emptyConfig: ModelConfig = { version: 1, models: [] };
export const exampleConfig: ModelConfig = {
  version: 1,
  defaultModelId: "your-chat-model",
  models: [
    {
      id: "your-chat-model",
      name: "我的对话模型",
      provider: "本地服务",
      baseUrl: "http://127.0.0.1:11434/v1",
    },
    {
      id: "your-reasoning-model",
      name: "我的推理模型",
      provider: "自定义服务",
      reasoning: {
        options: [
          { value: "low", label: "低" },
          { value: "medium", label: "中" },
          { value: "high", label: "高" },
        ],
        default: "medium",
      },
    },
  ],
};
export function parseModelConfig(source: string): ModelConfig {
  let data: unknown;
  try {
    data = JSON.parse(source.replace(/^\uFEFF/, ""));
  } catch {
    throw new Error("JSON 格式有误，请检查逗号、引号和括号。");
  }
  const parsed = schema.safeParse(data);
  if (!parsed.success) {
    const issue = parsed.error.issues[0];
    throw new Error(
      `配置无效（${issue?.path.join(".") || "文件"}）：${issue?.message ?? "格式不符合要求"}`,
    );
  }
  return parsed.data;
}
export function resolveSelection(
  config: ModelConfig,
  previous: ModelSelection,
): ModelSelection {
  const current =
    config.models.find((item) => item.id === previous.modelId) ??
    config.models.find((item) => item.id === config.defaultModelId) ??
    config.models[0];
  if (!current) return { modelId: null, effort: null };
  return {
    modelId: current.id,
    effort: current.reasoning
      ? current.reasoning.options.some(
          (option) => option.value === previous.effort,
        )
        ? previous.effort
        : current.reasoning.default
      : null,
  };
}
