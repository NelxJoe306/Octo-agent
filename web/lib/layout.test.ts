import test from "node:test";
import assert from "node:assert/strict";
import { fitWorkspace } from "./layout";

test("缩小窗口及拖宽侧栏时仍为聊天保留空间，右窗不覆盖聊天", () => {
  for (const width of [760, 804, 820, 980, 1280, 1920]) {
    const result = fitWorkspace({
      width,
      left: 420,
      right: 480,
      treeOpen: true,
    });
    assert.equal(result.kind, "wide");
    assert.ok(result.chat >= 320, `width=${width}`);
    assert.equal(result.left + result.chat + result.right, width);
  }
  const compact = fitWorkspace({
    width: 700,
    left: 420,
    right: 480,
    treeOpen: true,
  });
  assert.equal(compact.kind, "compact");
  assert.equal(compact.left, 0);
  assert.ok(compact.chat >= 320);
  const narrow = fitWorkspace({
    width: 390,
    left: 248,
    right: 276,
    treeOpen: true,
  });
  assert.equal(narrow.kind, "narrow");
  assert.equal(narrow.chat, 0);
  assert.equal(narrow.right, 390);
  assert.equal(
    fitWorkspace({ width: 390, left: 248, right: 276, treeOpen: false }).chat,
    390,
  );
});
