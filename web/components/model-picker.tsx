"use client";
import { useEffect, useRef, useState } from "react";
import { Check, ChevronDown, FileJson, Search, Settings2 } from "lucide-react";
import type { ModelConfig, ModelSelection } from "@/lib/models";

export function ModelPicker({
  config,
  selection,
  onChange,
  onConfigure,
}: {
  config: ModelConfig;
  selection: ModelSelection;
  onChange: (value: ModelSelection) => void;
  onConfigure: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const search = useRef<HTMLInputElement>(null);
  const selected = config.models.find((item) => item.id === selection.modelId);
  const strength = selected?.reasoning?.options.find(
    (option) => option.value === selection.effort,
  );
  useEffect(() => {
    if (!open) return;
    search.current?.focus();
    function close(event: PointerEvent) {
      if (event.target instanceof Node && !root.current?.contains(event.target))
        setOpen(false);
    }
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, [open]);
  const filtered = config.models.filter((item) =>
    `${item.name} ${item.id} ${item.provider}`
      .toLocaleLowerCase()
      .includes(query.toLocaleLowerCase()),
  );
  return (
    <div
      className="model-picker"
      ref={root}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.stopPropagation();
          setOpen(false);
          trigger.current?.focus();
        }
      }}
    >
      <button
        ref={trigger}
        className="model-trigger"
        aria-label="选择模型"
        aria-expanded={open}
        aria-controls="model-popover"
        onClick={() => {
          setOpen((value) => !value);
          setQuery("");
        }}
      >
        <span>{selected?.name ?? "选择模型"}</span>
        {strength && <small>{strength.label}</small>}
        <ChevronDown size={13} />
      </button>
      {open && (
        <div className="model-popover" id="model-popover">
          <div className="model-popover-heading">
            选择模型<span>本地配置</span>
          </div>
          {config.models.length > 0 && (
            <label className="model-search">
              <Search size={14} />
              <input
                ref={search}
                aria-label="搜索模型"
                placeholder="搜索模型名称"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
              />
            </label>
          )}
          <div className="model-options" role="group" aria-label="可用模型">
            {filtered.map((item) => (
              <button
                className="model-option"
                key={item.id}
                aria-pressed={item.id === selection.modelId}
                onClick={() =>
                  onChange(
                    item.id === selection.modelId
                      ? selection
                      : {
                          modelId: item.id,
                          effort: item.reasoning?.default ?? null,
                        },
                  )
                }
              >
                <div>
                  <strong>{item.name}</strong>
                  <small>
                    {item.provider} · {item.id}
                  </small>
                </div>
                {item.id === selection.modelId && <Check size={15} />}
              </button>
            ))}
            {!filtered.length && (
              <div className="model-empty">
                <FileJson size={22} />
                <strong>
                  {config.models.length ? "没有找到模型" : "添加你使用的模型"}
                </strong>
                <p>
                  {config.models.length
                    ? "试试其他名称或模型 ID。"
                    : "打开本机配置文件后，模型将显示在这里。"}
                </p>
              </div>
            )}
          </div>
          {selected?.reasoning && (
            <div className="reasoning-options">
              <div>推理强度</div>
              <div className="segmented" role="group" aria-label="推理强度">
                {selected.reasoning.options.map((option) => (
                  <button
                    key={option.value}
                    aria-pressed={selection.effort === option.value}
                    onClick={() =>
                      onChange({ ...selection, effort: option.value })
                    }
                  >
                    {option.label}
                  </button>
                ))}
              </div>
            </div>
          )}
          <button
            className="model-manage"
            onClick={() => {
              setOpen(false);
              onConfigure();
            }}
          >
            <Settings2 size={14} />
            管理本地模型
            <ChevronDown size={12} />
          </button>
        </div>
      )}
    </div>
  );
}
