'use strict';

/**
 * 给你的 agent 直接用的 tool 定义 + 分发器。
 * 格式是 OpenAI / DeepSeek 通用的 function-calling schema，其他框架（LangChain、自定义 ReAct）
 * 只需映射 name/description/parameters 三个字段。
 */

const fs = require('node:fs');
const path = require('node:path');
const coach = require('./coach.js');

const REFERENCE_WHITELIST = new Set(
  fs.readdirSync(path.join(coach.SKILL_ROOT, 'references'))
    .filter((name) => name.endsWith('.md'))
);

const PROFILE_SCHEMA = {
  type: 'object',
  description: '用户稳定画像。字段名必须与 athlete-state.js 白名单一致。',
  properties: {
    goal: { type: 'string', description: '减脂 / 增肌 / 力量 / 心肺 / 综合健康' },
    experience_level: { type: 'string', enum: ['beginner', 'intermediate', 'advanced'] },
    training_days_per_week: { type: 'integer', minimum: 1, maximum: 7 },
    available_time_min: { type: 'integer', minimum: 5 },
    training_venue: { type: 'string', description: 'gym / home / outdoor' },
    available_equipment: { type: 'array', items: { type: 'string' } },
    injury_or_medical_constraints: { type: 'string', description: '没有写"无"' },
    long_term_preferences: { type: 'array', items: { type: 'string' } }
  },
  required: ['goal', 'experience_level', 'training_days_per_week', 'available_time_min', 'available_equipment', 'injury_or_medical_constraints']
};

const TOOLS = [
  {
    type: 'function',
    function: {
      name: 'coach_safety_gate',
      description: '第一优先级。判断输入里是否有运动红旗症状（胸痛、晕厥、异常心悸、进行性麻木无力、近期手术等）。返回 blocked=true 时禁止生成任何训练处方，只做安全分流。任何训练/营养请求都应先调用它。',
      parameters: {
        type: 'object',
        properties: {
          instruction: { type: 'string', description: '用户原话' },
          profile: { type: 'object', description: '可选的画像（含 injury_or_medical_constraints）' },
          currentState: { type: 'object', description: '可选的当前状态（pain / symptoms / red_flags）' }
        },
        required: ['instruction']
      }
    }
  },
  {
    type: 'function',
    function: {
      name: 'coach_plan_program',
      description: '生成训练计划的骨架：每周几天、每次几分钟、每次训练的动作模式、组次区间、RIR 目标、进阶与降级规则、最低有效版本。缺失必要信息时返回 needs_input + 需要问用户的字段，不要自己编造。',
      parameters: {
        type: 'object',
        properties: {
          profile: PROFILE_SCHEMA,
          instruction: { type: 'string' },
          outputDirectory: { type: 'string', description: '可选。给了就把 CURRENT_PROGRAM.md 落盘（不覆盖已有文件）' }
        },
        required: ['profile']
      }
    }
  },
  {
    type: 'function',
    function: {
      name: 'coach_today_session',
      description: '今日训练适配。根据睡眠/疲劳/疼痛/可用时间返回 proceed | regress | stop，并给出本次只调整的那 1 个变量、最低任务、停止条件、训练后必记字段。',
      parameters: {
        type: 'object',
        properties: {
          profile: PROFILE_SCHEMA,
          currentState: {
            type: 'object',
            properties: {
              sleep: { type: ['number', 'string'], description: '小时数或"差"' },
              fatigue: { type: ['number', 'string'], description: '1-10 或"很累"' },
              stress: { type: ['number', 'string'] },
              pain: { type: ['number', 'string'], description: '没有写"无"；"无"不会触发降级' },
              available_time_min: { type: 'integer' }
            }
          },
          sessionId: { type: 'string', description: '计划里的 session_id，如 session_1' },
          instruction: { type: 'string' }
        },
        required: ['profile']
      }
    }
  },
  {
    type: 'function',
    function: {
      name: 'coach_nutrition_targets',
      description: '计算营养目标：BMR（Mifflin-St Jeor）、TDEE、温和缺口/小幅盈余后的目标热量、蛋白质 1.4-2.0 g/kg、脂肪与碳水区间。支持 precise / simplified / habit 三种模式。不给补剂或药物建议。',
      parameters: {
        type: 'object',
        properties: {
          profile: PROFILE_SCHEMA,
          body: {
            type: 'object',
            properties: {
              sex: { type: 'string', enum: ['male', 'female'] },
              age: { type: 'integer' },
              height_cm: { type: 'number' },
              weight_kg: { type: 'number' },
              activity_factor: { type: 'number', description: '可选，覆盖按训练天数推算的活动系数' }
            },
            required: ['sex', 'age', 'height_cm', 'weight_kg']
          },
          mode: { type: 'string', enum: ['precise', 'simplified', 'habit'], description: '默认 simplified' }
        },
        required: ['profile', 'body']
      }
    }
  },
  {
    type: 'function',
    function: {
      name: 'coach_review_week',
      description: '周复盘：从训练记录生成事实 → 判断 → 决策日志。最多只改 2 个变量，每项改动带 expected_effect / rollback_condition / review_date。记录不足时明确标 unknown，不制造趋势。',
      parameters: {
        type: 'object',
        properties: {
          records: {
            type: 'array',
            description: '训练记录。每条至少含 date / exercise / 一个数值（load_kg 或 reps）/ sets / rir',
            items: {
              type: 'object',
              properties: {
                date: { type: 'string', description: 'YYYY-MM-DD' },
                exercise: { type: 'string' },
                load_kg: { type: 'number' },
                reps: { type: 'number' },
                sets: { type: 'number' },
                rir: { type: 'number' },
                source_record_id: { type: 'string' }
              },
              required: ['date', 'exercise']
            }
          },
          completionRate: { type: 'number', description: '0-1，本周计划完成率' },
          dateStart: { type: 'string' },
          dateEnd: { type: 'string' },
          currentProgram: { type: 'object', description: '当前计划（含 version）' }
        },
        required: ['records']
      }
    }
  },
  {
    type: 'function',
    function: {
      name: 'coach_read_reference',
      description: '按需读取 skill 的知识文档。用户问"为什么""原理""动作怎么调""有氧怎么排""饮食怎么吃"时读取对应文件，再回答。只允许白名单内的 md 文件。',
      parameters: {
        type: 'object',
        properties: {
          name: {
            type: 'string',
            enum: [...REFERENCE_WHITELIST].sort(),
            description: 'references/ 下的文件名'
          }
        },
        required: ['name']
      }
    }
  }
];

function readReference(name) {
  if (!REFERENCE_WHITELIST.has(name)) {
    return { status: 'error', error: 'reference_not_allowed', allowed: [...REFERENCE_WHITELIST].sort().slice(0, 12) };
  }
  const target = path.join(coach.SKILL_ROOT, 'references', name);
  return { status: 'ok', name, content: fs.readFileSync(target, 'utf8') };
}

/** 把模型返回的 tool_call 分发到门面函数 */
function dispatch(name, args = {}) {
  switch (name) {
    case 'coach_safety_gate': return coach.safetyGate(args);
    case 'coach_plan_program': return coach.planProgram(args.profile || {}, args);
    case 'coach_today_session': return coach.todaySession(args);
    case 'coach_nutrition_targets': return coach.nutritionTargets(args);
    case 'coach_review_week': return coach.reviewWeek(args);
    case 'coach_read_reference': return readReference(args.name);
    default: return { status: 'error', error: `unknown_tool:${name}`, available: TOOLS.map((t) => t.function.name) };
  }
}

module.exports = { TOOLS, dispatch, readReference, REFERENCE_WHITELIST };
