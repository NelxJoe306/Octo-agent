# Octo 图标

日期：2026-11-12。来源为用户提供的绿色底板、白色机器人图形。

最终资源：`web/public/octo-mark-black.png`，1254×1254 RGBA PNG。绿色底板与外围底色删除，机器人主体黑色，眼睛及主体外区域透明。生成方式：内置 image_gen 图像编辑；未使用 CLI 或 Python 改图。

`components/mark.tsx` 统一引用该资源，覆盖侧栏、空状态、消息署名与设置标识；`app/layout.tsx` 同时将其作为页面图标。深色主题通过 CSS 反色保持对比，文件本身仍为黑色版本。

检查：PNG alpha 范围0–255、角点透明；浏览器浅色页面实际观察主体、眼睛镂空与透明边界；生产构建和类型检查通过。未commit。

## 最终提示词

Edit the provided image into a production UI icon. Preserve the central robot symbol's exact silhouette, geometry, proportions, rounded rectangular head, two curved connecting stems, and two rounded rectangular feet. Remove the entire green rounded-square tile and the off-white outer background completely. Convert only the original white robot symbol to solid pure black (#000000). The right-pointing chevron eye and circular eye are negative-space transparent cutouts through the black head, never green or white pixels. All spaces outside the robot, including the gap between stems and feet, must have true alpha transparency. Flat vector-like crisp edges with antialiasing only; no gradients, shadows, textures, background, extra outlines, or new details. Tightly frame the preserved robot with modest equal transparent padding; do not retain the large tile-sized empty margins. This is a precise recolor and background-extraction edit of the given logo, not a redesign.
