"""意图识别评估用例：只测"理解"，不执行任何业务写操作。

这不是提示词里的 few-shot，而是回归测试集：模型/提示词/路由改动后跑一遍，
看 intent、scope、关键 slots 是否仍然正确。当前是 10 条基础集，后续按同一格式
扩充到 100–200 条不同表达。
"""

from __future__ import annotations

BASE_CONTEXT = {
    "page": "workout",
    "has_plan": True,
    "in_workout": True,
    "plan_version": 3,
    "today_plan": {
        "summary": "今天的训练：高脚杯深蹲、单臂哑铃划船",
        "source": "plan",
        "actions": [
            {"index": 1, "slot": "warmup", "name": "动态热身", "ref": "dynamic_warmup",
             "sets": 1, "reps": None, "minutes": 5},
            {"index": 2, "slot": "strength", "name": "高脚杯深蹲", "ref": "goblet-squat",
             "sets": 2, "reps": 10, "minutes": 8},
            {"index": 3, "slot": "strength", "name": "单臂哑铃划船",
             "ref": "one-arm-dumbbell-row", "sets": 2, "reps": 10, "minutes": 8},
        ],
    },
    "current_workout": {
        "status": "active",
        "current_index": 2,
        "total": 3,
        "current": {"name": "高脚杯深蹲", "ref": "goblet-squat", "sets": 2, "reps": 10},
        "items": [
            {"index": 1, "name": "动态热身", "ref": "dynamic_warmup", "status": "done",
             "sets": 1, "completed_sets": 1},
            {"index": 2, "name": "高脚杯深蹲", "ref": "goblet-squat", "status": "active",
             "sets": 2, "completed_sets": 0},
            {"index": 3, "name": "单臂哑铃划船", "ref": "one-arm-dumbbell-row",
             "status": "pending", "sets": 2, "completed_sets": 0},
        ],
    },
    "profile": {
        "goal": "fat_loss", "weekly_days": 3, "session_minutes": 20,
        "equipment": ["dumbbell", "band", "mat"], "preferred_location": "home",
        "preferred_time_window": "19:00-20:00", "experience": "beginner",
    },
    "recent": [],
}


def _ctx(*, in_workout: bool = True, page: str = "workout", recent=None) -> dict:
    import copy

    ctx = copy.deepcopy(BASE_CONTEXT)
    ctx["in_workout"] = in_workout
    ctx["page"] = page
    if not in_workout:
        ctx["current_workout"] = None
    if recent:
        ctx["recent"] = recent
    return ctx


CASES: list[dict] = [
    {
        "id": "T01_early_end",
        "text": "今天练得太累了，剩下的别安排了。",
        "context": _ctx(),
        "expected_intents": ["adjust_workout", "adjust_today"],
        "expected_scope": ["current_workout", "today"],
        "expected_slots": ["rest", "easier"],
    },
    {
        "id": "T02_minutes",
        "text": "我今天只有二十分钟，给我重新安排一下。",
        "context": _ctx(in_workout=False, page="today"),
        "expected_intents": ["adjust_today"],
        "expected_scope": ["today"],
        "expected_slots": ["minutes"],
        "slot_expect": {"minutes": 20},
    },
    {
        "id": "T03_equipment_busy",
        "text": "腿举机有人用，换个动作吧。",
        "context": _ctx(),
        "expected_intents": ["adjust_workout", "adjust_today"],
        "expected_scope": ["current_workout", "today"],
        "expected_slots": ["equipment_busy", "avoid_actions"],
    },
    {
        "id": "T04_long_term_dislike",
        "text": "以后尽量别给我安排深蹲了。",
        "context": _ctx(in_workout=False, page="today"),
        "expected_intents": ["update_preference", "adjust_plan"],
        "expected_scope": ["long_term"],
        "expected_slots": [],
    },
    {
        "id": "T05_back_discomfort",
        "text": "我最近腰总是不得劲，今天还能练吗？",
        "context": _ctx(in_workout=False, page="today"),
        "expected_intents": ["injury_advice", "adjust_today"],
        "expected_scope": ["today"],
        "expected_slots": ["injured_regions"],
        "slot_expect": {"injured_regions": ["lower_back"]},
    },
    {
        "id": "T06_more_load",
        "text": "今天状态不错，再给我加点量。",
        "context": _ctx(in_workout=False, page="today"),
        "expected_intents": ["adjust_today", "adjust_workout"],
        "expected_scope": ["today", "current_workout"],
        "expected_slots": ["harder"],
    },
    {
        "id": "T07_context_reference",
        "text": "刚才那个动作我做不标准，教我一下。",
        "context": _ctx(),
        "expected_intents": ["exercise_guidance"],
        "expected_scope": ["message", "current_workout"],
        "expected_slots": ["exercise"],
    },
    {
        "id": "T08_home_only",
        "text": "以后我就在家练了，别再给我安排健身房器械。",
        "context": _ctx(in_workout=False, page="today"),
        "expected_intents": ["update_preference"],
        "expected_scope": ["long_term"],
        "expected_slots": [],
    },
    {
        "id": "T09_burger_nutrition",
        "text": "吃个牛肉汉堡会不会影响今天的增肌计划？",
        "context": _ctx(in_workout=False, page="today"),
        "expected_intents": ["nutrition_advice"],
        "expected_scope": ["message", "today"],
        "expected_slots": [],
    },
    {
        "id": "T10_followup",
        "text": "就按刚才说的来吧。",
        "context": _ctx(
            in_workout=False, page="today",
            recent=[
                {"role": "user", "text": "根据我的档案和最近训练，给我一些建议"},
                {"role": "agent",
                 "text": "建议这周先把训练量降一档，今天做 20 分钟最小训练。"},
            ],
        ),
        "expected_intents": ["review_followup", "adjust_today"],
        "expected_scope": ["today"],
        "expected_slots": [],
    },
]


# ---------------------------------------------------------------- 扩充语料
# 说明：这是同一套评估格式的扩展集，目标是覆盖不同表达方式，而不是把句子塞进
# 生产 Prompt。每条只断言"意图/范围/关键槽位"，不执行任何业务写操作。

def _extra(case_id: str, text: str, intents: list[str], scopes: list[str],
           slots: dict | None = None, ctx: str = "today") -> dict:
    if ctx == "workout":
        context = _ctx()
    elif ctx == "followup":
        context = _ctx(
            in_workout=False, page="today",
            recent=[
                {"role": "user", "text": "根据我的档案和最近训练，给我一些建议"},
                {"role": "agent",
                 "text": "建议这周先把训练量降一档，今天做 20 分钟最小训练。"},
            ],
        )
    else:
        context = _ctx(in_workout=False, page="today")
    return {
        "id": case_id, "text": text, "context": context,
        "expected_intents": intents, "expected_scope": scopes,
        "expected_slots": list((slots or {}).keys()),
        "slot_expect": slots or {},
    }


EXTRA_CASES = [
    # 今天：时间 / 地点 / 强度 / 休息
    _extra("T11", "今天只有半小时，帮我压缩一下。", ["adjust_today"], ["today"],
           {"minutes": 30}),
    _extra("T12", "今天在家练，不去健身房。", ["adjust_today"], ["today"],
           {"location": "home"}),
    _extra("T13", "外面下雨了，今天改室内训练。", ["adjust_today"], ["today"],
           {"location": "home"}),
    _extra("T14", "今天有点累，练轻一点。", ["adjust_today"], ["today"],
           {"easier": True}),
    _extra("T15", "今天状态好，多练一组。", ["adjust_today"], ["today"],
           {"harder": True}),
    _extra("T16", "今天想休息，不训练了。", ["adjust_today"], ["today"],
           {"rest": True}),
    _extra("T17", "今天不想练腿，改成上半身。", ["adjust_today"], ["today"]),
    _extra("T18", "今天只有二十分钟，别安排太多。", ["adjust_today"], ["today"],
           {"minutes": 20}),
    _extra("T19", "今晚要加班，训练缩短到十五分钟。", ["adjust_today"], ["today"],
           {"minutes": 15}),
    _extra("T20", "今天在办公室，只有弹力带。", ["adjust_today"], ["today"],
           {"location": "office"}),
    _extra("T21", "今天健身房关门了，在家练。", ["adjust_today"], ["today"],
           {"location": "home"}),
    _extra("T22", "今天不想出门，居家训练。", ["adjust_today"], ["today"],
           {"location": "home"}),
    _extra("T23", "今天想练轻松点，别太累。", ["adjust_today"], ["today"],
           {"easier": True}),
    _extra("T24", "今天体力不错，加一点强度。", ["adjust_today"], ["today"],
           {"harder": True}),
    _extra("T25", "今天不练了，改成拉伸。", ["adjust_today"], ["today"],
           {"rest": True}),

    # 训练中：换动作 / 组数 / 结束 / 指代
    _extra("T26", "这个动作太难了，换简单点。", ["adjust_workout"], ["current_workout"],
           {"easier": True}, ctx="workout"),
    _extra("T27", "器械被占了，换一个。", ["adjust_workout"], ["current_workout"],
           {"equipment_busy": True}, ctx="workout"),
    _extra("T28", "膝盖有点疼，换动作。", ["adjust_workout", "injury_advice"],
           ["current_workout", "today"], ctx="workout"),
    _extra("T29", "太累了，剩下的不练了。", ["adjust_workout"], ["current_workout"],
           {"rest": True}, ctx="workout"),
    _extra("T30", "还能再练两组。", ["adjust_workout"], ["current_workout"],
           {"harder": True}, ctx="workout"),
    _extra("T31", "减一组吧。", ["adjust_workout"], ["current_workout"],
           {"easier": True}, ctx="workout"),
    _extra("T32", "这个动作怎么做？", ["exercise_guidance"], ["current_workout"],
           ctx="workout"),
    _extra("T33", "第一个换掉。", ["adjust_workout"], ["current_workout"],
           {"exercise": "动态热身"}, ctx="workout"),
    _extra("T34", "第二个动作换一个。", ["adjust_workout"], ["current_workout"],
           {"exercise": "高脚杯深蹲"}, ctx="workout"),
    _extra("T35", "跳过这个动作。", ["adjust_workout"], ["current_workout"], ctx="workout"),
    _extra("T36", "今天练到这里。", ["adjust_workout"], ["current_workout"],
           {"rest": True}, ctx="workout"),
    _extra("T37", "我状态不错，加量。", ["adjust_workout"], ["current_workout"],
           {"harder": True}, ctx="workout"),
    _extra("T38", "有点吃力，降一点。", ["adjust_workout"], ["current_workout"],
           {"easier": True}, ctx="workout"),
    _extra("T39", "哑铃被占了。", ["adjust_workout"], ["current_workout"],
           {"equipment_busy": True}, ctx="workout"),
    _extra("T40", "腿举机有人用。", ["adjust_workout"], ["current_workout"],
           {"equipment_busy": True}, ctx="workout"),

    # 伤病：部位 + 疼痛
    _extra("T41", "膝盖疼。", ["injury_advice"], ["today"],
           {"injured_regions": ["knee"]}),
    _extra("T42", "腰疼。", ["injury_advice"], ["today"],
           {"injured_regions": ["lower_back"]}),
    _extra("T43", "肩膀不舒服。", ["injury_advice"], ["today"],
           {"injured_regions": ["shoulder"]}),
    _extra("T44", "手腕疼。", ["injury_advice"], ["today"],
           {"injured_regions": ["wrist"]}),
    _extra("T45", "脚踝扭了。", ["injury_advice"], ["today"],
           {"injured_regions": ["ankle"]}),
    _extra("T46", "脖子不舒服。", ["injury_advice"], ["today"],
           {"injured_regions": ["neck"]}),
    _extra("T47", "髋部疼。", ["injury_advice"], ["today"],
           {"injured_regions": ["hip"]}),
    _extra("T48", "深蹲时膝盖疼。", ["injury_advice"], ["today"],
           {"injured_regions": ["knee"]}),
    _extra("T49", "跑步后膝盖不舒服。", ["injury_advice"], ["today"],
           {"injured_regions": ["knee"]}),
    _extra("T50", "腰最近总是酸。", ["injury_advice"], ["today"],
           {"injured_regions": ["lower_back"]}),
    _extra("T51", "肩膀抬不起来。", ["injury_advice"], ["today"],
           {"injured_regions": ["shoulder"]}),
    _extra("T52", "脚踝肿了。", ["injury_advice"], ["today"],
           {"injured_regions": ["ankle"]}),

    # 长期偏好
    _extra("T53", "以后我想增肌。", ["update_preference"], ["long_term"],
           {"goal": "muscle_gain"}),
    _extra("T54", "我不想减脂了，改增肌。", ["update_preference"], ["long_term"],
           {"goal": "muscle_gain"}),
    _extra("T55", "以后都在家练。", ["update_preference"], ["long_term"],
           {"location": "home"}),
    _extra("T56", "以后去健身房练。", ["update_preference"], ["long_term"],
           {"location": "gym"}),
    _extra("T57", "以后训练改到早上。", ["update_preference"], ["long_term"]),
    _extra("T58", "以后每周练四天。", ["update_preference", "adjust_plan"],
           ["long_term"], {"weekly_days": 4}),
    _extra("T59", "以后每次练三十分钟。", ["update_preference"], ["long_term"],
           {"session_minutes": 30}),
    _extra("T60", "以后别安排深蹲。", ["update_preference", "adjust_plan"],
           ["long_term"]),
    _extra("T61", "我不喜欢波比。", ["update_preference"], ["long_term"],
           {"dislikes": ["波比"]}),
    _extra("T62", "以后多做划船。", ["update_preference"], ["long_term"],
           {"likes": ["划船"]}),
    _extra("T63", "我体重80公斤。", ["update_preference"], ["long_term"],
           {"weight_kg": 80}),
    _extra("T64", "我身高180。", ["update_preference"], ["long_term"],
           {"height_cm": 180}),
    _extra("T65", "我1995年出生。", ["update_preference"], ["long_term"],
           {"birth_year": 1995}),
    _extra("T66", "我是男生。", ["update_preference"], ["long_term"]),
    _extra("T67", "以后都在晚上八点练。", ["update_preference"], ["long_term"]),
    _extra("T68", "我只有哑铃和弹力带。", ["update_preference"], ["long_term"]),
    _extra("T69", "我零基础。", ["update_preference"], ["long_term"]),

    # 长期计划
    _extra("T70", "把每周训练改成四天。", ["adjust_plan"], ["long_term"]),
    _extra("T71", "下周少练一天。", ["adjust_plan"], ["long_term"]),
    _extra("T72", "以后计划里不要深蹲。", ["adjust_plan", "update_preference"],
           ["long_term"]),
    _extra("T73", "帮我把计划改成三天。", ["adjust_plan"], ["long_term"]),
    _extra("T74", "这个计划太累了，降一档。", ["adjust_plan"], ["long_term"]),
    _extra("T75", "下周我想练背。", ["adjust_plan"], ["long_term"]),
    _extra("T76", "把腿举换成腿弯举。", ["adjust_plan"], ["long_term"]),
    _extra("T77", "计划里加一个核心动作。", ["adjust_plan"], ["long_term"]),

    # 建立计划
    _extra("T78", "帮我制定一个减脂计划。", ["create_plan"], ["long_term"]),
    _extra("T79", "重新生成一份计划。", ["create_plan"], ["long_term"]),
    _extra("T80", "我想开始健身。", ["create_plan"], ["long_term"]),
    _extra("T81", "按我的档案排一份计划。", ["create_plan"], ["long_term"]),
    _extra("T82", "换一份计划。", ["create_plan"], ["long_term"]),

    # 饮食
    _extra("T83", "中午吃黄焖鸡，晚上怎么吃？", ["nutrition_advice"], ["message"]),
    _extra("T84", "牛肉汉堡会影响减脂吗？", ["nutrition_advice"], ["message"]),
    _extra("T85", "今天蛋白吃够了吗？", ["nutrition_advice"], ["message"]),
    _extra("T86", "推荐一个高蛋白晚餐。", ["nutrition_advice"], ["message"]),
    _extra("T87", "外卖怎么点更健康？", ["nutrition_advice"], ["message"]),
    _extra("T88", "奶茶能喝吗？", ["nutrition_advice"], ["message"]),
    _extra("T89", "训练后吃什么？", ["nutrition_advice"], ["message"]),
    _extra("T90", "今天热量还剩多少？", ["nutrition_advice"], ["message"]),
    _extra("T91", "增肌需要多少蛋白？", ["nutrition_advice"], ["message"]),

    # 记忆
    _extra("T92", "你记得我的目标吗？", ["query_memory"], ["message"]),
    _extra("T93", "我的档案是什么？", ["query_memory"], ["message"]),
    _extra("T94", "你记得我不喜欢什么吗？", ["query_memory"], ["message"]),
    _extra("T95", "忘掉我喜欢跑步。", ["edit_memory"], ["long_term"]),
    _extra("T96", "删除我的训练时间记忆。", ["edit_memory"], ["long_term"]),

    # 周安排 / 进展 / 复盘
    _extra("T97", "把每周安排整理给我。", ["plan_overview"], ["long_term"]),
    _extra("T98", "整周怎么练？", ["plan_overview"], ["long_term"]),
    _extra("T99", "我最近完成率怎么样？", ["progress_review"], ["message"]),
    _extra("T100", "看我的训练记录。", ["progress_review"], ["message"]),
    _extra("T101", "根据最近训练给我建议。", ["coach_review"], ["long_term"]),
    _extra("T102", "我练得怎么样？", ["coach_review", "progress_review"],
           ["long_term", "message"]),
    _extra("T103", "给我一个下周建议。", ["coach_review"], ["long_term"]),

    # 多轮 followup
    _extra("T104", "就按刚才说的来。", ["review_followup"], ["today"], ctx="followup"),
    _extra("T105", "好的，开始吧。", ["review_followup", "adjust_today"], ["today"],
           ctx="followup"),
    _extra("T106", "按你说的办。", ["review_followup", "adjust_today"], ["today"],
           ctx="followup"),

    # 点餐
    _extra("T107", "帮我点麦当劳，北京三里屯。", ["mcd_order"], ["meal"]),
    _extra("T108", "我想吃汉堡外卖，送到国贸。", ["mcd_order"], ["meal"]),
    _extra("T109", "点一份炸鸡。", ["mcd_order"], ["meal"]),

    # 模糊 / 无关
    _extra("T110", "随便。", ["clarify"], ["none"]),
    _extra("T111", "你猜。", ["clarify"], ["none"]),
    _extra("T112", "今天天气不错。", ["clarify"], ["none"]),
]

CASES.extend(EXTRA_CASES)
