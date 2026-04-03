import ast
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx
import asyncio
import time
import uuid
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv


APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")

# Ensure .env always takes effect.
load_dotenv(os.path.join(APP_DIR, ".env"), override=True)


def _ai_http_read_timeout_s(attempt: int) -> float:
    """
    单次调用上游 LLM 的 HTTP 读超时（秒）。生成较长 JSON 或排队时经常 >30s；
    过短会导致反复 ReadTimeout 重试，前端长时间无结果。
    """
    base = float((os.environ.get("AI_READ_TIMEOUT") or "120").strip() or "120")
    step = float((os.environ.get("AI_READ_TIMEOUT_STEP") or "40").strip() or "40")
    cap = float((os.environ.get("AI_READ_TIMEOUT_MAX") or "240").strip() or "240")
    return min(cap, base + max(0, attempt - 1) * step)


def _use_llm_stream() -> bool:
    """默认关闭流式：整段 JSON 一次返回，经隧道/代理更稳；需要流式可设 DOUBAO_STREAM=1。"""
    v = (os.environ.get("DOUBAO_STREAM") or "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def _max_tokens_diagnose() -> int:
    return max(256, int((os.environ.get("AI_MAX_TOKENS_DIAGNOSE") or "768").strip() or "768"))


def _max_tokens_advise() -> int:
    return max(256, int((os.environ.get("AI_MAX_TOKENS_ADVISE") or "768").strip() or "768"))


app = FastAPI(title="Crucial Talk Coach MVP")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# In-memory job store (single-process MVP)
JOBS: Dict[str, Dict[str, Any]] = {}

@app.get("/api/health")
def health() -> JSONResponse:
    # Do NOT return secrets; only booleans and selected non-sensitive values.
    base_url = (os.environ.get("DOUBAO_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or "").strip()
    model = (os.environ.get("DOUBAO_MODEL") or os.environ.get("OPENAI_MODEL") or "").strip()
    return JSONResponse(
        content={
            "ok": True,
            "has_doubao_api_key": bool((os.environ.get("DOUBAO_API_KEY") or "").strip()),
            "has_openai_api_key": bool((os.environ.get("OPENAI_API_KEY") or "").strip()),
            "base_url": base_url,
            "model": model,
        }
    )


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> JSONResponse:
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse(status_code=404, content={"error": "not_found"})
    # Never return secrets
    return JSONResponse(content=job)


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


def _require_fields(d: Dict[str, Any], keys: List[str]) -> List[str]:
    missing: List[str] = []
    for k in keys:
        if not _nonempty(str(d.get(k, ""))):
            missing.append(k)
    return missing


def _clamp(s: Optional[str], max_len: int) -> str:
    if not s:
        return ""
    s = s.strip()
    if len(s) > max_len:
        return s[:max_len]
    return s


def _compact_text(s: str, max_len: int) -> str:
    s = (s or "").strip()
    if len(s) <= max_len:
        return s
    return s[:max_len]


def _path_side_from_ai(obj: Any) -> Dict[str, str]:
    """facts / story / emotion / behavior / emotion_why — 双方行为路径拆解。"""
    if not isinstance(obj, dict):
        return {}
    out: Dict[str, str] = {}
    for k in ("facts", "story", "emotion", "behavior", "emotion_why"):
        v = obj.get(k)
        out[k] = str(v).strip() if v is not None else ""
    return out


_PATH_KEY_ALIASES = {
    "事实": "facts",
    "故事": "story",
    "情绪": "emotion",
    "行为": "behavior",
    "情绪成因": "emotion_why",
    "原因": "emotion_why",
}


def _maybe_json_dict(val: Any) -> Optional[Dict[str, Any]]:
    if isinstance(val, dict):
        return val
    if isinstance(val, str) and val.strip().startswith("{"):
        try:
            o = json.loads(val)
            return o if isinstance(o, dict) else None
        except Exception:
            return None
    return None


def _normalize_path_dict(obj: Any) -> Dict[str, str]:
    raw = _maybe_json_dict(obj)
    if not raw:
        return {}
    merged: Dict[str, Any] = dict(raw)
    for cn, en in _PATH_KEY_ALIASES.items():
        if cn in merged and en not in merged:
            merged[en] = merged[cn]
    return _path_side_from_ai(merged)


def _normalize_diagnosis_raw(j: Dict[str, Any]) -> Dict[str, Any]:
    """模型常用中文键名或嵌套字段名，归一成解析逻辑可读的形态。"""
    out = dict(j)
    # 部分接口把整段 JSON 包在单键里（如 {"result": "{...}"}）
    if len(out) == 1:
        v = next(iter(out.values()))
        inner = _maybe_json_dict(v) if isinstance(v, str) else None
        if inner:
            out = dict(inner)
    if not str(out.get("emotion_why_both") or "").strip():
        for k in ("双方情绪成因", "情绪成因综合", "双方情绪原因", "情绪成因"):
            v = out.get(k)
            if v and str(v).strip():
                out["emotion_why_both"] = str(v).strip()
                break
    if not str(out.get("common_goal") or "").strip() and out.get("共同目标"):
        out["common_goal"] = str(out["共同目标"]).strip()
    if not out.get("path_you"):
        for k in ("你方", "你的路径", "pathYou"):
            if out.get(k) is not None:
                out["path_you"] = out[k]
                break
    if not out.get("path_them"):
        for k in ("对方路径", "pathThem"):
            if out.get(k) is not None:
                out["path_them"] = out[k]
                break
    if not out.get("path_them") and isinstance(out.get("对方"), dict):
        out["path_them"] = out["对方"]
    # 顶层 CPR 中文扁平字段
    if not isinstance(out.get("cpr"), dict) and not (out.get("cpr_c") or out.get("cpr_p") or out.get("cpr_r")):
        if any(out.get(k) for k in ("内容", "模式", "关系")):
            out["cpr"] = {
                "内容": str(out.get("内容") or ""),
                "模式": str(out.get("模式") or ""),
                "关系": str(out.get("关系") or ""),
            }
    if not str(out.get("story_clean") or "").strip() and out.get("故事改写"):
        out["story_clean"] = str(out["故事改写"]).strip()
    return out


def _cpr_from_ai_json(j: Dict[str, Any]) -> Dict[str, str]:
    c = j.get("cpr")
    if isinstance(c, dict):
        cc = dict(c)
        if not (cc.get("content") or cc.get("c")):
            for k in ("内容", "议题"):
                if cc.get(k):
                    cc["content"] = cc[k]
                    break
        if not (cc.get("pattern") or cc.get("p")):
            for k in ("模式", "互动模式"):
                if cc.get(k):
                    cc["pattern"] = cc[k]
                    break
        if not (cc.get("relationship") or cc.get("r")):
            for k in ("关系", "信任"):
                if cc.get(k):
                    cc["relationship"] = cc[k]
                    break
        return {
            "content": str(cc.get("content") or cc.get("c") or "").strip(),
            "pattern": str(cc.get("pattern") or cc.get("p") or "").strip(),
            "relationship": str(cc.get("relationship") or cc.get("r") or "").strip(),
        }
    if isinstance(c, str) and c.strip():
        return {"content": c.strip(), "pattern": "", "relationship": ""}
    return {
        "content": str(j.get("cpr_c", j.get("内容", ""))).strip(),
        "pattern": str(j.get("cpr_p", j.get("模式", ""))).strip(),
        "relationship": str(j.get("cpr_r", j.get("关系", ""))).strip(),
    }


def _key_facts_from_ai(j: Dict[str, Any]) -> List[str]:
    kf = j.get("key_facts") or j.get("关键事实")
    if isinstance(kf, list):
        return [str(x).strip() for x in kf if str(x).strip()]
    if isinstance(kf, str) and kf.strip():
        return [kf.strip()]
    k1 = j.get("key_fact")
    if k1:
        return [str(k1).strip()]
    return []


def _behavior_path_mono(path: Dict[str, str]) -> str:
    order = (
        ("facts", "事实"),
        ("story", "故事"),
        ("emotion", "情绪"),
        ("behavior", "行为"),
        ("emotion_why", "情绪成因"),
    )
    lines: List[str] = []
    for k, lab in order:
        v = (path or {}).get(k, "")
        if v:
            lines.append(f"{lab}：{v}")
    return "\n".join(lines)


def _path_has_content(path: Dict[str, str]) -> bool:
    return bool(path and any((path.get(k) or "").strip() for k in ("facts", "story", "emotion", "behavior", "emotion_why")))


def _nonempty(s: str) -> bool:
    return bool(s and s.strip())


def _extract(payload: Dict[str, Any]) -> Dict[str, Any]:
    # Required-ish fields (frontend enforces, backend double-checks)
    data = {
        "relationship": _clamp(payload.get("relationship"), 30),
        "channel": _clamp(payload.get("channel"), 20),
        "counterpart": _clamp(payload.get("counterpart"), 40),
        "goal": _clamp(payload.get("goal"), 120),
        "fear": _clamp(payload.get("fear"), 120),
        "situation": _clamp(payload.get("situation"), 500),
        "task": _clamp(payload.get("task"), 350),
        "action": _clamp(payload.get("action"), 450),
        "result": _clamp(payload.get("result"), 350),
        # Optional enhancers
        "last_dialogue_you": _clamp(payload.get("last_dialogue_you"), 280),
        "last_dialogue_them": _clamp(payload.get("last_dialogue_them"), 280),
        "your_story": _clamp(payload.get("your_story"), 220),
        "constraints": _clamp(payload.get("constraints"), 160),
    }
    return data


def _is_crucial(d: Dict[str, Any]) -> bool:
    # Heuristic: fear present OR stakeholder/relationship indicates risk OR clear conflict markers
    if _nonempty(d.get("fear", "")):
        return True
    if d.get("relationship") in {"上级", "客户"} and _nonempty(d.get("goal", "")):
        return True
    # If STAR has any conflict indicators in Chinese
    text = " ".join([d.get("situation", ""), d.get("task", ""), d.get("action", ""), d.get("result", "")])
    conflict_markers = ["冲突", "争执", "对立", "不满", "抱怨", "质疑", "指责", "失控", "尴尬", "冷战", "沉默", "威胁"]
    return any(m in text for m in conflict_markers)

def _split_sentences(text: str) -> List[str]:
    t = (text or "").strip()
    if not t:
        return []
    parts = re.split(r"[。\n；;!?！？]+", t)
    return [p.strip(" ，,。；;") for p in parts if p and p.strip()]


def _pick_facts(d: Dict[str, Any], limit: int = 3) -> List[str]:
    # Very simple: treat STAR S/A/R as candidate factual lines and pick short, specific ones.
    candidates: List[str] = []
    for key in ["situation", "action", "result"]:
        candidates.extend(_split_sentences(d.get(key, "")))

    # De-duplicate while preserving order
    seen = set()
    uniq: List[str] = []
    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        uniq.append(c)

    def score(s: str) -> int:
        # Prefer lines containing time/quantity or concrete nouns; penalize "觉得/感觉/故意/总是"
        bad = ["觉得", "感觉", "故意", "总是", "从不", "明显", "就是", "针对"]
        good = ["周", "月", "日", "次", "分钟", "会议", "邮件", "IM", "上线", "交付", "变更", "排期", "资源", "范围"]
        sc = 0
        sc += 2 if any(g in s for g in good) else 0
        sc -= 2 if any(b in s for b in bad) else 0
        sc += 1 if len(s) <= 40 else 0
        return sc

    ranked = sorted(uniq, key=score, reverse=True)
    facts = [x for x in ranked if x]
    return facts[:limit]


def _detect_triggers(text: str) -> List[str]:
    t = text or ""
    triggers = []
    patterns = [
        ("绝对化", r"(总是|从不|永远|根本|一直)"),
        ("指责/归因", r"(就是你|你必须|你怎么|你又|你才)"),
        ("贴标签", r"(无能|不专业|不负责|甩锅|垃圾)"),
        ("读心/动机推断", r"(故意|针对|不在乎|不想|想甩锅)"),
    ]
    for label, pat in patterns:
        m = re.search(pat, t)
        if m:
            triggers.append(f"{label}：命中“{m.group(1)}”")
    return triggers


def _rewrite_blame(sentence: str, facts: List[str], goal: str) -> List[str]:
    s = (sentence or "").strip().strip("“”\"'")
    if not s:
        return []
    f1 = facts[0] if facts else "（补一条可验证事实）"
    g = goal or "把问题谈清楚并决定下一步"
    return [
        f"原句：{s}",
        f"改写（更安全）：我想先对齐一下事实。我这边看到的是：{f1}。这让我有点担心会影响{g}。我想听听你这边看到的情况是什么？",
        f"改写（更直接但不指责）：关于{g}，我观察到：{f1}。我们能不能先把变更/排期的规则对齐一下，再决定后续怎么处理？",
    ]


def _diagnose(d: Dict[str, Any]) -> Dict[str, Any]:
    txt = " ".join(
        [
            d.get("action", ""),
            d.get("result", ""),
            d.get("last_dialogue_you", ""),
            d.get("last_dialogue_them", ""),
        ]
    )
    triggers = _detect_triggers(txt + " " + d.get("your_story", ""))
    facts = _pick_facts(d, limit=3)

    # Qualitative primary focus
    primary = "先修复安全感（先把‘目的/尊重’说清楚）" if triggers else "先澄清事实与彼此关注点（避免陷入动机争论）"
    if d.get("relationship") in {"上级", "客户"}:
        primary = "先修复安全感 + 先对齐共同目的（权力/利益相关更容易触发防御）"

    their_possible_story: List[str] = []
    if _nonempty(d.get("last_dialogue_them", "")):
        their_possible_story.append(f"对方原话里可能在表达：{d['last_dialogue_them']}")
    if "排期" in txt or "不合理" in txt:
        their_possible_story.append("对方可能担心：你在用‘排期/规则’压他承担风险。")
    if "背锅" in txt:
        their_possible_story.append("对方可能担心：被归因、背责任或被贴标签。")
    if not their_possible_story:
        their_possible_story = ["对方可能担心：被指责/被要求承担不对等成本；或担心问题被简化成‘谁的错’。"]

    your_story_clean = d.get("your_story", "")
    if _nonempty(your_story_clean):
        # Light de-intensify
        your_story_clean = re.sub(r"(就是|永远|从不|总是)", "可能", your_story_clean)

    return {
        "is_crucial": _is_crucial(d),
        "primary_focus": primary,
        "facts": facts,
        "triggers": triggers,
        "your_story_clean": your_story_clean,
        "their_possible_story": their_possible_story,
    }


def _fast_problem_analysis(d: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deterministic <30s output using Crucial Conversations concepts,
    used as guaranteed "quick" result when AI may time out.
    """
    diag = _diagnose(d)
    rel = d.get("relationship") or "对方"
    who = d.get("counterpart") or rel
    goal = d.get("goal") or "把问题谈清楚并决定下一步"
    fear = d.get("fear") or "关系变僵/事情谈崩"
    facts = diag.get("facts") or []
    triggers = diag.get("triggers") or []
    their_story = (diag.get("their_possible_story") or ["对方可能担心：被指责/背责任。"])[0]

    c_content = f"分歧集中在：如何在{goal}的同时处理变更/规则/责任边界。"
    c_pattern = "你用评价/绝对化提法（如“总是/不能这样搞”）→对方反击/辩护→升级对抗→无结论。"
    c_rel = f"你担心{fear}，对方可能担心被归因或背锅，彼此安全感下降。"

    you_path = "更偏‘暴力-控制/指责’（如果出现“总是/不能这样搞/甩锅”等）；建议先降火到事实+担心。"
    them_path = f"更偏‘防御-反击/辩护’（如强调“排期不合理/不背锅”）。"
    if not triggers:
        you_path = "表达相对克制，但容易跳到结论；建议把事实写清楚并邀请对方补充。"

    common_goal = f"我们共同目标是：{goal}，同时避免{fear}（对事不对人）。"

    return {
        "is_crucial": diag.get("is_crucial", True),
        "cpr": {"content": c_content, "pattern": c_pattern, "relationship": c_rel},
        "behavior_path": {"you": you_path, "them": them_path},
        "common_goal": common_goal,
        "key_facts": facts[:1] if facts else [],
        "your_story_clean": diag.get("your_story_clean") or "我担心我们在用不同标准理解问题，继续下去会影响结果。",
        "their_story_hint": their_story,
    }


def _fast_comms_advice(d: Dict[str, Any], diagnosis: Dict[str, Any]) -> Dict[str, Any]:
    goal = d.get("goal") or "把问题谈清楚并决定下一步"
    fear = d.get("fear") or "关系变僵/事情谈崩"
    rel = d.get("relationship") or "对方"
    who = d.get("counterpart") or rel
    facts = _pick_facts(d, limit=2)
    f1 = facts[0] if facts else "（补一条可验证事实）"
    them = (d.get("last_dialogue_them") or "").strip()
    common_goal = (diagnosis or {}).get("common_goal") or f"目标是{goal}，同时避免{fear}"

    opening = f"{who}，我想把这件事谈清楚，目标是{goal}。我不是想指责你；我想先对齐事实，再一起定下一步，避免{fear}。"
    state = [
        f"【事实】我看到的是：{f1}。",
        f"【影响/担心】这让我担心会影响{goal}。",
        "【邀请】你这边看到的关键事实是什么？你最担心的点是什么？",
    ]
    ampp = [
        f"我听到你在强调“{them[:18]}{'…' if len(them)>18 else ''}”，你最担心的是什么？" if them else "你最担心的风险是什么？",
        "如果我们先不争论谁对谁错，只谈怎么达成目标，你觉得第一步该对齐什么？",
    ]
    close = [
        "我们先把三件事写下来：结论/范围（WHAT）、负责人（WHO）、时间点（WHEN）。",
        "如果后续再有变更，按约定的规则走：提出人→影响评估→双方确认后再进入范围。",
    ]
    return {"opening": opening, "state": state, "ampp": ampp, "close": close, "common_goal": common_goal}


def _script(d: Dict[str, Any], diag: Dict[str, Any]) -> Dict[str, Any]:
    goal = d.get("goal") or "把关键问题谈清楚"
    fear = d.get("fear") or "把关系弄僵/把事情谈崩"
    rel = d.get("relationship") or "对方"
    counterpart = d.get("counterpart") or rel
    constraints = d.get("constraints", "")

    facts: List[str] = diag.get("facts") or []
    f1 = facts[0] if len(facts) >= 1 else "（请补一条可验证事实，比如‘上周三之后发生了3次临时变更’）"
    f2 = facts[1] if len(facts) >= 2 else ""

    opening_lines = [
        f"我想和你单独花 10 分钟把这件事谈清楚，目标是：{goal}。",
        f"我先说明一下：我不是想指责你、也不是想把责任推给你；我真正想要的是把事实对齐，然后一起决定下一步怎么做，避免{fear}。",
        f"我这边先说我看到的事实：{f1}" + (f"；另外还有：{f2}" if f2 else "") + "。",
        "我可能理解得不完整，所以也想听听你这边看到的情况。",
    ]
    if constraints:
        opening_lines.insert(1, f"（限制条件：{constraints}，所以我想我们更聚焦在事实与下一步。）")
    opening_script = "\n".join(opening_lines)

    rewrites: List[str] = []
    if _nonempty(d.get("last_dialogue_you", "")):
        rewrites.extend(_rewrite_blame(d["last_dialogue_you"], facts=facts, goal=goal))
    else:
        # Provide a default rewrite example based on common trigger words
        rewrites.extend(
            _rewrite_blame("你们总是临时改，影响大家进度。", facts=facts, goal=goal)
        )

    # CASE-SPECIFIC STATE lines (filled, not blanks)
    story_hint = d.get("your_story_clean", "") if isinstance(d.get("your_story_clean", ""), str) else ""
    if not story_hint:
        story_hint = diag.get("your_story_clean") or "我担心我们现在在用不同的标准理解问题，继续下去会影响交付。"

    state_lines = [
        f"【事实】我观察到：{f1}。",
        f"【影响/担心】这让我担心会影响{goal}，也担心我们彼此会误解对方的出发点。",
        f"【邀请】你这边看到的关键事实是什么？你最担心的点是什么？",
        f"【试探】我可能有偏差，你直接纠正我也没关系。",
    ]

    # AMPP tailored with counterpart and their last message
    them = d.get("last_dialogue_them", "").strip()
    ampp = [
        f"我听到你在强调“{them[:18]}{'…' if len(them) > 18 else ''}”，你的核心担心是什么？（Paraphrase → Ask）" if them else "我想确认一下：你最担心的风险是什么？（Ask）",
        f"我注意到你刚才语气变重/停顿了一下，我猜这件事对你也有压力。你在担心什么？（Mirror）",
        f"如果我们把‘谁对谁错’先放一边，只谈怎么达成{goal}，你觉得最需要先对齐哪一条规则/事实？（Ask）",
        f"我先猜一个：你是不是担心最后责任会落在你身上？如果是，我们可以把责任边界写清楚。（Prime）",
    ]

    agreement = [
        "我们先确认三件事：",
        "1) 结论/目标：___",
        "2) 下一步行动（WHAT）：___",
        "3) 负责人（WHO）+ 时间（WHEN）：___",
        "我会在___（时间）再同步一次进展，确保这件事落地。",
    ]

    safety_fix = [
        "如果对话开始变得紧张：先停一下，回到共同目的。",
        f"可以直接说：我感觉我们有点紧张了。我很在乎这次沟通能对事不对人，我们先回到目标：{goal}。",
        f"如果出现尊重缺口：补一句认可（不是讨好）：我认可你在你部门目标/压力上的难处，我们一起找一个让双方都能承受的方案。",
    ]

    focus = diag.get("primary_focus", "")
    if "安全感" in focus:
        top = [
            f"先把安全感拉回来：用“我不是要___；我真正要___”的对比法，把{counterpart}从‘被指责’拉回到‘共同解决’。",
            "把争论从‘动机/态度’拉回到‘事实/规则/下一步’，避免越吵越虚。",
        ]
    else:
        top = [
            "先把事实写成 2–3 条（从 STAR 里挑），再说影响/担心，最后邀请对方补全事实。",
            "针对对方的‘合理性/背锅/排期不合理’叙事，先承认其关切，再讨论规则与边界。",
        ]

    return {
        "top_moves": top,
        "opening_script": opening_script,
        "rewrites": rewrites,
        "state_lines": state_lines,
        "ampp_prompts": ampp,
        "safety_fix": safety_fix,
        "agreement_close": agreement,
        "practice_task": [
            "从你 STAR 里抄出 2–3 条“可验证事实”（每条一句话，不带解释）。",
            "把你最想说的那句“刺激句”（含总是/从不/你就是）写出来，让系统/AI帮你改成事实+影响+邀请。",
            "准备收尾三件事：WHAT/WHO/WHEN（写在会议纪要/IM 里，避免口头扯皮）。",
        ],
    }


@app.post("/api/diagnose")
async def diagnose(request: Request) -> JSONResponse:
    payload = await request.json()
    d = _extract(payload or {})
    missing = _require_fields(d, ["relationship", "goal", "fear", "situation", "task", "action", "result"])
    if missing:
        return JSONResponse(status_code=400, content={"error": "missing_fields", "missing": missing})

    # AI-only: return a job id and poll for result.
    job_id = str(uuid.uuid4())
    JOBS[job_id] = {
        "id": job_id,
        "type": "diagnose",
        "status": "running",
        "created_at": time.time(),
    }

    async def run_once(attempt: int):
        return await _ai_diagnose(d, timeout_s=_ai_http_read_timeout_s(attempt))

    asyncio.create_task(_run_until_ok(job_id=job_id, kind="diagnose", run_once=run_once))

    return JSONResponse(
        content={
            "job_id": job_id,
            "status": "running",
        }
    )


@app.post("/api/advise")
async def advise(request: Request) -> JSONResponse:
    payload = await request.json()
    d = _extract(payload or {})
    diagnosis = payload.get("diagnosis") if isinstance(payload, dict) else None
    missing = _require_fields(d, ["relationship", "goal", "fear", "situation", "task", "action", "result"])
    if missing:
        return JSONResponse(status_code=400, content={"error": "missing_fields", "missing": missing})

    if not isinstance(diagnosis, dict):
        return JSONResponse(status_code=400, content={"error": "missing_diagnosis"})
    diag = diagnosis

    job_id = str(uuid.uuid4())
    JOBS[job_id] = {
        "id": job_id,
        "type": "advise",
        "status": "running",
        "created_at": time.time(),
    }

    async def run_once(attempt: int):
        return await _ai_advise(d, diag, timeout_s=_ai_http_read_timeout_s(attempt))

    asyncio.create_task(_run_until_ok(job_id=job_id, kind="advise", run_once=run_once))

    return JSONResponse(
        content={
            "job_id": job_id,
            "status": "running",
        }
    )


@app.post("/api/analyze")
async def analyze(request: Request) -> JSONResponse:
    """
    Backward-compatible: returns diagnosis + plan in one call.
    UI now prefers /api/diagnose then /api/advise for faster partial rendering.
    """
    payload = await request.json()
    d = _extract(payload or {})
    missing = _require_fields(d, ["relationship", "goal", "fear", "situation", "task", "action", "result"])
    if missing:
        return JSONResponse(status_code=400, content={"error": "missing_fields", "missing": missing})

    diag_res = await _ai_diagnose(d)
    if diag_res.get("ok"):
        plan_res = await _ai_advise(d, diag_res["diagnosis"])
        if plan_res.get("ok"):
            return JSONResponse(
                content={
                    "input": d,
                    "diagnosis": diag_res["diagnosis"],
                    "plan": plan_res["plan"],
                    "analysis_mode": "ai",
                }
            )
        plan_fallback = _script(d, diag_res["diagnosis"])
        return JSONResponse(
            content={
                "input": d,
                "diagnosis": diag_res["diagnosis"],
                "plan": plan_fallback,
                "analysis_mode": "local",
                "ai_failure": plan_res.get("failure") or {"reason": "unknown", "detail": "生成建议失败，已兜底。"},
            }
        )

    diag = _diagnose(d)
    plan = _script(d, diag)
    out: Dict[str, Any] = {"input": d, "diagnosis": diag, "plan": plan, "analysis_mode": "local"}
    if diag_res.get("failure"):
        out["ai_failure"] = diag_res["failure"]
    return JSONResponse(content=out)


def _delta_text(delta: Dict[str, Any]) -> str:
    """OpenAI / some providers use string or list-of-parts for delta.content."""
    if not delta:
        return ""
    c = delta.get("content")
    if isinstance(c, str) and c:
        return c
    if isinstance(c, list):
        out: List[str] = []
        for part in c:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict):
                t = part.get("text")
                if isinstance(t, str):
                    out.append(t)
        return "".join(out)
    # 少数推理模型把可见输出放在 reasoning_content
    r = delta.get("reasoning_content")
    if isinstance(r, str) and r:
        return r
    return ""


def _accumulate_stream_sse(text: str) -> str:
    """Parse OpenAI-compatible SSE chunk lines; returns concatenated assistant content."""
    parts: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            piece = _delta_text(delta)
            if piece:
                parts.append(piece)
    return "".join(parts)


async def _openai_chat(messages: List[Dict[str, str]], max_tokens: int, timeout_s: float) -> Dict[str, Any]:
    # Prefer Doubao/ARK vars; keep OPENAI_* for compatibility
    api_key = (os.environ.get("DOUBAO_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("API key not set")

    base_url = (
        os.environ.get("DOUBAO_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or "https://api.openai.com/v1"
    ).strip().rstrip("/")
    model = (os.environ.get("DOUBAO_MODEL") or os.environ.get("OPENAI_MODEL") or "doubao-lite-4k").strip()

    url = f"{base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}"}
    use_stream = _use_llm_stream()
    body: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "stream": use_stream,
    }
    if (os.environ.get("DOUBAO_JSON_OBJECT", "1") or "").strip() != "0":
        body["response_format"] = {"type": "json_object"}

    timeout = httpx.Timeout(connect=15.0, read=timeout_s, write=15.0, pool=4.0)

    if not use_stream:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, headers=headers, json=body)
            r.raise_for_status()
            return r.json()

    buf: List[str] = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, headers=headers, json=body) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if line is None:
                    continue
                buf.append(line)
                if line.strip() == "data: [DONE]":
                    break
    full_text = _accumulate_stream_sse("\n".join(buf))
    # 流式偶发拼出空串（或网关格式差异），回退一次非流式完整 JSON
    if not (full_text or "").strip():
        body_ns = dict(body)
        body_ns["stream"] = False
        async with httpx.AsyncClient(timeout=timeout) as client:
            r2 = await client.post(url, headers=headers, json=body_ns)
            r2.raise_for_status()
            return r2.json()
    return {"choices": [{"message": {"content": full_text}}]}

def _json_loose_fix(s: str) -> str:
    """Remove trailing commas before } or ] (common invalid JSON from models)."""
    return re.sub(r",(\s*[\]}])", r"\1", s)


def _try_python_literal_dict(text: str) -> Optional[Dict[str, Any]]:
    """
    Fallback for python-like dict/list outputs, e.g. single quotes / True / False.
    """
    if not text:
        return None
    t = text.strip()
    start = t.find("{")
    end = t.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    candidate = t[start : end + 1]
    try:
        obj = ast.literal_eval(candidate)
        if isinstance(obj, dict):
            return obj
        if isinstance(obj, list):
            for item in obj:
                if isinstance(item, dict):
                    return item
    except Exception:
        return None
    return None


def _score_json_richness(obj: Any) -> int:
    """Prefer complete diagnosis objects over {}, wrappers, or tiny fragments."""
    if not isinstance(obj, dict):
        return -1

    def _sv(v: Any) -> int:
        if v is None:
            return 0
        if isinstance(v, bool):
            return 2
        if isinstance(v, (int, float)):
            return 6
        if isinstance(v, str):
            s = v.strip()
            return min(len(s) * 2, 800) if s else 0
        if isinstance(v, list):
            return 12 + sum(_sv(x) for x in v[:40])
        if isinstance(v, dict):
            return 8 + sum(_sv(x) for x in v.values())
        return 1

    return _sv(obj)


def _json_from_model_text(text: str) -> Optional[Dict[str, Any]]:
    """
    Try to recover a JSON object from model output.
    Accepts either pure JSON, or JSON inside ``` fences.
    Uses JSONDecoder.raw_decode so nested braces inside strings do not break parsing.
    """
    if not text:
        return None
    t = text.strip()
    # 全角引号等偶发情况
    t = t.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    if "```" in t:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", t, re.IGNORECASE)
        if m:
            t = m.group(1).strip()

    decoder = json.JSONDecoder()
    best: Optional[Dict[str, Any]] = None
    best_sc = -1
    i = 0
    while i < len(t):
        start = t.find("{", i)
        if start == -1:
            break
        sub = t[start:]
        for candidate in (sub, _json_loose_fix(sub)):
            try:
                obj, _end = decoder.raw_decode(candidate)
                if isinstance(obj, dict):
                    sc = _score_json_richness(obj)
                    if sc > best_sc:
                        best_sc = sc
                        best = obj
                    break
            except json.JSONDecodeError:
                continue
        i = start + 1
    if best is not None:
        return best
    # Fallback: tolerate python-like dict/list text
    return _try_python_literal_dict(t)


def _chat_content(data: Dict[str, Any]) -> str:
    try:
        return ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "") or ""
    except Exception:
        return ""


async def _call_ai_json(prompt: str, max_tokens: int, timeout_s: float, retries: int = 1) -> Dict[str, Any]:
    api_key = (os.environ.get("DOUBAO_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return {"ok": False, "failure": {"reason": "no_api_key", "detail": "未配置 DOUBAO_API_KEY。"}}

    sys_text = (
        os.environ.get("DOUBAO_SYSTEM_PROMPT", "").strip()
        or "只输出一个合法 JSON 对象（以{开头以}结尾），不要 markdown、不要解释。键名用英文双引号。结合案例细节。"
    )
    system = {"role": "system", "content": sys_text}
    user = {"role": "user", "content": prompt}

    last_err: Optional[Dict[str, Any]] = None
    for attempt in range(max(1, retries + 1)):
        # On retry, shrink output further to increase chance of returning in time.
        tok = max_tokens if attempt == 0 else max(80, int(max_tokens * 0.6))
        tout = timeout_s if attempt == 0 else max(6.0, timeout_s * 0.35)
        try:
            data = await _openai_chat([system, user], max_tokens=tok, timeout_s=tout)
            text = _chat_content(data)
            obj = _json_from_model_text(text)
            if not obj:
                last_err = {
                    "reason": "bad_json",
                    "detail": "模型返回无法解析为 JSON（可能被截断）。",
                    "raw_preview": (text or "")[:500],
                }
                continue
            return {"ok": True, "json": obj}
        except httpx.HTTPStatusError as e:
            body = ""
            try:
                body = (e.response.text or "")[:1200]
            except Exception:
                pass
            return {
                "ok": False,
                "failure": {"reason": "http_error", "detail": f"HTTP {e.response.status_code}", "body_preview": body},
            }
        except httpx.ReadTimeout:
            last_err = {
                "reason": "timeout",
                "detail": f"调用模型读超时（本次 HTTP 读超时上限 {tout:.0f}s）。若经常超时，可在 .env 提高 AI_READ_TIMEOUT。",
            }
            continue
        except Exception as e:
            last_err = {"reason": "exception", "detail": f"{type(e).__name__}: {repr(e)}"[:800]}
            continue

    return {"ok": False, "failure": last_err or {"reason": "timeout", "detail": "调用豆包超时，建议重试。"}}


async def _ai_diagnose(d: Dict[str, Any], *, timeout_s: float = 120.0) -> Dict[str, Any]:
    # 学员反馈原文（STAR+补充），供拆解 CPR / 路径，不要求模型「复述剧情」
    case = {
        "rel": d.get("relationship"),
        "who": d.get("counterpart") or "",
        "goal": d.get("goal"),
        "fear": d.get("fear"),
        "S": _compact_text(d.get("situation", ""), 280),
        "T": _compact_text(d.get("task", ""), 220),
        "A": _compact_text(d.get("action", ""), 320),
        "R": _compact_text(d.get("result", ""), 220),
        "last_you": _compact_text(d.get("last_dialogue_you") or "", 200),
        "last_them": _compact_text(d.get("last_dialogue_them") or "", 200),
        "your_story": _compact_text(d.get("your_story") or "", 200),
    }
    tmpl = (os.environ.get("DOUBAO_PROMPT_DIAGNOSE") or "").strip()
    if tmpl:
        prompt = tmpl.replace("{case}", json.dumps(case, ensure_ascii=False))
    else:
        prompt = (
            "【任务】对下面「学员反馈」做原因分析：拆解 CPR，并分别梳理「你方 / 对方」的行为路径。"
            "重点写清：双方情绪为何被点燃或升级（触发点、解读、未被满足的需要等），不要像讲故事一样复述 STAR。\n"
            "【输出】仅一个 JSON 对象，字段如下（均为字符串，数组除外）：\n"
            "- is_crucial: boolean\n"
            "- cpr_c, cpr_p, cpr_r: CPR 三层——内容/议题；互动模式（沉默或暴力倾向的具体互动）；关系与信任层面\n"
            "- path_you, path_them: 对象，各含 facts, story, emotion, behavior, emotion_why。"
            " facts=可验证事实；story=对事实赋予的意义；emotion=显性情绪或可从行为推断的情绪；"
            " behavior=沉默或暴力式应对；emotion_why=该方情绪产生的可能原因（因果，要写细）\n"
            "- emotion_why_both: 全文最重点——综合双方情绪成因（可引用反馈原话，避免空泛）\n"
            "- common_goal: 一句话共同目标\n"
            "- key_facts: 字符串数组，可验证关键事实\n"
            "- story_clean: 若学员「故事」偏满，给一句更中性表述（可选，可为空字符串）\n"
            "【学员反馈】"
            f"{json.dumps(case, ensure_ascii=False)}"
        )
    res = await _call_ai_json(prompt, max_tokens=_max_tokens_diagnose(), timeout_s=timeout_s, retries=0)
    if not res.get("ok"):
        return res
    j = _normalize_diagnosis_raw(res["json"])
    cpr = _cpr_from_ai_json(j)
    path_you = _normalize_path_dict(j.get("path_you"))
    path_them = _normalize_path_dict(j.get("path_them"))
    if not _path_has_content(path_you) and (j.get("behavior_you") or "").strip():
        path_you = _normalize_path_dict({**dict(path_you), "behavior": str(j.get("behavior_you", "")).strip()})
    if not _path_has_content(path_them) and (j.get("behavior_them") or "").strip():
        path_them = _normalize_path_dict({**dict(path_them), "behavior": str(j.get("behavior_them", "")).strip()})
    mono_you = _behavior_path_mono(path_you)
    mono_them = _behavior_path_mono(path_them)
    diagnosis = {
        "is_crucial": bool(j.get("is_crucial", True)),
        "cpr": cpr,
        "behavior_path": {
            "you": mono_you or (j.get("behavior_you") or ""),
            "them": mono_them or (j.get("behavior_them") or ""),
        },
        "path_you": path_you,
        "path_them": path_them,
        "emotion_why_both": str(j.get("emotion_why_both") or j.get("emotion_focus") or "").strip(),
        "common_goal": str(j.get("common_goal", "")).strip(),
        "key_facts": _key_facts_from_ai(j),
        "your_story_clean": str(j.get("story_clean", j.get("your_story_clean", "")) or "").strip(),
    }
    return {"ok": True, "diagnosis": diagnosis}


async def _ai_advise(d: Dict[str, Any], diagnosis: Optional[Dict[str, Any]], *, timeout_s: float = 120.0) -> Dict[str, Any]:
    diag = diagnosis or {}
    # 建议阶段只依赖「已完成的分析 + 沟通目标」，避免再让模型复述 STAR 场景
    advise_ctx = {
        "relationship": d.get("relationship"),
        "counterpart": d.get("counterpart") or "",
        "channel": d.get("channel"),
        "goal": d.get("goal"),
        "fear": d.get("fear"),
        "constraints": _clamp(d.get("constraints"), 160),
        "diagnosis": {
            "cpr": diag.get("cpr"),
            "emotion_why_both": diag.get("emotion_why_both"),
            "common_goal": diag.get("common_goal"),
            "path_you": diag.get("path_you"),
            "path_them": diag.get("path_them"),
            "key_facts": diag.get("key_facts"),
        },
    }
    tmpl = (os.environ.get("DOUBAO_PROMPT_ADVISE") or "").strip()
    if tmpl:
        prompt = tmpl.replace("{case}", json.dumps(advise_ctx, ensure_ascii=False))
    else:
        prompt = (
            "【任务】基于下方 diagnosis（原因分析已完成），只写「下一次沟通」可直接照读的话术。"
            "按关键对话：开场（共同目的/对比说明）→ STATE（分享你的路径）→ AMPP（征询观点）→ 收尾（WHAT/WHO/WHEN）。\n"
            "【禁止】不要复述、分析或总结 STAR 场景；不要重复原因分析段落；不要写「当时应该怎样」。\n"
            "【输出】仅一个 JSON：opening(string), state(string[]), ampp(string[]), close(string[])。"
            "state 数组顺序对应 STATE 各步；ampp 每句为提问话术；全部为可直接说出口的短句。\n"
            "【输入】"
            f"{json.dumps(advise_ctx, ensure_ascii=False)}"
        )
    res = await _call_ai_json(prompt, max_tokens=_max_tokens_advise(), timeout_s=timeout_s, retries=0)
    if not res.get("ok"):
        return res
    j = res["json"]
    plan = {
        "opening": j.get("opening", ""),
        "state": j.get("state", []) if isinstance(j.get("state"), list) else [],
        "ampp": j.get("ampp", []) if isinstance(j.get("ampp"), list) else [],
        "close": j.get("close", []) if isinstance(j.get("close"), list) else [],
    }
    return {"ok": True, "plan": plan}


async def _run_until_ok(
    *,
    job_id: str,
    kind: str,
    run_once,
    total_timeout_s: float = 30 * 60,
    backoff_s: float = 0.8,
    backoff_max_s: float = 10.0,
) -> None:
    """
    Keep retrying until success or total timeout.
    Updates JOBS[job_id] in-place with progress.
    """
    deadline = time.time() + total_timeout_s
    attempt = 0
    sleep_s = backoff_s

    while time.time() < deadline:
        attempt += 1
        JOBS[job_id] = {
            **JOBS.get(job_id, {}),
            "status": "running",
            "attempt": attempt,
            "updated_at": time.time(),
        }
        res = await run_once(attempt)
        if res.get("ok"):
            payload = {"status": "done", "analysis_mode": "ai", "updated_at": time.time()}
            if kind == "diagnose":
                payload["diagnosis"] = res["diagnosis"]
            else:
                payload["plan"] = res["plan"]
            JOBS[job_id] = {**JOBS[job_id], **payload}
            return

        # Save last failure but keep running
        JOBS[job_id] = {
            **JOBS[job_id],
            "last_failure": res.get("failure"),
            "updated_at": time.time(),
        }

        # Faster early retries to improve time-to-first-success under queueing.
        await asyncio.sleep(sleep_s)
        sleep_s = min(backoff_max_s, sleep_s * 1.2)

    JOBS[job_id] = {
        **JOBS.get(job_id, {}),
        "status": "failed",
        "analysis_mode": "local",
        "ai_failure": {
            "reason": "timeout_total",
            "detail": "等待超过 30 分钟仍未拿到豆包返回，请稍后再试。",
        },
        "updated_at": time.time(),
    }

    case = {
        "relationship": d.get("relationship"),
        "counterpart": d.get("counterpart"),
        "channel": d.get("channel"),
        "goal": d.get("goal"),
        "fear": d.get("fear"),
        "constraints": d.get("constraints"),
        "star": {
            "situation": d.get("situation"),
            "task": d.get("task"),
            "action": d.get("action"),
            "result": d.get("result"),
        },
        "last_dialogue": {
            "you": d.get("last_dialogue_you"),
            "them": d.get("last_dialogue_them"),
        },
        "your_story": d.get("your_story"),
    }

    system = {
        "role": "system",
        "content": (
            "你是《关键对话》（Crucial Conversations）课程的资深讲师与教练。"
            "你要基于学员提交的具体案例进行透彻的定性分析，并给出可直接照读的话术。"
            "必须使用关键概念来组织分析："
            "Start with Heart、Make it Safe（共同目的/相互尊重/对比法）、"
            "Master My Stories（事实与故事分离）、STATE My Path、Explore Others’ Paths（AMPP）、Move to Action（WHAT/WHO/WHEN）。"
            "\n约束：不要输出空模板或公式化填空；必须引用案例细节（STAR与原话）；明确指出卡点与触发防御的句子，并给替代说法；语言自然像真实教练。"
        ),
    }

    user = {
        "role": "user",
        "content": (
            "请对下面这个学员案例做分析与话术生成。\n"
            "请只输出一个 JSON 对象（不要输出任何额外文字）。\n"
            "JSON 结构必须是：\n"
            "{\n"
            '  \"diagnosis\": {\n'
            '    \"is_crucial\": true/false,\n'
            '    \"primary_focus\": \"一句话优先关注\",\n'
            '    \"triggers\": [\"触发防御点...\"],\n'
            '    \"facts\": [\"2-4条可验证事实（来自STAR/原话）\"],\n'
            '    \"your_story_clean\": \"把学员的故事降火后的表述（不贴标签/不读心）\",\n'
            '    \"their_possible_story\": [\"对方可能的故事/关切（基于案例）\"]\n'
            "  },\n"
            '  \"plan\": {\n'
            '    \"top_moves\": [\"为什么这样做（结合案例）...\"],\n'
            '    \"opening_script\": \"可直接照读的开场（含对比法/共同目的）\",\n'
            '    \"rewrites\": [\"把学员原话改写（至少2种风格）...\"],\n'
            '    \"state_lines\": [\"下一步你怎么说（4-7句，全部结合案例细节）...\"],\n'
            '    \"ampp_prompts\": [\"2-5个追问（结合对方原话/可能关切）...\"],\n'
            '    \"safety_fix\": [\"紧张时怎么修复安全感（结合场景）...\"],\n'
            '    \"agreement_close\": [\"收尾落地（WHAT/WHO/WHEN）...\"],\n'
            '    \"practice_task\": [\"课后练习/准备清单（结合本案例）...\"]\n'
            "  }\n"
            "}\n\n"
            f"案例：{case}"
        ),
    }

    try:
        data = await _openai_chat([system, user])
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        obj = _json_from_model_text(text)
        if not obj:
            return None
        if not isinstance(obj.get("diagnosis"), dict) or not isinstance(obj.get("plan"), dict):
            return None
        return {"diagnosis": obj["diagnosis"], "plan": obj["plan"]}
    except Exception:
        return None


@app.post("/api/chat")
async def chat(request: Request) -> JSONResponse:
    payload = await request.json()
    messages = payload.get("messages") or []
    context = payload.get("context") or {}

    # Server-side system prompt: keep it aligned with Crucial Conversations.
    system = {
        "role": "system",
        "content": (
            "你是《关键对话》课程的教练助手。"
            "目标：帮助学员在高风险/强情绪/观点差异场景中，先建立安全感与共同目的，再用事实-故事-邀请对方的方式推进。"
            "输出要求："
            "1) 先问 1-3 个高价值澄清问题；"
            "2) 给 1 个可直接复述的开场话术（可选对比法）；"
            "3) 给 2-4 个可用的追问（AMPP）；"
            "4) 若存在指责/贴标签/读心，帮学员改写成事实+感受+担心；"
            "5) 必须引用学员案例中的具体细节（不要只给通用模板）；"
            "6) 语气专业、具体、不过度说教。"
        ),
    }

    # Add lightweight context from analysis if provided.
    ctx_text = ""
    if isinstance(context, dict) and context:
        who = context.get("counterpart", "") or context.get("relationship", "")
        goal = context.get("goal", "")
        primary = (context.get("diagnosis") or {}).get("primary_focus", "")
        if who or goal or primary:
            ctx_text = f"学员场景简要信息：对方/对象={who}；目标={goal}；优先关注={primary}。"

    if ctx_text:
        system = {**system, "content": system["content"] + "\n" + ctx_text}

    full_messages = [system] + messages

    try:
        data = await _openai_chat(full_messages)
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        return JSONResponse(content={"mode": "live", "reply": text})
    except Exception:
        # Mock reply: still useful when no key available.
        last = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last = m.get("content", "")
                break
        mock = (
            "我先确认几个关键点（你可以按序回答其中 1-2 个就好）：\n"
            "1) 你真正想要的具体结果是什么（可观察）？\n"
            "2) 你最担心的风险是什么（关系/结果/面子/合规）？\n"
            "3) 你能列出 2-3 条‘可验证事实’吗（不包含动机推断）？\n\n"
            "一个更安全的开场（对比法）：\n"
            "“我想把___谈清楚，目标是___。我不是想指责你/让你难堪；我真正想要的是我们对齐事实，然后决定下一步怎么做。”\n\n"
            "可用追问（AMPP）：\n"
            "- “你刚才停顿了一下，我想确认你在担心什么？”\n"
            "- “我听到你的重点是___，对吗？”\n"
            "- “在你看来，最关键的事实是什么？”\n\n"
            f"你刚才的补充是：{last[:120]}{'…' if len(last) > 120 else ''}\n"
            "你可以先把你准备说的‘事实’写成 3 条，我帮你改写成更不引发防御的版本。"
        )
        return JSONResponse(content={"mode": "mock", "reply": mock})


if __name__ == "__main__":
    import uvicorn

    # 默认 0.0.0.0：同一局域网内其他设备可用「本机 IP:端口」访问；仅本机则设 HOST=127.0.0.1
    _host = (os.environ.get("HOST") or "0.0.0.0").strip() or "0.0.0.0"
    _port = int((os.environ.get("PORT") or "8000").strip() or "8000")
    uvicorn.run("app:app", host=_host, port=_port, reload=True)

