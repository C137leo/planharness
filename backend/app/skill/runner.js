'use strict';

/**
 * PlanHarness → healthy-fitness-coach 的桥。
 *
 * 为什么用 Node 子进程而不是把 skill 逻辑搬进 Python：
 * skill 的唯一真源是 Node 写的那套确定性引擎（program-rules / safety-context /
 * readiness / nutrition）。复制一份到 Python 一定会在某次改动后与真源漂移，
 * 所以这里只做"协议转换"：stdin 收 JSON，stdout 出 JSON，逻辑一点都不重写。
 *
 * 用法：
 *   node runner.js <command> < args.json
 *   node runner.js tools            # 输出 function-calling schema（给模型看）
 *   node runner.js meta             # skill 版本 / 路径 / 可用命令
 *
 * 约定：业务错误也走 stdout 的 {status:"error"}，退出码保持 0；
 * 只有"连 skill 都加载不了"才 exit 1，方便 Python 侧区分。
 */

const fs = require('node:fs');
const path = require('node:path');

const COMMANDS = [
  'meta',
  'skill_md',
  'tools',
  'safety_gate',
  'screen_profile',
  'plan_program',
  'today_session',
  'nutrition_targets',
  'nutrition_plan',
  'review_week',
  'read_reference',
];

function resolveSkillRoot() {
  const candidates = [process.env.SKILL_DIR, process.env.SKILL_PATH, '/Users/leo/PycharmProjects/healthy-fitness-coach-skill']
    .filter(Boolean);
  for (const candidate of candidates) {
    const root = String(candidate);
    if (fs.existsSync(path.join(root, 'agent-integration', 'coach.js'))) return root;
    const nested = path.join(root, 'healthy-fitness-coach');
    if (fs.existsSync(path.join(nested, 'agent-integration', 'coach.js'))) return path.dirname(nested);
  }
  return null;
}

function readStdin() {
  try {
    const raw = fs.readFileSync(0, 'utf8');
    return raw.trim() ? JSON.parse(raw) : {};
  } catch (error) {
    return { __bad_input: String(error && error.message ? error.message : error) };
  }
}

function main() {
  const command = process.argv[2] || 'meta';
  const root = resolveSkillRoot();
  if (!root) {
    process.stdout.write(JSON.stringify({
      status: 'error',
      error: 'skill_not_found',
      hint: '设置 SKILL_DIR 指向 healthy-fitness-coach-skill 仓库根目录',
    }));
    process.exit(1);
    return;
  }

  const args = readStdin();
  let coach;
  let nutrition;
  try {
    coach = require(path.join(root, 'agent-integration', 'coach.js'));
    nutrition = require(path.join(root, 'web', 'lib', 'nutrition.js'));
  } catch (error) {
    process.stdout.write(JSON.stringify({
      status: 'error',
      error: 'skill_load_failed',
      detail: String(error && error.message ? error.message : error),
    }));
    process.exit(1);
    return;
  }

  let result;
  try {
    switch (command) {
      case 'meta': {
        const skillMd = fs.readFileSync(path.join(coach.SKILL_ROOT, 'SKILL.md'), 'utf8');
        result = {
          status: 'ok',
          root,
          skill_root: coach.SKILL_ROOT,
          skill_version: (skillMd.match(/version:\s*(\S+)/) || [])[1] || 'unversioned',
          commands: COMMANDS,
          node: process.version,
        };
        break;
      }
      case 'skill_md': {
        // 模型要看的入口说明（渐进披露的第 0 层），原样返回，不在 Python 侧抄一份
        const raw = fs.readFileSync(path.join(coach.SKILL_ROOT, 'SKILL.md'), 'utf8');
        result = { status: 'ok', name: 'SKILL.md', content: raw };
        break;
      }
      case 'tools': {
        // 直接拿 skill 自己维护的 function-calling schema，避免我们手抄一份
        const { TOOLS } = require(path.join(root, 'agent-integration', 'tools.js'));
        result = { status: 'ok', tools: TOOLS };
        break;
      }
      case 'safety_gate':
        result = coach.safetyGate(args);
        break;
      case 'screen_profile':
        result = nutrition.screenProfile(args);
        break;
      case 'plan_program':
        result = coach.planProgram(args.profile || {}, args);
        break;
      case 'today_session':
        result = coach.todaySession(args);
        break;
      case 'nutrition_targets':
        result = coach.nutritionTargets(args);
        break;
      case 'nutrition_plan':
        result = nutrition.nutritionPlan(args);
        break;
      case 'review_week':
        result = coach.reviewWeek(args);
        break;
      case 'read_reference': {
        const name = String(args.name || '');
        const safe = /^[a-z0-9._-]+\.md$/i.test(name);
        const target = path.join(coach.SKILL_ROOT, 'references', name);
        if (!safe || !fs.existsSync(target)) {
          result = { status: 'error', error: 'reference_not_allowed', name };
          break;
        }
        result = { status: 'ok', name, content: fs.readFileSync(target, 'utf8') };
        break;
      }
      default:
        result = { status: 'error', error: `unknown_command:${command}`, available: COMMANDS };
    }
  } catch (error) {
    result = {
      status: 'error',
      error: 'command_failed',
      command,
      detail: String(error && error.stack ? error.stack.split('\n')[0] : error),
    };
  }

  process.stdout.write(JSON.stringify(result));
}

main();
