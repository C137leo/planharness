<script setup lang="ts">
import { onLaunch } from '@dcloudio/uni-app'
import { ensureToken } from './api'

onLaunch(() => {
  ensureToken().catch(() => {
    // The pages show a retry state; do not crash the app on cold start.
  })
})
</script>

<style>
@import "./styles/theme.css";

page {
  background: var(--canvas, #fdf2e9);
  color: var(--ink, #2b1a12);
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC',
    'Helvetica Neue', Arial, sans-serif;
}

view,
text {
  box-sizing: border-box;
}

.page {
  min-height: 100vh;
  padding: 24px 18px 96px;
}

.eyebrow {
  color: var(--muted, #8a7466);
  font-size: 12px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.title {
  font-size: 28px;
  font-weight: 700;
  line-height: 1.25;
  margin: 8px 0 6px;
}

.subtitle {
  color: var(--muted, #8a7466);
  font-size: 14px;
  line-height: 1.6;
}

.card {
  background: #ffffff;
  border: 1px solid #e4e9ea;
  border-radius: 18px;
  padding: 18px;
  margin-top: 14px;
}

.row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.chip {
  background: #eaf6f0;
  border-radius: 999px;
  color: #1d6b4f;
  display: inline-block;
  font-size: 12px;
  padding: 6px 10px;
}

.btn {
  align-items: center;
  background: var(--primary, #e2552b);
  border: none;
  border-radius: 14px;
  color: #ffffff;
  display: flex;
  font-size: 15px;
  font-weight: 600;
  justify-content: center;
  margin-top: 14px;
  padding: 14px 16px;
  width: 100%;
}

.btn.secondary {
  background: #ffffff;
  border: 1px solid #cdd8d8;
  color: #253b3d;
}

.btn.danger {
  background: #a33a32;
}

.btn[disabled] {
  opacity: 0.45;
}

.muted {
  color: var(--muted, #8a7466);
  font-size: 13px;
  line-height: 1.55;
}

.notice {
  background: #fdf6ec;
  border: 1px solid #f2dfc4;
  border-radius: 14px;
  color: #7a5a20;
  font-size: 13px;
  line-height: 1.6;
  padding: 12px 14px;
  white-space: pre-line;
}

.error {
  background: #fdeceb;
  border: 1px solid #f3cdc8;
  border-radius: 14px;
  color: #9a2f22;
  font-size: 13px;
  line-height: 1.6;
  padding: 12px 14px;
}
</style>
