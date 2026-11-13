export function fitWorkspace({
  width,
  left,
  right,
  treeOpen,
}: {
  width: number;
  left: number;
  right: number;
  treeOpen: boolean;
}) {
  const clamp = (value: number, min: number, max: number) =>
    Math.round(Math.max(min, Math.min(max, value)));
  const kind = width >= 760 ? "wide" : width >= 600 ? "compact" : "narrow";
  const leftRoom =
    kind === "wide"
      ? clamp(left, 192, Math.min(420, width - (treeOpen ? 220 : 0) - 320))
      : 0;
  const rightRoom = !treeOpen
    ? 0
    : kind === "narrow"
      ? width
      : clamp(right, 220, Math.min(480, width - leftRoom - 320));
  return {
    kind,
    left: leftRoom,
    right: rightRoom,
    chat: width - leftRoom - rightRoom,
    leftMax: kind === "wide" ? Math.min(420, width - rightRoom - 320) : 420,
    rightMax: kind === "narrow" ? width : Math.min(480, width - leftRoom - 320),
    drawerWidth: Math.max(0, Math.min(280, width - 32)),
  };
}
