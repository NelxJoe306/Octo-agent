"use client";

import { useEffect, useRef, useState } from "react";
import { FileJson, FolderOpen, RefreshCw, Save } from "lucide-react";
import { emptyConfig, exampleConfig, parseModelConfig } from "@/lib/models";
import type { ModelConfig } from "@/lib/models";

type PickerOptions = {
  multiple?: boolean;
  suggestedName?: string;
  types?: { description: string; accept: Record<string, string[]> }[];
};
declare global {
  interface Window {
    showOpenFilePicker?: (
      options: PickerOptions,
    ) => Promise<FileSystemFileHandle[]>;
    showSaveFilePicker?: (
      options: PickerOptions,
    ) => Promise<FileSystemFileHandle>;
  }
}
type Source =
  | { kind: "none" }
  | { kind: "imported"; name: string }
  | {
      kind: "file";
      name: string;
      handle: FileSystemFileHandle;
      original: string;
    };
const pickerOptions: PickerOptions = {
  types: [
    { description: "Octo 模型配置", accept: { "application/json": [".json"] } },
  ],
};

async function writeFile(handle: FileSystemFileHandle, content: string) {
  const writer = await handle.createWritable();
  try {
    await writer.write(content);
    await writer.close();
  } catch (error) {
    await writer.abort().catch(() => undefined);
    throw error;
  }
}

export function useLocalModels() {
  const [config, setConfig] = useState<ModelConfig>(emptyConfig);
  const [draft, setDraft] = useState(JSON.stringify(emptyConfig, null, 2));
  const [source, setSource] = useState<Source>({ kind: "none" });
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [canPick, setCanPick] = useState(false);
  const [exported, setExported] = useState<{
    url: string;
    draft: string;
  } | null>(null);
  const fallback = useRef<HTMLInputElement>(null);
  useEffect(() => {
    setCanPick(typeof window.showOpenFilePicker === "function");
  }, []);
  useEffect(
    () => () => {
      if (exported) URL.revokeObjectURL(exported.url);
    },
    [exported],
  );
  function editDraft(value: string) {
    setDraft(value);
    setExported(null);
    setStatus("");
    setError("");
  }
  function failed(error: unknown) {
    if (error instanceof DOMException && error.name === "AbortError") return;
    setError(
      error instanceof Error ? error.message : "无法访问配置文件，请重试。",
    );
  }
  function accept(text: string, next: Source) {
    const parsed = parseModelConfig(text);
    setConfig(parsed);
    setExported(null);
    setDraft(JSON.stringify(parsed, null, 2));
    setSource(next);
    setError("");
    setStatus(`已加载 ${parsed.models.length} 个模型`);
  }
  async function open() {
    if (!window.showOpenFilePicker) {
      fallback.current?.click();
      return;
    }
    setBusy(true);
    setError("");
    try {
      const [handle] = await window.showOpenFilePicker({
        ...pickerOptions,
        multiple: false,
      });
      if (!handle) return;
      const file = await handle.getFile();
      const text = await file.text();
      accept(text, { kind: "file", name: file.name, handle, original: text });
    } catch (error) {
      failed(error);
    } finally {
      setBusy(false);
    }
  }
  async function importFile(file: File) {
    setBusy(true);
    setError("");
    try {
      accept(await file.text(), { kind: "imported", name: file.name });
    } catch (error) {
      failed(error);
    } finally {
      setBusy(false);
    }
  }
  async function reload() {
    if (source.kind !== "file") return;
    setBusy(true);
    setError("");
    try {
      const file = await source.handle.getFile();
      const text = await file.text();
      accept(text, { ...source, original: text });
    } catch (error) {
      failed(error);
    } finally {
      setBusy(false);
    }
  }
  async function save() {
    setError("");
    let parsed: ModelConfig;
    try {
      parsed = parseModelConfig(draft);
    } catch (error) {
      failed(error);
      return;
    }
    const content = JSON.stringify(parsed, null, 2) + "\n";
    setBusy(true);
    try {
      if (source.kind === "file") {
        const current = await (await source.handle.getFile()).text();
        if (current !== source.original)
          throw new Error("文件已被其他程序修改。请先重新读取，再编辑保存。");
        await writeFile(source.handle, content);
        accept(content, { ...source, original: content });
        setStatus("已保存到本机配置文件");
      } else if (window.showSaveFilePicker) {
        const handle = await window.showSaveFilePicker({
          ...pickerOptions,
          suggestedName:
            source.kind === "imported" ? source.name : "octo-models.json",
        });
        await writeFile(handle, content);
        accept(content, {
          kind: "file",
          name: handle.name,
          handle,
          original: content,
        });
        setStatus("已保存到本机配置文件");
      } else {
        setConfig(parsed);
        setExported({
          url: URL.createObjectURL(
            new Blob([content], { type: "application/json" }),
          ),
          draft,
        });
        setStatus("已应用到本次预览。点击下载链接，将配置保存到本机。");
      }
    } catch (error) {
      failed(error);
    } finally {
      setBusy(false);
    }
  }
  function apply() {
    try {
      const parsed = parseModelConfig(draft);
      setConfig(parsed);
      setError("");
      setStatus("已应用到本次预览；尚未写入配置文件。");
    } catch (error) {
      failed(error);
    }
  }
  return {
    config,
    draft,
    setDraft: editDraft,
    source,
    status,
    error,
    busy,
    fallback,
    canPick,
    exportUrl: exported?.draft === draft ? exported.url : null,
    open,
    importFile,
    reload,
    save,
    apply,
  };
}
export type LocalModels = ReturnType<typeof useLocalModels>;

export function LocalModelSettings({ models }: { models: LocalModels }) {
  return (
    <>
      <p className="settings-description">
        模型由你选择的本机 JSON 文件管理，直接在浏览器读取和保存。
      </p>
      <div className="config-file-row">
        <FileJson size={19} />
        <div>
          <strong>
            {models.source.kind === "none"
              ? "尚未打开配置文件"
              : models.source.name}
          </strong>
          <small>
            {models.source.kind === "imported"
              ? "当前浏览器使用文件导入，保存时将导出新文件；刷新后需重新导入。"
              : "配置不会上传到服务器；刷新页面后需重新打开。"}
          </small>
        </div>
      </div>
      <div className="config-actions">
        {models.canPick ? (
          <button
            className="button"
            disabled={models.busy}
            onClick={models.open}
          >
            <FolderOpen size={14} />
            打开配置文件
          </button>
        ) : (
          <label className="button config-open">
            <FolderOpen size={14} />
            打开配置文件
            <input
              ref={models.fallback}
              className="config-file-input"
              type="file"
              accept=".json,application/json"
              aria-label="打开配置文件"
              disabled={models.busy}
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) void models.importFile(file);
                event.target.value = "";
              }}
            />
          </label>
        )}
        <button
          className="button"
          disabled={models.busy || models.source.kind !== "file"}
          onClick={models.reload}
        >
          <RefreshCw size={14} />
          重新读取
        </button>
        <button
          className="text-button"
          disabled={models.busy}
          onClick={() => {
            models.setDraft(JSON.stringify(exampleConfig, null, 2));
          }}
        >
          填入示例
        </button>
      </div>
      <label className="config-editor-label" htmlFor="model-config">
        模型配置
      </label>
      <textarea
        id="model-config"
        className="config-editor"
        disabled={models.busy}
        spellCheck={false}
        value={models.draft}
        onChange={(event) => models.setDraft(event.target.value)}
      />
      <p className="settings-note">
        name 为显示名称，id 为实际模型 ID；仅支持强度调节的模型添加
        reasoning.options 与
        default。示例中的名称、ID、能力需要替换成你实际使用的模型。
      </p>
      {models.error && (
        <p className="config-error" role="alert">
          {models.error}
        </p>
      )}
      <div className="config-actions">
        <button
          className="button"
          disabled={models.busy}
          onClick={models.apply}
        >
          应用到预览
        </button>
        <button
          className="button primary"
          disabled={models.busy}
          onClick={models.save}
        >
          <Save size={14} />
          保存配置文件
        </button>
        {models.exportUrl && (
          <a
            className="button"
            href={models.exportUrl}
            download="octo-models.json"
          >
            下载配置文件
          </a>
        )}
        <span className="config-status" role="status">
          {models.status}
        </span>
      </div>
      <p className="settings-note">
        当前仍为演示对话，模型配置尚未接入推理调用。浏览器无法读取系统环境变量或自动访问任意磁盘路径。
      </p>
    </>
  );
}
