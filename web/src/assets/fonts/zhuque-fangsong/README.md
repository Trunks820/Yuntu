# 朱雀仿宋（Zhuque Fangsong）字体切片说明

## 上游来源与授权

- **上游仓库与 release**：`https://github.com/TrionesType/zhuque`
- **当前版本**：`v0.212`（technical preview，字体元数据版本 `0.212;TRIO;ZhuqueFangsong-Regular`）
- **许可证**：SIL Open Font License 1.1（详见同目录 [`OFL.txt`](./OFL.txt)）
- **当前目录产物**：`185` 个按 `unicode-range` 拆分的 `.woff2` 切片文件及 [`zhuque-fangsong.css`](./zhuque-fangsong.css)

## 切分记录

- **切片工具与版本**：未记录（`docs/design/yuntu-spread-20261022/HANDOFF.md` 中仅示例性提及“如 `cn-font-split`”，仓库与本地缓存均未保留当时实际使用的工具名称及版本号）
- **实际执行的命令与参数**：未记录

## 后续更换版本 / 重切步骤

1. **获取上游产物**：仅从上游 release 页面（`https://github.com/TrionesType/zhuque/releases`）下载目标版本的原始 `.ttf` 字体文件与随附的 `OFL.txt`（不从第三方 npm 重打包源获取）。
2. **清理旧切片**：清空本目录下旧版本的 `.woff2` 文件与旧的 `zhuque-fangsong.css`，用新版许可证覆盖 [`OFL.txt`](./OFL.txt)。
3. **按 `unicode-range` 重切为 `.woff2`**：使用支持 `unicode-range` 按需分片的字体切分工具（如 `cn-font-split`）切分完整 `.ttf`，输出到本目录并生成同目录下的 `zhuque-fangsong.css`。
4. **核对 CSS 规则格式**（须符合 [`scripts/verify-spread-fonts.mjs`](../../../../scripts/verify-spread-fonts.mjs) 与 [`src/__tests__/spreadFonts.test.ts`](../../../__tests__/spreadFonts.test.ts) 的校验）：
   - 文件头注明上游版本、来源与 `./OFL.txt`；
   - 每条 `@font-face` 使用 `font-family: "Zhuque Fangsong"`；
   - 保持单字重写法 `font-weight: 100 900;`、`font-style: normal;`、`font-display: swap;`；
   - `src` 使用同目录相对路径 `url("./<chunk>.woff2") format("woff2")`，不要包含 `local(...)`；
   - 每条规则均带有效 `unicode-range: U+...`，且与磁盘上的 `.woff2` 文件一一对应，无多余孤立文件。
5. **运行校验**：在 `yuntu-web` 根目录执行以下三项，确认全部通过：

```bash
node scripts/verify-spread-fonts.mjs
npm test -- src/__tests__/spreadFonts.test.ts
npm run build
```
