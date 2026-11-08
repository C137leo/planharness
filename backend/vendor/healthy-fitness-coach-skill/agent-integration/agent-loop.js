'use strict';

/**
 * 最小可用的 function-calling agent 循环。
 * 兼容任何 OpenAI 格式的 endpoint（OpenAI / DeepSeek / 本地 vLLM / Ollama 的 /v1）。
 *
 * 用法：
 *   node agent-loop.js --mock                                  # 不联网，验证工具链路
 *   LLM_BASE_URL=https://api.deepseek.com/v1 LLM_API_KEY=sk-xxx LLM_MODEL=deepseek-chat \
 *     node agent-loop.js "我30岁175cm80kg，想减脂，每周3天每次40分钟，只有哑铃和垫子"
 */

const fs = require('node:fs');
const path = require('node:path');
const { TOOLS, dispatch } = require('./tools.js');

// 单一真源：system prompt 直接读 skill 的 SKILL.md，不复制、不漂移
const SKILL_MD = fs.readFileSync(path.join(__dirname, '..', 'healthy-fitness-coach', 'SKILL.md'), 'utf8');

const SYSTEM_PROMPT = `${SKILL_MD}

---
# 你的运行方式（宿主约定）

你是上面这份 skill 的执行者。硬性规则：

1. **每一步都先调 coach_safety_gate**。返回 blocked=true 时只做安全分流，绝对不输出训练或饮食处方。
2. 需要训练计划 → coach_plan_program。返回 needs_input 时只问缺的字段，不要一次问完所有问题，也不要自己填默认值。
3. 今日训练 → coach_today_session。返回 regress 时只调整它给出的那 1 个变量，不要叠加别的调整。
4. 饮食 → coach_nutrition_targets。用户不愿记录热量时用 mode=habit。
5. 讲原理、动作细节、有氧编排、恢复、证据 → coach_read_reference 读对应文件后再回答，不要凭记忆编。
6. 不知道的写"未知"，不要编造用户的体重、伤病或训练记录。
7. 复盘 → coach_review_week。只陈述它返回的事实与判断，不要额外发明趋势。
8. 出现红旗症状时，输出里必须包含"停止自动训练建议并寻求专业评估"，不要软化。`;

async function callLlm(messages, { baseUrl, apiKey, model }) {
  const response = await fetch(`${baseUrl.replace(/\/$/, '')}/chat/completions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${apiKey}` },
    body: JSON.stringify({ model, messages, tools: TOOLS, tool_choice: 'auto', temperature: 0.3 })
  });
  if (!response.ok) throw new Error(`LLM ${response.status}: ${await response.text()}`);
  return response.json();
}

async function run(userText, options) {
  const messages = [
    { role: 'system', content: SYSTEM_PROMPT },
    { role: 'user', content: userText }
  ];

  for (let turn = 0; turn < 6; turn += 1) {
    const payload = await callLlm(messages, options);
    const message = payload.choices[0].message;
    messages.push(message);

    if (!message.tool_calls || message.tool_calls.length === 0) {
      return { answer: message.content, messages };
    }

    for (const call of message.tool_calls) {
      const args = JSON.parse(call.function.arguments || '{}');
      const result = dispatch(call.function.name, args);
      console.log(`[tool] ${call.function.name}(${JSON.stringify(args).slice(0, 80)}) -> ${result.status || result.safety?.status || (result.error ? 'error' : 'ok')}`);
      messages.push({
        role: 'tool',
        tool_call_id: call.id,
        content: JSON.stringify(result).slice(0, 12000) // 防止单次工具结果把 context 撑爆
      });
    }
  }
  return { answer: '达到最大工具轮数，未收敛。', messages };
}

// ---------------------------------------------------------------------------
// --mock：不联网，用固定脚本验证"工具真的被调用 + 参数形状正确"
// ---------------------------------------------------------------------------
function mock(userText) {
  const steps = [
    ['coach_safety_gate', { instruction: userText }],
    ['coach_plan_program', {
      profile: {
        goal: '减脂', experience_level: 'beginner', training_days_per_week: 3,
        available_time_min: 40, training_venue: 'home',
        available_equipment: ['哑铃', '瑜伽垫'], injury_or_medical_constraints: '无'
      },
      instruction: userText
    }],
    ['coach_nutrition_targets', {
      profile: { goal: '减脂', training_days_per_week: 3 },
      body: { sex: 'male', age: 30, height_cm: 175, weight_kg: 80 },
      mode: 'simplified',
      instruction: userText          // 原话必须转发，否则红旗检查看不见症状
    }],
    ['coach_today_session', {
      profile: {
        goal: '减脂', experience_level: 'beginner', training_days_per_week: 3,
        available_time_min: 40, available_equipment: ['哑铃'], injury_or_medical_constraints: '无'
      },
      currentState: { sleep: 5.5, fatigue: 5, pain: '无' },
      instruction: userText
    }]
  ];
  for (const [name, args] of steps) {
    const result = dispatch(name, args);
    const brief = result.status || result.safety?.status;
    console.log(`[mock tool] ${name} -> status=${brief}`);
    if (name === 'coach_plan_program' && result.program) {
      console.log('           结构:', result.program.structure, '| 天数:', result.program.days_per_week,
        '| 动作:', result.program.sessions[0].exercises.map((e) => e.name_zh).join(' / '));
    }
    if (name === 'coach_nutrition_targets' && result.status === 'ready') {
      console.log('           热量:', result.target_kcal, 'kcal | 蛋白:', JSON.stringify(result.macros.protein_g));
    }
    if (name === 'coach_today_session') {
      console.log('           判定:', result.status, '| 调整:', JSON.stringify(result.adjustment));
    }
  }
}

const mockIndex = process.argv.indexOf('--mock');
if (mockIndex !== -1) {
  const text = process.argv[mockIndex + 1] || '我30岁175cm80kg，想减脂，每周3天每次40分钟，只有哑铃和垫子';
  console.log('user:', text, '\n');
  mock(text);
  console.log('\n[mock] 工具链路 OK。配置 LLM_* 环境变量即可接真实模型。');
} else {
  const userText = process.argv[2];
  if (!userText) {
    console.error('用法: node agent-loop.js "你的问题"  或  node agent-loop.js --mock');
    process.exit(1);
  }
  const options = {
    baseUrl: process.env.LLM_BASE_URL,
    apiKey: process.env.LLM_API_KEY,
    model: process.env.LLM_MODEL
  };
  if (!options.baseUrl || !options.apiKey) {
    console.error('缺少 LLM_BASE_URL / LLM_API_KEY。先跑 node agent-loop.js --mock 验证工具链路。');
    process.exit(1);
  }
  run(userText, options).then(({ answer }) => console.log(`\n=== 回答 ===\n${answer}`))
    .catch((error) => { console.error('失败:', error.message); process.exitCode = 1; });
}
