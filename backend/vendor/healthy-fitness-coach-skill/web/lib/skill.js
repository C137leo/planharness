'use strict';

/**
 * skill 加载器：定位并加载 /Users/leo/PycharmProjects/healthy-fitness-coach-skill
 * 下的确定性引擎。所有 require 集中在这里，方便替换路径或做降级。
 */

const fs = require('node:fs');
const path = require('node:path');

const DEFAULT_ROOT = '/Users/leo/PycharmProjects/healthy-fitness-coach-skill';

function resolveSkillPath(input) {
  const candidates = [input, process.env.SKILL_PATH, DEFAULT_ROOT].filter(Boolean);
  for (const candidate of candidates) {
    const direct = String(candidate);
    if (fs.existsSync(path.join(direct, 'SKILL.md'))) return direct;
    const nested = path.join(direct, 'healthy-fitness-coach');
    if (fs.existsSync(path.join(nested, 'SKILL.md'))) return nested;
  }
  throw new Error(`找不到 skill 目录（需要含 SKILL.md）。已尝试: ${candidates.join(' | ')}`);
}

const SKILL_ROOT = resolveSkillPath();
const ref = (name) => require(path.join(SKILL_ROOT, 'references', name));

const engines = {
  orchestrator: ref('training-orchestrator.js'),
  programRules: ref('program-rules.js'),
  safety: ref('safety-context.js'),
  readiness: ref('readiness-engine.js'),
  guidance: ref('training-guidance.js'),
  review: ref('review-decision-engine.js'),
  athlete: ref('athlete-state.js'),
  workoutGuide: ref('workout-guide.js'),
  routing: ref('output-routing.js')
};

const SKILL_MD = fs.readFileSync(path.join(SKILL_ROOT, 'SKILL.md'), 'utf8');
const SKILL_VERSION = (SKILL_MD.match(/version:\s*(\S+)/) || [])[1] || 'unversioned';

module.exports = { SKILL_ROOT, SKILL_MD, SKILL_VERSION, engines, ref };
