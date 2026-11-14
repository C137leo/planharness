<script setup lang="ts">
/**
 * 登录 / 注册。
 *
 * 防滥用不是前端说了算，这里只做三件事：
 *   1. 每次提交前重新申请一次性票据（服务端签名，客户端改不了时间）；
 *   2. 蜜罐字段 + 最短填表时间提示，让脚本的成本变高；
 *   3. 拿到 429 时按 Retry-After 倒计时，不盲目重试。
 * 真正的守门在 `app/abuse.py`：分层限流、全站熔断、账号上限。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { api, ApiError, setStoredToken, type AuthChallenge } from '../../api'
import { resetCoachWorkspace } from '../../composables/coachWorkspace'

const mode = ref<'login' | 'register'>('login')
const busy = ref(false)
const error = ref('')
const notice = ref('')
const lockUntil = ref(0)
const now = ref(Date.now())
const challenge = ref<AuthChallenge | null>(null)
// 票据是"页面打开时就签发"，用户输入的时间才是填表耗时；
// 若在提交那一刻才申请，服务端只会看到几十毫秒 → 一律判成机器。
const challengeAt = ref(0)
const challengeError = ref('')

const form = reactive({
  phone: '',
  password: '',
  nickname: '',
  company: '', // 蜜罐：隐藏字段，真人不会填
})

let timer: ReturnType<typeof setInterval> | null = null

const lockedSeconds = computed(() =>
  lockUntil.value > now.value ? Math.ceil((lockUntil.value - now.value) / 1000) : 0,
)

const isRegister = computed(() => mode.value === 'register')
const canSubmit = computed(
  () =>
    !busy.value &&
    lockedSeconds.value === 0 &&
    /^1[3-9]\d{9}$/.test(form.phone.trim()) &&
    form.password.length >= 8,
)

const passwordHint = computed(() => {
  const value = form.password
  if (!value) return '至少 8 位，包含字母和数字'
  if (value.length < 8) return '还差 ' + (8 - value.length) + ' 位'
  if (!/[A-Za-z]/.test(value) || !/\d/.test(value)) return '还缺字母或数字'
  // 与后端 app/abuse.py 的 COMMON_PASSWORDS 保持同一批，避免
  // "前端说强度够用、后端说太常见"（评审 P-017）。
  const weak = [
    '12345678', '123456789', '1234567890', 'password', 'password1', 'qwerty123',
    '11111111', '00000000', 'abc12345', 'iloveyou', 'admin123', 'test1234',
    'a1234567', '1qaz2wsx', 'passw0rd', 'woaini1314',
  ]
  if (weak.includes(value.toLowerCase())) return '这个密码太常见了，换一个'
  if (new Set(value).size <= 3) return '重复字符太多，换一个'
  if (value.includes(form.phone)) return '别把手机号写进密码'
  return '强度够用'
})

const strength = computed(() => {
  const value = form.password
  if (!value) return 0
  let score = 0
  if (value.length >= 8) score += 1
  if (value.length >= 12) score += 1
  if (/[A-Za-z]/.test(value) && /\d/.test(value)) score += 1
  if (/[^A-Za-z0-9]/.test(value)) score += 1
  return Math.min(4, score)
})

function switchMode(next: 'login' | 'register') {
  mode.value = next
  error.value = ''
  notice.value = ''
  challenge.value = null
  fetchChallenge()
}

async function fetchChallenge() {
  const wanted = mode.value
  challengeError.value = ''
  let lastError = ''
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      const ticket = await api.authChallenge(wanted)
      // 用户可能在请求过程中切了登录/注册，旧 tab 的票据不能覆盖新 tab。
      if (mode.value !== wanted) return
      challenge.value = ticket
      challengeAt.value = Date.now()
      return
    } catch (err) {
      lastError = err instanceof Error ? err.message : '票据请求失败'
      if (attempt === 0) await sleep(250)
    }
  }
  if (mode.value !== wanted) return
  challenge.value = null
  challengeAt.value = 0
  challengeError.value = lastError
}

/** 票据有效期 10 分钟，这里留 1 分钟余量；过期或没有就重新取。 */
async function ensureTicket(): Promise<AuthChallenge | null> {
  const fresh =
    challenge.value !== null && Date.now() - challengeAt.value < 9 * 60 * 1000
  if (fresh) return challenge.value
  await fetchChallenge()
  return challenge.value
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/** 把一次提交最多重试一次：处理"票据刚签发就点了"和"票据已被用掉"两种情况。 */
async function submitWithTicket(
  call: (ticket: { challenge_id: string; challenge_token: string }) => Promise<unknown>,
): Promise<void> {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    let ticket = await ensureTicket()
    if (!ticket) {
      // 页面打开时的请求可能刚好失败；提交前再争取一次，不直接判死。
      await fetchChallenge()
      ticket = await ensureTicket()
    }
    if (!ticket) {
      error.value = challengeError.value
        ? `票据没拿到：${challengeError.value}。再点一次试试`
        : '网络不给力，票据没取到，请再点一次'
      return
    }
    try {
      await call({
        challenge_id: ticket.challenge_id,
        challenge_token: ticket.token,
      })
      return
    } catch (err) {
      const message = err instanceof Error ? err.message : ''
      const tooFast = message.includes('太快')
      const stale = message.includes('已被使用') || message.includes('请刷新页面')

      if (attempt === 1 || (!tooFast && !stale)) {
        handleError(err)
        // 票据可能已被这次失败的提交用掉（服务端先验票据再验策略），
        // 无论什么原因失败都换一张新的，下一次点击才不会撞在旧票据上
        await fetchChallenge()
        return
      }

      if (tooFast) {
        // 等过最短填表时间再重试；这不是绕过校验，而是把"人手速度"如实补上
        notice.value = '校验刚签发，正在按正常速度重试…'
        await sleep(1400)
      } else {
        await fetchChallenge()
      }
    }
  }
}

function startCountdown(seconds: number) {
  lockUntil.value = Date.now() + seconds * 1000
  if (timer) clearInterval(timer)
  timer = setInterval(() => {
    now.value = Date.now()
    if (lockUntil.value <= now.value && timer) {
      clearInterval(timer)
      timer = null
    }
  }, 500)
}

async function submit() {
  if (!canSubmit.value) return
  busy.value = true
  error.value = ''
  notice.value = ''

  try {
    const base = { phone: form.phone.trim(), password: form.password }
    await submitWithTicket(async (ticket) => {
      const token = isRegister.value
        ? await api.register({
            ...base,
            ...ticket,
            nickname: form.nickname.trim() || '新用户',
            company: form.company,
          })
        : await api.login({ ...base, ...ticket })

      setStoredToken(token.access_token)
      resetCoachWorkspace()
      uni.showToast({ title: isRegister.value ? '注册成功' : '欢迎回来', icon: 'success' })
      setTimeout(() => uni.switchTab({ url: '/pages/today/index' }), 400)
    })
  } finally {
    busy.value = false
    notice.value = ''
  }
}

/** demo 账号：演示用，同样受服务端 IP 限流约束。 */
async function useDemo() {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    const token = await api.demoLogin()
    setStoredToken(token.access_token)
    resetCoachWorkspace()
    uni.showToast({ title: '已进入演示账号', icon: 'success' })
    setTimeout(() => uni.switchTab({ url: '/pages/today/index' }), 400)
  } catch (err) {
    handleError(err)
  } finally {
    busy.value = false
  }
}

function handleError(err: unknown) {
  if (err instanceof ApiError) {
    error.value = err.message
    if (err.statusCode === 429) {
      // 服务端把秒数写进了文案（"…（123 秒后可重试）"），优先用它
      const matched = err.message.match(/(\d+)\s*秒/)
      const seconds = matched ? Number(matched[1]) : 30
      startCountdown(Number.isFinite(seconds) && seconds > 0 ? seconds : 30)
    }
    return
  }
  error.value = err instanceof Error ? err.message : '请求失败'
}

onMounted(() => {
  // 打开页面就签发：用户输手机号 + 密码的时间（通常 >2 秒）就是真实的填表耗时
  fetchChallenge()
})
</script>

<template>
  <view class="page login-page">
    <view class="eyebrow">AI FITNESS COACH</view>
    <view class="title">{{ isRegister ? '开一个账号，开始练。' : '欢迎回来。' }}</view>
    <view class="subtitle">
      {{ isRegister ? '填 4 项条件，教练按你的目标、时间和器械排计划。' : '登录后继续你的计划与打卡。' }}
    </view>

    <!-- 评审 P-006：首访先回答"这是什么/能帮我什么"，而不是直接甩一个登录框 -->
    <view class="login-value">
      <text class="login-value-title">没时间的打工人，你需要一个“不费脑”的AI私教</text>
      <text class="login-value-copy">
        别再去套那些看不懂的健身公式了！只要告诉我你的目标、一周能练几天、有什么器械，3分钟给你排好这周的跟练表。身体不舒服？系统会自动帮你避开风险动作！
      </text>
      <view class="login-points">
        <text>· <text class="point-strong">告别长篇大论</text>：今天练什么，一屏清清楚楚。</text>
        <text>· <text class="point-strong">拒绝反人类填表</text>：想改计划？直接用大白话告诉教练。</text>
        <text>· <text class="point-strong">拒绝硬扛</text>：每周根据你的实际完成度动态调整，练不动就减量。</text>
      </view>
    </view>

    <view class="card">
      <view class="tabs">
        <view class="tab" :class="{ active: mode === 'login' }" @click="switchMode('login')">登录</view>
        <view class="tab" :class="{ active: mode === 'register' }" @click="switchMode('register')">
          注册
        </view>
      </view>

      <view class="field">
        <text class="label">手机号</text>
        <input v-model="form.phone" class="input" type="number" maxlength="11" placeholder="11 位中国大陆手机号" />
      </view>

      <view v-if="isRegister" class="field">
        <text class="label">昵称（可选）</text>
        <input v-model="form.nickname" class="input" maxlength="20" placeholder="怎么称呼你" />
      </view>

      <view class="field">
        <text class="label">密码</text>
        <input v-model="form.password" class="input" password maxlength="72" placeholder="至少 8 位，含字母和数字" />
        <view class="strength" v-if="isRegister && form.password">
          <view class="bar" v-for="index in 4" :key="index" :class="{ on: strength >= index }" />
        </view>
        <text class="hint" v-if="isRegister">{{ passwordHint }}</text>
      </view>

      <!-- 蜜罐：视觉上完全隐藏，真人看不到 -->
      <view class="hp" aria-hidden="true">
        <input v-model="form.company" name="company" tabindex="-1" autocomplete="off" />
      </view>

      <view v-if="error" class="error" style="margin-top: 12px">{{ error }}</view>
      <view v-if="lockedSeconds" class="notice" style="margin-top: 12px">
        {{ error || '提交太频繁' }}（还剩 {{ lockedSeconds }} 秒）
      </view>

      <view class="btn-row login-actions">
        <view class="btn-sm primary" :class="{ disabled: !canSubmit }" @click="submit">
          {{ busy ? '处理中…' : lockedSeconds ? `等待 ${lockedSeconds}s` : isRegister ? '注册并开始' : '登录' }}
        </view>
        <view class="btn-sm" @click="useDemo">先用演示账号看看</view>
      </view>

      <view class="muted" style="margin-top: 12px">
        {{ isRegister
          ? '注册即建画像；手机号只用于登录，不做短信验证（demo 没有短信通道）。'
          : '忘记密码暂时没有自助找回，demo 阶段请重新注册。' }}
      </view>
    </view>

  </view>
</template>

<style>
/* 评审 P-027：登录卡在 1440 下横跨全屏，输入动线太长；收窄并居中 */
.login-page {
  margin: 0 auto;
  max-width: 520px;
}

/* 评审 P-006：首访价值说明 */
.login-value {
  border-left: 3px solid var(--primary);
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 0 0 16px;
  padding: 2px 0 2px 12px;
}
.login-value-title {
  color: var(--ink);
  display: block;
  font-size: 15px;
  font-weight: 700;
}
.login-value-copy {
  color: var(--muted);
  display: block;
  font-size: 13px;
  line-height: 1.6;
}
.login-points {
  color: var(--ink);
  display: flex;
  flex-direction: column;
  font-size: 12px;
  gap: 2px;
}
.login-points > text { display: block; line-height: 1.7; }
.login-points .point-strong { display: inline; font-weight: 700; color: var(--ink); }
.login-actions { justify-content: flex-end; }

.tabs {
  background: var(--line-soft);
  border-radius: 999px;
  display: flex;
  gap: 4px;
  padding: 4px;
}

.tab {
  border-radius: 999px;
  color: var(--muted);
  flex: 1;
  font-size: 14px;
  padding: 9px 0;
  text-align: center;
}

.tab.active {
  background: var(--surface);
  color: var(--primary-deep);
  font-weight: 600;
}

.field {
  margin-top: 16px;
}

.label {
  color: var(--muted);
  display: block;
  font-size: 12px;
  margin-bottom: 6px;
}

.hint {
  color: var(--muted);
  font-size: 12px;
  margin-top: 6px;
}

.strength {
  display: flex;
  gap: 4px;
  margin-top: 8px;
  /* 评审 P-026：固定短条，避免 4 段被拉满整行像分割线 */
  max-width: 180px;
}

.bar {
  background: var(--line);
  border-radius: 999px;
  flex: 1;
  height: 4px;
}

.bar.on {
  background: var(--primary);
}

/* 蜜罐：移出可视区，但仍在 DOM 里，脚本会填 */
.hp {
  height: 0;
  left: -9999px;
  overflow: hidden;
  position: absolute;
  width: 0;
}

.btn-sm.disabled {
  opacity: 0.45;
}
</style>
