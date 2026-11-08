# 动作图原始文件（只做本地留档）

这里放的是 2048×2048 / 1536×1536 的原始 PNG（AI 生成，带水印），**不会被打包进前端**，
也不参与运行时加载。

真正部署的是处理后的 320×320 JPEG：

```
frontend/src/static/ex/pull-up.jpg
frontend/src/static/ex/dead-bug.jpg
frontend/src/static/ex/dumbbell-bench-press.jpg
frontend/src/static/ex/deadlift.jpg
frontend/src/static/ex/walking-lunge.jpg
frontend/src/static/ex/leg-press.jpg
frontend/src/static/ex/seated-row.jpg
frontend/dist/build/h5/static/ex/*.jpg   # npm run build:h5 后生成
```

映射在 `frontend/src/styles/thumbs.ts`：先按 `action_ref` 精确匹配，再按动作模式匹配，
最后退回 `legs/hip/chest/back/core/stretch` 六张部位图。

处理方式（macOS `sips`，裁掉边缘水印并压到 320）：

```bash
sips -c 1800 1800 "划船.png" --out /tmp/seated-row.png
sips -z 320 320 /tmp/seated-row.png \
  --out frontend/src/static/ex/seated-row.jpg \
  -s format jpeg -s formatOptions 82
```

`腿举.png` 原始尺寸是 1536，裁切参数用 1360。原始 PNG 较大（7 张约 22MB），
已加入 `.gitignore`，只在本机留档；仓库里只提交运行时用的 JPEG。
