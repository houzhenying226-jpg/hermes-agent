"""Project-specific watchdog for the Fesun nine-module SPEC runbook.

This watchdog is intentionally local-first: each tick inspects files, git,
kanban state, and the runbook ledger without calling an LLM.  It only creates a
Hermes worker after repeated no-progress ticks, which keeps token spend low and
prevents the old "one slow turn spawned three more" failure mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any
from uuid import uuid4

from hermes_autonomous import DEFAULT_BOARD, DEFAULT_GOAL_TURNS, _create_goal_task, _dispatch_once


FESUN_SCHEMA = "hermes.fesun-nine-spec.v1"
FESUN_JOB_NAME = "Fesun Nine Module SPEC Watchdog"
FESUN_SCRIPT_NAME = "fesun_nine_spec_watchdog.py"
FESUN_VISIBLE_STATUS_NAME = "Hermes-Fesun-实时状态.md"
DEFAULT_REPO = "/Users/james/fesun-platform"
DEFAULT_ASSIGNEE = "default"
DEFAULT_RUNBOOK = (
    "/Users/james/fesun-platform/docs/plans/"
    "00_九模块开发SOP与状态台账_权威runbook.md"
)
REQUIRED_READING = [
    DEFAULT_RUNBOOK,
    "/Users/james/fesun-platform/docs/handoff/交接给Hermes_九模块自主执行_2026-06-30.md",
    "/Users/james/fesun-platform/docs/handoff/全程总账_做了什么没做什么_2026-06-30.md",
    "/Users/james/fesun-platform/docs/handoff/护栏清单_放Hermes碰代码前必做_2026-06-30.md",
    DEFAULT_RUNBOOK + "#§F",
    DEFAULT_RUNBOOK + "#§C",
]
NO_PROGRESS_THRESHOLD = 1
MAX_STAGE_ATTEMPTS = 3
CLOSURE_STALL_SECONDS = 10 * 60
CLOSURE_LOG_TAIL_BYTES = 96 * 1024
ALLOWED_PREFIXES = (
    "docs/specs/",
    "docs/stories/",
    "docs/flows/",
    "docs/contracts/",
    "docs/plans/",
    "docs/reviews/",
    "docs/handoff/",
    "docs/ai-memory/",
)
FORBIDDEN_PREFIXES = (
    ".github/",
    "apps/",
    "config/",
    "fesun_contract_hub/",
    "frontend/",
    "sites/",
    "scripts/deploy",
    "deploy",
    "bench",
)
FORBIDDEN_PARTS = (
    "/doctype/",
    "/services/",
    "/workflow",
    "/workflows/",
    "/secrets",
    "/.env",
)
ACTIVE_STATUSES = {"todo", "scheduled", "ready", "running", "review"}
TERMINAL_STATUSES = {"done", "archived"}
DOCS_POLICY_GAP_PREFIXES = (
    "docs/",
)
REVIEW_STAGE_MARKERS = ("brief_review", "story_flow_review", "spec_review", "contract_review", "plan_review")
REVIEW_RECOVERY_PREFIX = "fesun-nine-spec-recovery"
STATUS_HEADERS = ("模块", "当前步", "故事", "spec", "contract", "Linear", "开发", "真待决")
CLOSURE_COMPLETION_MARKERS = (
    "已完成了 SPEC worker 能做的一切",
    "SPEC worker 能做的一切",
    "completed the SPEC-worker-achievable",
    "completed the three SPEC-worker-doable",
    "written the results to disk",
    "三项 code_preflight 审查产物",
    "code_preflight 审查产物",
    "产物已落盘",
    "runbook was updated",
    "runbook 已回写",
    "work is substantively correct",
)
STAGE_ORDER = {
    "brief": 10,
    "brief_review": 20,
    "story_flow": 30,
    "story_flow_review": 40,
    "spec": 50,
    "spec_revision": 55,
    "spec_review": 60,
    "contract": 70,
    "contract_review": 80,
    "plan": 90,
    "plan_review": 100,
    "pr_breakdown": 110,
    "code_preflight": 120,
}
SUPERVISED_CODE_PR_ACTION_MARKERS = (
    "进入受监督Code/PR准备",
    "进入supervisedCode/PR准备",
    "supervisedCode/PRimplementationpreparation",
)
SUPERVISED_CODE_PR_GATE_MARKERS = (
    "Code前门禁supervised已解锁",
    "code_preflight2/3PASS",
    "code_preflightreviews",
    "code_preflightconclusion",
    "code_preflight结论",
)
SUPERVISED_CODE_PR_HANDOFF_MARKERS = (
    "next_action_",
    "code-readygate",
    "PR拆解前置准备",
    "PR拆解已完成",
    "PR拆解卡",
    "pr-breakdown-prep",
    "pr-breakdown.md",
)


@dataclass(frozen=True)
class FesunTickOptions:
    repo: str = DEFAULT_REPO
    runbook: str = DEFAULT_RUNBOOK
    board: str = DEFAULT_BOARD
    assignee: str | None = DEFAULT_ASSIGNEE
    goal_max_turns: int = DEFAULT_GOAL_TURNS
    create_tasks: bool = False
    dispatch: bool = False
    no_progress_threshold: int = NO_PROGRESS_THRESHOLD
    max_stage_attempts: int = MAX_STAGE_ATTEMPTS
    closure_stall_seconds: int = CLOSURE_STALL_SECONDS


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()


def state_path() -> Path:
    return _home() / "delivery" / "fesun-nine-spec-watchdog.json"


def heartbeat_path() -> Path:
    return _home() / "delivery" / "fesun-nine-spec-heartbeat.json"


def visible_status_path() -> Path:
    return _home() / "delivery" / "fesun-nine-spec-status.md"


def desktop_visible_status_path() -> Path:
    return Path.home() / "Desktop" / FESUN_VISIBLE_STATUS_NAME


def _scripts_dir() -> Path:
    return _home() / "scripts"


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return fallback
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback
    return data if isinstance(data, dict) else fallback


def _write_text_best_effort(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError:
        return


def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _run(args: list[str], cwd: str | Path | None = None, timeout: int = 30) -> dict[str, Any]:
    try:
        result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=timeout)
    except Exception as exc:
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": str(exc), "args": args}
    return {
        "ok": result.returncode == 0,
        "exit_code": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "args": args,
    }


def _initial_state() -> dict[str, Any]:
    return {
        "schema": FESUN_SCHEMA,
        "created_at": _utcnow(),
        "updated_at": _utcnow(),
        "enabled": True,
        "repo": DEFAULT_REPO,
        "runbook": DEFAULT_RUNBOOK,
        "board": DEFAULT_BOARD,
        "no_progress_threshold": NO_PROGRESS_THRESHOLD,
        "max_stage_attempts": MAX_STAGE_ATTEMPTS,
        "closure_stall_seconds": CLOSURE_STALL_SECONDS,
        "last_progress_fingerprint": "",
        "no_progress_count": 0,
        "attempts": {},
        "baseline_out_of_scope": [],
        "ticks": [],
    }


def load_state() -> dict[str, Any]:
    state = _read_json(state_path(), _initial_state())
    for key, value in _initial_state().items():
        state.setdefault(key, value)
    return state


def save_state(state: dict[str, Any]) -> dict[str, Any]:
    state["updated_at"] = _utcnow()
    _atomic_write_json(state_path(), state)
    return state


def _runbook_sections(text: str) -> dict[str, str]:
    matches = list(re.finditer(r"^## §([A-Z]) .*$", text, flags=re.MULTILINE))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[match.group(1)] = text[start:end]
    return sections


def _parse_table(section: str) -> list[dict[str, str]]:
    lines = [line.strip() for line in section.splitlines() if line.strip().startswith("|")]
    if len(lines) < 3:
        return []
    headers = [cell.strip() for cell in lines[0].strip("|").split("|")]
    rows: list[dict[str, str]] = []
    for line in lines[2:]:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < len(headers):
            cells += [""] * (len(headers) - len(cells))
        rows.append(dict(zip(headers, cells[: len(headers)])))
    return rows


def _parse_status_rows(section: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < len(STATUS_HEADERS):
            continue
        if cells[0] == "模块" or set(cells[0]) <= {"-", ":"}:
            continue
        row = dict(zip(STATUS_HEADERS, cells[: len(STATUS_HEADERS)]))
        row_text = " ".join(row.values())
        if not any(marker in row_text for marker in ("✅", "FSN-", "下一合法动作", "可进入", "BLOCKED", "PASS")):
            continue
        rows.append(row)
    return rows


def _merge_status_rows(primary: list[dict[str, str]], rescue: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[str]]:
    modules = list(primary)
    seen = {row.get("模块", "") for row in modules}
    rescued: list[str] = []
    for row in rescue:
        name = row.get("模块", "")
        if not name or name in seen:
            continue
        modules.append(row)
        seen.add(name)
        rescued.append(name)
    return modules, rescued


def parse_runbook(runbook: str | Path) -> dict[str, Any]:
    path = Path(runbook)
    text = path.read_text(encoding="utf-8")
    sections = _runbook_sections(text)
    modules = _parse_status_rows(sections.get("E", ""))
    rescue_rows: list[dict[str, str]] = []
    for key, section in sections.items():
        if key == "E":
            continue
        rescue_rows.extend(_parse_status_rows(section))
    modules, rescued_modules = _merge_status_rows(modules, rescue_rows)
    boundaries = _parse_table(sections.get("F", ""))
    return {
        "path": str(path),
        "sha": _sha_text(text),
        "sections": sections,
        "modules": modules,
        "rescued_modules": rescued_modules,
        "boundaries": boundaries,
    }


def _module_slug(name: str) -> str:
    cleaned = re.sub(r"[`*_（）()\\[\\] ]+", "", name)
    if "商务" in cleaned:
        return "商务"
    if "销售" in cleaned or "B2B" in cleaned:
        return "销售B2B"
    if "生产" in cleaned or "交付" in cleaned:
        return "生产交付"
    if "产品" in cleaned or "成本" in cleaned:
        return "产品成本"
    if "人力" in cleaned or "KPI" in cleaned:
        return "人力KPI"
    if "门店" in cleaned or "B2C" in cleaned:
        return "门店B2C"
    if "专项" in cleaned:
        return "专项制度"
    if "财务" in cleaned:
        return "财务"
    if "合同" in cleaned:
        return "合同中心"
    return cleaned[:32] or "unknown"


NEXT_ACTION_PATTERNS = (
    re.compile(r"下一合法动作\s*(?:仅为|为|=|＝|:|：)\s*([^；|。\n]+)"),
    re.compile(r"下一步仅允许\s*([^；|。\n]+)"),
    re.compile(r"下一步\s*(?:=|＝|:|：)\s*([^；|。\n]+)"),
)


def _clean_next_action(action: str) -> str:
    return action.strip().strip("`").strip(" ；。|")


def _extract_next_action(module: dict[str, str]) -> str:
    # Prefer the current ledger step over older evidence columns.  The runbook
    # can carry stale "开发" notes after a stage has advanced.
    ordered_cells = [
        str(module.get("当前步", "")),
        str(module.get("开发", "")),
        str(module.get("真待决", "")),
        " ".join(str(module.get(key, "")) for key in module),
    ]
    for cell in ordered_cells:
        for pattern in NEXT_ACTION_PATTERNS:
            match = pattern.search(cell)
            if match:
                action = _clean_next_action(match.group(1))
                if action:
                    return action
    return ""


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _stage_from_next_action(action: str) -> str:
    if not action:
        return ""
    compact = _compact(action)
    lowered = compact.lower()
    if any(
        marker in compact
        for marker in (
            "Code前门禁",
            "Code前核验",
            "Code门禁",
            "硬门禁",
            "外部护栏",
            "护栏blocker",
            "护栏 blocker",
            "护栏清单A-K",
            "Goldenfixture",
            "LinearUI核验",
        )
    ):
        return "code_preflight"
    if any(marker in compact for marker in ("PR拆解", "Code前准备")):
        return "pr_breakdown"
    if "Plan三方审查" in compact or "PlanReview" in compact or "planreview" in lowered:
        return "plan_review"
    if "Plan" in compact:
        return "plan"
    if any(marker in compact for marker in ("Contract三方审查", "ContractReview", "独立Contract")):
        return "contract_review"
    if "Contract" in compact:
        return "contract"
    if any(marker in compact for marker in ("Spec三方审查", "SpecReview", "独立Spec")):
        return "spec_review"
    if "Spec" in compact:
        return "spec"
    if any(marker in compact for marker in ("Story/Flow三方审查", "StoryFlow三方审查", "Story/FlowGate")):
        return "story_flow_review"
    if "Story/Flow" in compact or "StoryFlow" in compact:
        return "story_flow"
    if any(marker in compact for marker in ("Brief三方审查", "BriefReview")):
        return "brief_review"
    if "Brief" in compact:
        return "brief"
    if "Linear" in compact:
        return "linear_handoff"
    return f"next_action_{_sha_text(action)[:8]}"


def _external_blocker(target: dict[str, Any] | None) -> dict[str, Any]:
    if not target:
        return {
            "status": "clear",
            "kind": "none",
            "summary": "No external blocker.",
            "recommended_action": "Continue normally.",
            "auto_dispatch": True,
            "markers": [],
        }
    row = target.get("row") if isinstance(target.get("row"), dict) else {}
    text = " ".join(
        str(part or "")
        for part in (
            target.get("next_action"),
            target.get("stage"),
            row.get("当前步"),
            row.get("开发"),
            row.get("真待决"),
            row.get("contract"),
        )
    )
    compact = _compact(text)
    lowered = compact.lower()
    upstream_markers = [
        marker
        for marker in (
            "上游未发布",
            "上游不可用",
            "上游/外部",
            "不可安装",
            "未兼容",
            "Starlette",
            "starlette",
            "pip-audit",
        )
        if marker in compact or marker.lower() in lowered
    ]
    if upstream_markers and any(marker in compact for marker in ("漏洞", "修复版本", "1.x", "上游", "不可安装", "未发布")):
        exception_markers = [
            marker
            for marker in (
                "J例外",
                "风险例外",
                "例外/等待",
                "例外等待",
                "上游未发布例外",
                "J生产发布前复核",
                "guardrail-starlette-exception",
            )
            if marker in compact or marker.lower() in lowered
        ]
        if exception_markers:
            return {
                "status": "clear",
                "kind": "upstream_exception_recorded",
                "summary": "Dependency/security upstream blocker has a supervised risk exception for Code/PR prep.",
                "recommended_action": "Continue supervised Code/PR prep; re-check J before merge/deploy.",
                "auto_dispatch": True,
                "markers": upstream_markers + exception_markers,
            }
        return {
            "status": "blocked",
            "kind": "upstream_unavailable",
            "summary": "Dependency/security gate is blocked by an unavailable upstream fix version.",
            "recommended_action": "Wait for upstream compatible packages, make an explicit architecture decision, or record a supervised risk exception. Do not auto-dispatch Hermes workers.",
            "auto_dispatch": False,
            "markers": upstream_markers,
        }
    authority_markers = [
        marker
        for marker in (
            "振英直接触发",
            "振英直接调度",
            "直接触发/回收权威",
            "权威Round2",
            "权威Round",
            "权威审查",
            "来源未证",
            "来源未证明",
            "来源未能证明",
            "Hermesdelegate仅可算预检",
            "Hermes自己delegate",
            "预检/待定证据",
        )
        if marker in compact
    ]
    if authority_markers:
        return {
            "status": "blocked",
            "kind": "external_authority_required",
            "summary": "Runbook requires account-owner/direct authoritative review evidence; Hermes delegates cannot self-certify this gate.",
            "recommended_action": "Park this module until authoritative Codex/DeepSeek/Zhipu evidence is provided, and continue other runnable modules.",
            "auto_dispatch": False,
            "markers": authority_markers,
        }
    external_markers = [
        marker
        for marker in (
            "外部权限",
            "外部护栏",
            "外部blocker",
            "外部 blocker",
            "护栏blocker",
            "护栏 blocker",
            "服务端取证",
            "GitHub服务端",
            "Settings/API",
            "secrets取证",
            "secrets权限",
            "secret取证",
            "secret权限",
            "staging取证",
            "staging检查",
            "staging验证",
            "staging权限",
            "独立DB",
            "独立DB/redis",
            "token收窄",
            "权限处理",
            "振英或supervised",
            "振英或supervised会话",
            "人工风险例外",
        )
        if marker in compact
    ]
    if external_markers:
        return {
            "status": "blocked",
            "kind": "external_permission_required",
            "summary": "Runbook next action needs external permissions, server-side evidence, secrets/staging checks, or supervised human decision.",
            "recommended_action": "Handle in a supervised session or by the account owner; do not auto-dispatch Hermes workers.",
            "auto_dispatch": False,
            "markers": external_markers,
        }
    return {
        "status": "clear",
        "kind": "none",
        "summary": "No external blocker.",
        "recommended_action": "Continue normally.",
        "auto_dispatch": True,
        "markers": [],
    }


def _classify_stage(module: dict[str, str], next_action: str | None = None) -> str:
    current = module.get("当前步", "")
    dev = module.get("开发", "")
    story = module.get("故事", "")
    spec = module.get("spec", "")
    contract = module.get("contract", "")
    linear = module.get("Linear", "")
    row_text = " ".join(str(module.get(key, "")) for key in module)
    contract_gate_text = " ".join((current, dev, contract))
    next_action_stage = _stage_from_next_action(next_action if next_action is not None else _extract_next_action(module))
    if next_action_stage in {"plan_review", "pr_breakdown", "code_preflight"} and any(
        marker in row_text
        for marker in (
            "Plan Draft",
            "Plan=`docs/plans/",
            "Plan Review final",
            "Plan三方审查",
        )
    ):
        return next_action_stage
    if next_action_stage == "plan" and any(
        marker in row_text
        for marker in (
            "Contract Gate闭合",
            "Contract Gate已闭合",
            "Contract Gate Final",
            "Contract Gate已三方确认PASS",
            "Contract Round1 repair三方PASS",
            "Contract三方审查PASS",
            "Contract三方门禁PASS",
            "Contract 三方审查 PASS",
            "Contract 三方门禁PASS",
        )
    ):
        return "plan"
    if any(
        marker in contract_gate_text
        for marker in (
            "待Contract",
            "待 Contract",
            "待Contract三方",
            "待 Contract 三方",
            "Contract三方审查",
            "Contract 三方审查",
            "等DeepSeek",
            "等智谱",
            "等Qwen",
            "等回",
            "P1吸收",
            "P1 吸收",
            "未进Plan",
            "未进 Plan",
        )
    ):
        return "contract_review"
    if "PASS with P1" in contract_gate_text and not any(
        marker in contract_gate_text for marker in ("P1 absorbed", "P1已吸收", "P1 已吸收")
    ):
        return "contract_review"
    if "Contract Round" in contract_gate_text and any(
        marker in contract_gate_text for marker in ("待", "等", "等待", "P1吸收", "P1 吸收", "未进Plan", "未进 Plan")
    ):
        return "contract_review"
    if next_action_stage:
        return next_action_stage
    if any(
        marker in row_text
        for marker in (
            "待开发拆PR",
            "待拆PR",
            "待PR拆分",
            "待真缺口拆分",
            "待缺口拆分",
            "真缺口拆分",
            "下一合法动作=PR拆解",
            "下一合法动作 = PR拆解",
            "下一步仅允许PR拆解",
            "下一步仅允许 PR拆解",
            "PR拆解/Code前准备",
            "PR 拆解/Code 前准备",
            "Code前准备",
            "Code 前准备",
        )
    ):
        return "pr_breakdown"
    if any(
        marker in row_text
        for marker in ("下一合法动作=Plan", "下一合法动作 = Plan", "下一步仅允许Plan", "下一步仅允许 Plan", "可进入Plan", "可进入 Plan")
    ):
        return "plan"
    if "待开发" in dev and not any(marker in row_text for marker in ("Plan Draft", "Plan=", "Plan Gate", "Plan三方")):
        return "plan"
    if "FAIL" in row_text and "Spec" in row_text:
        return "spec_revision"
    if any(marker in row_text for marker in ("Spec三方审查", "Spec 三方审查", "待Spec三方审查", "待 Spec 三方审查")):
        return "spec_review"
    if "Spec Draft" in row_text and any(marker in row_text for marker in ("待", "下一合法动作", "审查")):
        return "spec_review"
    if "Spec审查" in row_text or "Spec Review" in row_text:
        return "spec_review"
    if ("待DeepSeek" in row_text or "待Zhipu" in row_text or "待智谱" in row_text or "待Qwen" in row_text) and "Spec" in row_text:
        return "spec_review"
    if "待审" in current or "等待" in current or "复审" in current:
        return "review"
    if (
        "下一合法动作=Spec" in current
        or "下一合法动作仅为 Spec" in dev
        or "Story/Flow Gate 3/3 PASS" in current
    ):
        return "spec"
    if "可进入Spec" in current or "可进入Spec" in dev or (story.startswith("✅") and not spec.startswith("✅")):
        return "spec"
    if "待Story" in dev or ("Story/Flow" in dev and "Spec" not in dev):
        return "story_flow"
    if "Spec Round" in current and "待三方审查" in current:
        return "spec_review"
    if "Spec Round" in dev and ("待三方审查" in dev or "待三方" in dev):
        return "spec_review"
    if "待Contract三方审查" in current or "待Contract三方审查" in dev:
        return "contract_review"
    if "可进入Contract" in current or "可进入Contract" in dev or (spec.startswith("✅") and not contract.startswith("✅")):
        return "contract"
    if "可启" in current or all(not (module.get(col, "").startswith("✅")) for col in ("故事", "spec", "contract")):
        return "brief"
    if spec.startswith("✅") and contract.startswith("✅") and not linear.startswith("✅"):
        return "linear_handoff"
    if spec.startswith("✅") and contract.startswith("✅") and "待" in dev:
        return "done_for_spec"
    return "inspect"


def _is_module_done_for_spec(module: dict[str, str]) -> bool:
    if _is_supervised_code_pr_ready(module):
        return True
    current = module.get("当前步", "")
    dev = module.get("开发", "")
    spec = module.get("spec", "")
    contract = module.get("contract", "")
    row_text = " ".join(str(module.get(key, "")) for key in module)
    if spec.startswith("✅") and contract.startswith("✅"):
        if any(
            marker in row_text
            for marker in (
                "下一合法动作",
                "下一步仅允许",
                "待开发",
                "待开发拆PR",
                "待拆PR",
                "待PR拆分",
                "待真缺口拆分",
                "待缺口拆分",
                "真缺口拆分",
                "待三方",
                "待Spec三方",
                "Spec三方审查",
                "Spec 三方审查",
                "Spec审查",
                "Spec Review",
                "待Contract三方",
                "待 Contract 三方",
                "Contract三方审查",
                "待DeepSeek",
                "待Zhipu",
                "待智谱",
                "待Qwen",
                "等DeepSeek",
                "等Zhipu",
                "等智谱",
                "等Qwen",
                "等回",
                "等待",
                "复审",
                "重审",
                "打回",
                "FAIL",
                "BLOCKED",
                "P1吸收",
                "P1 吸收",
                "未进Plan",
                "未进 Plan",
                "可进入Contract",
                "可进入 Contract",
                "可进入Plan",
                "可进入 Plan",
                "PR拆解",
                "Code前",
            )
        ):
            return False
        return True
    pending_markers = (
        "下一合法动作",
        "待开发",
        "待开发拆PR",
        "待拆PR",
        "待PR拆分",
        "待真缺口拆分",
        "待缺口拆分",
        "真缺口拆分",
        "待三方",
        "待Spec三方",
        "Spec三方审查",
        "Spec 三方审查",
        "Spec审查",
        "Spec Review",
        "待Contract三方",
        "待 Contract 三方",
        "Contract三方",
        "待DeepSeek",
        "待Zhipu",
        "待智谱",
        "待Qwen",
        "等DeepSeek",
        "等Zhipu",
        "等智谱",
        "等Qwen",
        "等回",
        "等待",
        "复审",
        "重审",
        "打回",
        "FAIL",
        "P1吸收",
        "P1 吸收",
        "未进Plan",
        "未进 Plan",
        "可进入Spec",
        "可进入Contract",
        "1/3",
        "2/3",
    )
    if any(marker in row_text for marker in pending_markers):
        return False
    if "Spec Round" in current and "待三方审查" in current:
        return False
    if "Spec Round" in dev and ("待三方审查" in dev or "待三方" in dev):
        return False
    if "待Contract三方审查" in current or "待Contract三方审查" in dev:
        return False
    if any(marker in current for marker in ("可启", "等待", "待审", "复审", "可进入Spec")):
        return False
    if any(marker in current for marker in ("可进入Contract",)):
        return False
    if any(marker in dev for marker in ("待Story", "Story/Flow", "可进入Spec", "可进入Contract")):
        return False
    return False


def _is_supervised_code_pr_ready(module: dict[str, str]) -> bool:
    """True when the SPEC watchdog has handed the module to supervised Code/PR.

    The runbook may keep a "next legal action" after a module leaves the
    SPEC-only world.  Without this guard, the watchdog retries the same
    already-satisfied next_action target until the attempt limit freezes it.
    """

    next_action = _compact(_extract_next_action(module))
    if not any(marker in next_action for marker in SUPERVISED_CODE_PR_ACTION_MARKERS):
        return False
    row_text = _compact(" ".join(str(module.get(key, "") or "") for key in module))
    has_gate_evidence = any(marker in row_text for marker in SUPERVISED_CODE_PR_GATE_MARKERS)
    has_handoff_evidence = any(marker in row_text for marker in SUPERVISED_CODE_PR_HANDOFF_MARKERS)
    return has_gate_evidence and has_handoff_evidence


def _supervised_ready_modules(modules: list[dict[str, str]]) -> list[dict[str, str]]:
    ready: list[dict[str, str]] = []
    for module in modules:
        if not _is_supervised_code_pr_ready(module):
            continue
        ready.append(
            {
                "module": module.get("模块", ""),
                "stage": "supervised_code_pr_ready",
                "next_action": _extract_next_action(module),
            }
        )
    return ready


def _clear_satisfied_attempts(state: dict[str, Any], satisfied: list[dict[str, str]]) -> list[dict[str, Any]]:
    attempts = state.setdefault("attempts", {})
    attempt_fingerprints = state.setdefault("attempt_fingerprints", {})
    resets: list[dict[str, Any]] = []
    for item in satisfied:
        prefix = f"{_module_slug(item.get('module', ''))}:"
        if prefix == ":":
            continue
        for key in list(attempts):
            if not key.startswith(prefix):
                continue
            previous_attempts = int(attempts.pop(key, 0) or 0)
            previous_fingerprint = str(attempt_fingerprints.pop(key, "") or "")
            resets.append(
                {
                    "stage": key,
                    "previous_attempts": previous_attempts,
                    "previous_fingerprint": previous_fingerprint,
                    "current_fingerprint": "satisfied",
                    "reason": "supervised Code/PR handoff ready",
                }
            )
    return resets


def _eligible_modules(modules: list[dict[str, str]]) -> list[dict[str, Any]]:
    order = ["④商务", "③财务", "⑤产品/成本", "⑥销售B2B", "①生产/交付", "②人力/KPI", "⑦门店B2C", "⑧专项制度"]
    indexed = {module.get("模块", ""): module for module in modules}
    result: list[dict[str, Any]] = []
    for name in order:
        module = indexed.get(name)
        if not module or _is_module_done_for_spec(module):
            continue
        next_action = _extract_next_action(module)
        result.append({"module": module.get("模块", ""), "stage": _classify_stage(module, next_action), "next_action": next_action, "row": module})
    for module in modules:
        name = module.get("模块", "")
        if name in order or _is_module_done_for_spec(module):
            continue
        next_action = _extract_next_action(module)
        result.append({"module": name, "stage": _classify_stage(module, next_action), "next_action": next_action, "row": module})
    return result


def _select_dispatch_target(eligible: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, Any]]:
    parked: list[dict[str, Any]] = []
    first_blocker: dict[str, Any] | None = None
    selected: dict[str, Any] | None = None
    selected_blocker: dict[str, Any] | None = None
    for candidate in eligible:
        blocker = _external_blocker(candidate)
        if blocker.get("status") == "blocked":
            if first_blocker is None:
                first_blocker = blocker
            parked.append(
                {
                    "module": candidate.get("module", ""),
                    "stage": candidate.get("stage", ""),
                    "next_action": candidate.get("next_action", ""),
                    "kind": blocker.get("kind", "external"),
                    "summary": blocker.get("summary", ""),
                    "recommended_action": blocker.get("recommended_action", ""),
                    "markers": blocker.get("markers", []),
                }
            )
            continue
        if selected is None:
            selected = candidate
            selected_blocker = blocker
    if selected is not None:
        return selected, parked, selected_blocker or _external_blocker(selected)
    if eligible:
        return eligible[0], parked, first_blocker or _external_blocker(eligible[0])
    return None, parked, _external_blocker(None)


def _git_changed(repo: Path) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z"],
            cwd=repo,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except Exception:
        result = None
    paths: list[str] = []
    if result is None or result.returncode != 0:
        return paths
    entries = result.stdout.split(b"\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        status = entry[:2].decode("ascii", errors="ignore")
        raw = entry[3:] if len(entry) > 3 else b""
        path = raw.decode("utf-8", errors="surrogateescape")
        if status and status[0] in {"R", "C"} and index < len(entries):
            index += 1
        abs_path = repo / path
        if path.endswith("/") and abs_path.is_dir():
            for child in sorted(abs_path.rglob("*")):
                if child.is_file() or child.is_symlink():
                    paths.append(str(child.relative_to(repo)))
        else:
            paths.append(path)
    return paths


def _git_changed_entries(repo: Path) -> list[dict[str, str]]:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z"],
            cwd=repo,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except Exception:
        result = None
    if result is None or result.returncode != 0:
        return []
    entries = result.stdout.split(b"\0")
    parsed: list[dict[str, str]] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        status = entry[:2].decode("ascii", errors="ignore")
        raw = entry[3:] if len(entry) > 3 else b""
        path = raw.decode("utf-8", errors="surrogateescape")
        if status and status[0] in {"R", "C"} and index < len(entries):
            # Keep the destination path; the next NUL entry is the source.
            index += 1
        abs_path = repo / path
        if path.endswith("/") and abs_path.is_dir():
            for child in sorted(abs_path.rglob("*")):
                if child.is_file() or child.is_symlink():
                    parsed.append({"path": str(child.relative_to(repo)), "status": status})
        else:
            parsed.append({"path": path, "status": status})
    return parsed


def _is_allowed_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return any(normalized == prefix or normalized.startswith(prefix) for prefix in ALLOWED_PREFIXES)


def _is_forbidden_path(path: str) -> bool:
    normalized = "/" + path.replace("\\", "/").lstrip("/")
    bare = normalized.lstrip("/")
    return any(bare.startswith(prefix) for prefix in FORBIDDEN_PREFIXES) or any(part in normalized for part in FORBIDDEN_PARTS)


def _diff_guard(repo: Path, baseline_out_of_scope: list[str]) -> dict[str, Any]:
    changed = _git_changed(repo)
    out_of_scope = [path for path in changed if not _is_allowed_path(path) or _is_forbidden_path(path)]
    baseline = set(baseline_out_of_scope)
    new_out_of_scope = [path for path in out_of_scope if path not in baseline]
    return {
        "changed": changed,
        "out_of_scope": out_of_scope,
        "new_out_of_scope": new_out_of_scope,
        "passed": not new_out_of_scope,
    }


def _row_for_module(runbook_data: dict[str, Any], module_slug: str) -> dict[str, str]:
    for module in runbook_data.get("modules", []):
        if _module_slug(str(module.get("模块", ""))) == module_slug:
            return module
    return {}


def _module_tokens(module_slug: str, row: dict[str, str]) -> list[str]:
    row_text = " ".join(str(value or "") for value in row.values())
    tokens = {
        module_slug,
        module_slug.replace("/", ""),
        str(row.get("模块") or ""),
        str(row.get("模块") or "").replace("/", ""),
    }
    tokens.update(re.findall(r"FSN-\d+", row_text))
    match = re.match(r"([①②③④⑤⑥⑦⑧⑨0-9]+)", str(row.get("模块") or ""))
    if match:
        tokens.add(f"Tier1_{match.group(1)}")
    return [token for token in tokens if token]


def _active_task_module_stage(task: dict[str, Any]) -> tuple[str, str]:
    key = str(task.get("idempotency_key") or "")
    parts = key.split(":")
    if len(parts) >= 3 and parts[0] == "fesun-nine-spec":
        return parts[1], parts[2]
    title = str(task.get("title") or "")
    match = re.search(r"Fesun SPEC watchdog:\s*(.+?)\s+([a-zA-Z0-9_]+)\s*$", title)
    if match:
        return _module_slug(match.group(1)), match.group(2)
    return "", ""


def _task_log_tail(task_id: str, board: str) -> str:
    from hermes_cli import kanban_db as kb

    try:
        return kb.read_worker_log(task_id, board=board, tail_bytes=CLOSURE_LOG_TAIL_BYTES) or ""
    except Exception:
        return ""


def _log_has_completion_signal(text: str) -> bool:
    if not text:
        return False
    return any(marker in text for marker in CLOSURE_COMPLETION_MARKERS)


def _stage_advanced(active_stage: str, row: dict[str, str]) -> bool:
    if not active_stage:
        return False
    row_stage = _classify_stage(row, _extract_next_action(row)) if row else ""
    if not row_stage or row_stage == active_stage:
        return False
    return STAGE_ORDER.get(row_stage, -1) > STAGE_ORDER.get(active_stage, -1)


def _source_stage_for_target(stage: str) -> str:
    if stage.endswith("_review"):
        return stage.removesuffix("_review")
    return stage


def _path_matches_stage(path: str, stage: str) -> bool:
    normalized = path.replace("\\", "/")
    base_stage = _source_stage_for_target(stage)
    stage_prefixes = {
        "brief": ("docs/reviews/brief/", "docs/reviews/briefs/"),
        "story_flow": ("docs/stories/", "docs/flows/", "docs/reviews/story_flow/"),
        "spec": ("docs/specs/", "docs/reviews/spec/"),
        "spec_revision": ("docs/specs/", "docs/reviews/spec/"),
        "contract": ("docs/contracts/", "docs/reviews/contract/"),
        "plan": ("docs/plans/", "docs/reviews/plan/"),
        "plan_review": ("docs/plans/", "docs/reviews/plan/"),
        "pr_breakdown": ("docs/plans/", "docs/reviews/plan/"),
        "code_preflight": ("docs/plans/", "docs/reviews/plan/"),
    }
    prefixes = stage_prefixes.get(base_stage)
    if not prefixes:
        return True
    return any(normalized.startswith(prefix) for prefix in prefixes)


def _has_primary_stage_artifact(paths: list[str], stage: str) -> bool:
    base_stage = _source_stage_for_target(stage)
    primary_prefixes = {
        "brief": ("docs/reviews/briefs/",),
        "story_flow": ("docs/stories/", "docs/flows/"),
        "spec": ("docs/specs/",),
        "spec_revision": ("docs/specs/",),
        "contract": ("docs/contracts/",),
        "plan": ("docs/plans/Tier",),
        "plan_review": ("docs/plans/Tier",),
        "pr_breakdown": ("docs/plans/Tier",),
        "code_preflight": ("docs/reviews/plan/",),
    }
    prefixes = primary_prefixes.get(base_stage)
    if not prefixes:
        return True
    return any(path.replace("\\", "/").startswith(prefix) for path in paths for prefix in prefixes)


def _related_changed_paths(repo: Path, runbook: Path, module_slug: str, row: dict[str, str], stage: str = "") -> list[str]:
    tokens = _module_tokens(module_slug, row)
    runbook_rel = ""
    try:
        runbook_rel = str(runbook.relative_to(repo))
    except ValueError:
        runbook_rel = str(runbook)
    related: list[str] = []
    for entry in _git_changed_entries(repo):
        path = entry["path"]
        status = entry["status"]
        if "D" in status:
            continue
        if not _is_allowed_path(path) or _is_forbidden_path(path):
            continue
        normalized = path.replace("\\", "/")
        if normalized == runbook_rel:
            related.append(path)
            continue
        if stage and not _path_matches_stage(normalized, stage):
            continue
        if any(token and token in normalized for token in tokens):
            related.append(path)
    return sorted(dict.fromkeys(related))


def _first_fsn(row: dict[str, str], paths: list[str]) -> str:
    haystack = " ".join([*(str(value or "") for value in row.values()), *paths])
    match = re.search(r"FSN-\d+", haystack)
    return match.group(0) if match else ""


def _commit_related_paths(repo: Path, paths: list[str], message: str) -> dict[str, Any]:
    staged_before = _run(["git", "diff", "--cached", "--name-only"], cwd=repo)
    pre_staged = [line for line in staged_before.get("stdout", "").splitlines() if line.strip()]
    if pre_staged:
        return {
            "ok": False,
            "status": "skipped",
            "reason": "preexisting_staged_changes",
            "pre_staged": pre_staged,
        }
    add = _run(["git", "add", "--", *paths], cwd=repo)
    if not add.get("ok"):
        return {"ok": False, "status": "failed", "reason": "git_add_failed", "command": add}
    check = _run(["git", "diff", "--cached", "--check", "--", *paths], cwd=repo)
    if not check.get("ok"):
        _run(["git", "restore", "--staged", "--", *paths], cwd=repo)
        return {"ok": False, "status": "failed", "reason": "diff_check_failed", "command": check}
    staged = _run(["git", "diff", "--cached", "--name-only", "--", *paths], cwd=repo)
    staged_paths = [line for line in staged.get("stdout", "").splitlines() if line.strip()]
    if not staged_paths:
        return {"ok": True, "status": "no_change", "paths": []}
    commit = _run(["git", "commit", "-m", message], cwd=repo, timeout=60)
    if not commit.get("ok"):
        _run(["git", "restore", "--staged", "--", *paths], cwd=repo)
        return {"ok": False, "status": "failed", "reason": "commit_failed", "command": commit}
    return {
        "ok": True,
        "status": "committed",
        "paths": staged_paths,
        "message": message,
        "commit": _latest_commit(repo),
        "stdout": commit.get("stdout", ""),
    }


def _complete_closure_task(
    *,
    repo: Path,
    board: str,
    task: dict[str, Any],
    module_slug: str,
    stage: str,
    commit_info: dict[str, Any],
    paths: list[str],
    row: dict[str, str],
) -> dict[str, Any]:
    from hermes_cli import kanban_db as kb

    task_id = str(task.get("id") or "")
    fsn = _first_fsn(row, paths)
    commit_hash = commit_info.get("commit") or _latest_commit(repo)
    result = (
        f"{module_slug} {stage} closure-stall recovered by Watchdog. "
        f"commit={commit_hash}; paths={len(paths)}; "
        "worker had completion evidence but did not close Kanban."
    )
    summary = (
        f"Closure-stall recovered for {module_slug} / {stage}. "
        f"Evidence committed as {commit_hash}. "
        "Next watchdog tick may continue from the runbook target."
    )
    metadata = {
        "closure_stall_recovered": True,
        "module": module_slug,
        "stage": stage,
        "fsn": fsn,
        "commit": commit_hash,
        "changed_files": paths,
        "commit_message": commit_info.get("message", ""),
    }
    with kb.connect(board=board) as conn:
        reclaimed = kb.reclaim_task(
            conn,
            task_id,
            reason="closure-stall: evidence committed by project watchdog",
        )
        completed = kb.complete_task(
            conn,
            task_id,
            result=result,
            summary=summary,
            metadata=metadata,
        )
    return {"reclaimed": reclaimed, "completed": completed, "result": result, "metadata": metadata}


def _recover_closure_stalls(
    *,
    repo: Path,
    runbook: Path,
    runbook_data: dict[str, Any],
    board: str,
    active: list[dict[str, Any]],
    diff_guard: dict[str, Any],
    closure_stall_seconds: int,
    enabled: bool,
) -> list[dict[str, Any]]:
    if not enabled or not diff_guard.get("passed"):
        return []
    now = int(time.time())
    recoveries: list[dict[str, Any]] = []
    for task in active:
        if task.get("status") != "running":
            continue
        created_at = int(task.get("created_at") or 0)
        if closure_stall_seconds > 0 and created_at and now - created_at < closure_stall_seconds:
            continue
        task_id = str(task.get("id") or "")
        module_slug, stage = _active_task_module_stage(task)
        if not module_slug:
            continue
        row = _row_for_module(runbook_data, module_slug)
        log_text = _task_log_tail(task_id, board)
        completion_signal = _log_has_completion_signal(log_text)
        advanced_signal = _stage_advanced(stage, row)
        if not completion_signal and not advanced_signal:
            continue
        paths = _related_changed_paths(repo, runbook, module_slug, row, stage=stage)
        if not paths:
            continue
        fsn = _first_fsn(row, paths)
        message_subject = fsn or module_slug
        commit_message = f"docs(fesun): {message_subject} close {stage} gate"
        commit_info = _commit_related_paths(repo, paths, commit_message)
        recovery = {
            "task_id": task_id,
            "module": module_slug,
            "stage": stage,
            "paths": paths,
            "signals": {
                "completion_log": completion_signal,
                "stage_advanced": advanced_signal,
            },
            "commit": commit_info,
            "status": "committed" if commit_info.get("status") == "committed" else "skipped",
        }
        if commit_info.get("ok") and commit_info.get("status") in {"committed", "no_change"}:
            completion = _complete_closure_task(
                repo=repo,
                board=board,
                task=task,
                module_slug=module_slug,
                stage=stage,
                commit_info=commit_info,
                paths=paths,
                row=row,
            )
            recovery["completion"] = completion
            recovery["status"] = "completed" if completion.get("completed") else "completion_failed"
        recoveries.append(recovery)
    return recoveries


def _recover_orphan_closure_evidence(
    *,
    repo: Path,
    runbook: Path,
    target: dict[str, Any] | None,
    active: list[dict[str, Any]],
    diff_guard: dict[str, Any],
    enabled: bool,
) -> list[dict[str, Any]]:
    if not enabled or not target or not diff_guard.get("passed"):
        return []
    module_slug = _module_slug(str(target.get("module") or ""))
    row = target.get("row") if isinstance(target.get("row"), dict) else {}
    target_stage = str(target.get("stage") or "")
    if not module_slug or not row or not target_stage:
        return []
    source_stage = _source_stage_for_target(target_stage)
    for task in active:
        active_module, active_stage = _active_task_module_stage(task)
        if active_module == module_slug and active_stage == source_stage:
            return []
    paths = _related_changed_paths(repo, runbook, module_slug, row, stage=source_stage)
    if not paths:
        return []
    active_target_stage = any(
        _active_task_module_stage(task) == (module_slug, target_stage)
        for task in active
    )
    if active_target_stage and not _has_primary_stage_artifact(paths, source_stage):
        return []
    fsn = _first_fsn(row, paths)
    message_subject = fsn or module_slug
    commit_message = f"docs(fesun): {message_subject} close {source_stage} gate"
    commit_info = _commit_related_paths(repo, paths, commit_message)
    return [
        {
            "task_id": f"orphan:{module_slug}:{source_stage}",
            "module": module_slug,
            "stage": source_stage,
            "target_stage": target_stage,
            "paths": paths,
            "signals": {"orphan_evidence": True},
            "commit": commit_info,
            "status": "completed" if commit_info.get("status") in {"committed", "no_change"} else "skipped",
        }
    ]


def _blocked_diagnosis(diff_guard: dict[str, Any], symlink_guard: dict[str, Any]) -> dict[str, Any]:
    new_paths = [str(path) for path in diff_guard.get("new_out_of_scope", [])]
    if symlink_guard.get("bad_symlinks"):
        return {
            "status": "blocked",
            "kind": "hard_violation",
            "severity": "critical",
            "summary": "Allowed docs roots contain symlinks that resolve outside the repo.",
            "paths": symlink_guard.get("bad_symlinks", []),
            "recommended_action": "Remove or replace the bad symlinks before continuing.",
            "auto_continue": False,
        }
    if not new_paths:
        return {
            "status": "clear",
            "kind": "none",
            "severity": "info",
            "summary": "No guard block.",
            "paths": [],
            "recommended_action": "Continue normally.",
            "auto_continue": True,
        }

    hard_paths = [path for path in new_paths if _is_forbidden_path(path) or not path.startswith(DOCS_POLICY_GAP_PREFIXES)]
    docs_gap_paths = [path for path in new_paths if path not in hard_paths and path.startswith(DOCS_POLICY_GAP_PREFIXES)]
    if hard_paths:
        return {
            "status": "blocked",
            "kind": "hard_violation",
            "severity": "critical",
            "summary": "New changes touched business code, CI/deploy/secrets, or non-doc paths.",
            "paths": hard_paths,
            "policy_gap_paths": docs_gap_paths,
            "recommended_action": "Stop worker output and inspect/revert or explicitly approve these paths; do not auto-dispatch.",
            "auto_continue": False,
        }
    return {
        "status": "blocked",
        "kind": "probable_policy_gap",
        "severity": "warning",
        "summary": "Only docs-only paths are blocked; this is likely a missing allowlist rule or wrong documentation landing zone.",
        "paths": docs_gap_paths,
        "recommended_action": "Decide whether these docs paths belong to the runbook flow. If yes, update the Watchdog allowlist and tests; if not, move the files to an allowed docs directory.",
        "auto_continue": False,
    }


def _symlink_guard(repo: Path) -> dict[str, Any]:
    bad: list[dict[str, str]] = []
    for prefix in ("docs/specs", "docs/stories", "docs/flows", "docs/contracts", "docs/plans", "docs/reviews", "docs/handoff"):
        root = repo / prefix
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_symlink():
                continue
            try:
                resolved = path.resolve()
            except OSError:
                resolved = Path("/__broken__")
            try:
                resolved.relative_to(repo.resolve())
            except ValueError:
                bad.append({"path": str(path.relative_to(repo)), "target": str(resolved)})
    return {"passed": not bad, "bad_symlinks": bad}


def _active_tasks(board: str) -> list[dict[str, Any]]:
    from hermes_cli import kanban_db as kb

    with kb.connect(board=board) as conn:
        rows = conn.execute(
            """
            SELECT id, title, status, created_at, last_heartbeat_at, worker_pid, idempotency_key
              FROM tasks
             WHERE (idempotency_key LIKE 'fesun-nine-spec:%'
                    OR idempotency_key LIKE 'fesun-nine-spec-recovery:%')
               AND status IN ('todo', 'scheduled', 'ready', 'running', 'review')
             ORDER BY created_at DESC
             LIMIT 20
            """
        ).fetchall()
    return [
        {
            "id": str(row["id"]),
            "title": str(row["title"]),
            "status": str(row["status"]),
            "created_at": int(row["created_at"] or 0),
            "last_heartbeat_at": int(row["last_heartbeat_at"] or 0),
            "worker_pid": int(row["worker_pid"] or 0),
            "idempotency_key": str(row["idempotency_key"] or ""),
        }
        for row in rows
    ]


def _review_output_exists(repo: Path, body: str) -> bool:
    match = re.search(r"输出文件[:：]\s*(.+)", body)
    if not match:
        return False
    raw = match.group(1).strip().strip("`").strip()
    output = Path(raw)
    if not output.is_absolute():
        output = repo / output
    try:
        return output.exists() and output.stat().st_size > 0
    except OSError:
        return False


def _blocked_review_tasks(board: str, repo: Path) -> list[dict[str, Any]]:
    from hermes_cli import kanban_db as kb

    with kb.connect(board=board) as conn:
        rows = conn.execute(
            """
            SELECT id, title, body, status, assignee, created_at, last_failure_error,
                   consecutive_failures, idempotency_key, skills
              FROM tasks
             WHERE title LIKE 'Fesun SPEC watchdog:%'
               AND status = 'blocked'
             ORDER BY created_at ASC
            """
        ).fetchall()
        recovery_rows = conn.execute(
            """
            SELECT id, status, idempotency_key
              FROM tasks
             WHERE idempotency_key LIKE ?
               AND status != 'archived'
            """,
            (f"{REVIEW_RECOVERY_PREFIX}:%",),
        ).fetchall()
    recoveries = [
        {
            "id": str(row["id"]),
            "status": str(row["status"]),
            "idempotency_key": str(row["idempotency_key"] or ""),
        }
        for row in recovery_rows
    ]
    result: list[dict[str, Any]] = []
    for row in rows:
        title = str(row["title"] or "")
        if not any(marker in title for marker in REVIEW_STAGE_MARKERS):
            continue
        body = str(row["body"] or "")
        if _review_output_exists(repo, body):
            continue
        task_id = str(row["id"])
        recovery_key = f"{REVIEW_RECOVERY_PREFIX}:{task_id}"
        existing = [
            item
            for item in recoveries
            if item["idempotency_key"] == recovery_key
            or item["idempotency_key"].startswith(recovery_key + ":")
        ]
        if any(item["status"] in ACTIVE_STATUSES or item["status"] in TERMINAL_STATUSES for item in existing):
            continue
        result.append(
            {
                "id": task_id,
                "title": title,
                "body": body,
                "assignee": str(row["assignee"] or ""),
                "created_at": int(row["created_at"] or 0),
                "last_failure_error": str(row["last_failure_error"] or ""),
                "consecutive_failures": int(row["consecutive_failures"] or 0),
                "idempotency_key": str(row["idempotency_key"] or ""),
                "skills": str(row["skills"] or ""),
                "recovery_key": recovery_key,
            }
        )
    return result


def _review_recovery_body(repo: Path, original: dict[str, Any]) -> str:
    return (
        "你是 Fesun SPEC watchdog 的审查补偿 worker。上一条同角色审查任务启动失败，"
        "本任务用于补齐 Gate 缺口。\n\n"
        "严格规则：\n"
        "- SPEC-only / review-only，只读输入并写指定 review 文件。\n"
        "- 不写业务代码、不开 PR、不 push、不触发 CI/bench/staging/部署、不读 secrets。\n"
        "- 不使用 `ai-collab-dev-law` 或任何额外 skill；按正文要求直接完成审查。\n"
        "- 必须给 PASS/FAIL、P0/P1/P2、file:line 证据、是否允许进入下一层。\n"
        "- 原任务若因 Unknown skill / agent crash / gave_up blocked 失败，不代表审查失败；必须重新审查事实。\n\n"
        f"repo: {repo}\n"
        f"原 blocked task: {original.get('id')} · {original.get('title')}\n"
        f"原失败: {original.get('last_failure_error') or 'unknown'}\n"
        f"原 skills: {original.get('skills') or 'none'}\n\n"
        "以下是原审查任务正文，请按它要求读取输入和写入输出：\n\n"
        f"{original.get('body') or ''}"
    )


def _create_review_recovery_task(
    *,
    repo: Path,
    board: str,
    assignee: str | None,
    goal_max_turns: int,
    original: dict[str, Any],
) -> tuple[str, bool, dict[str, Any]]:
    return _create_goal_task(
        title=f"{original['title']} — recovery",
        body=_review_recovery_body(repo, original),
        board=board,
        assignee=assignee or DEFAULT_ASSIGNEE,
        goal_max_turns=goal_max_turns,
        workspace_path=str(repo),
        idempotency_key=str(original["recovery_key"]),
    )


def _latest_commit(repo: Path) -> str:
    result = _run(["git", "rev-parse", "HEAD"], cwd=repo)
    return result.get("stdout", "")[:12] if result.get("ok") else ""


def _spec_fingerprint(repo: Path) -> str:
    roots = [repo / "docs" / "specs", repo / "docs" / "reviews"]
    parts: list[str] = []
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.md")):
            try:
                stat = path.stat()
            except OSError:
                continue
            parts.append(f"{path.relative_to(repo)}:{stat.st_mtime_ns}:{stat.st_size}")
    return _sha_text("\n".join(parts))


def _progress_fingerprint(repo: Path, runbook_data: dict[str, Any], active_tasks: list[dict[str, Any]]) -> str:
    task_bits = [
        f"{task['id']}:{task['status']}:{task.get('last_heartbeat_at', 0)}"
        for task in active_tasks
        if task.get("status") in ACTIVE_STATUSES
    ]
    payload = {
        "runbook": runbook_data.get("sha"),
        "specs": _spec_fingerprint(repo),
        "commit": _latest_commit(repo),
        "active": task_bits,
    }
    return _sha_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _mapping_validation(repo: Path, module_name: str) -> dict[str, Any]:
    slug = _module_slug(module_name)
    candidates = [repo / "docs" / "specs" / slug]
    if slug != module_name:
        candidates.append(repo / "docs" / "specs" / module_name)
    files: list[Path] = []
    for root in candidates:
        if root.exists():
            files.extend(sorted(root.glob("*.md")))
    if not files:
        return {"status": "missing", "files": [], "missing": ["spec_dir"]}
    text = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in files)
    required_markers = ["出处", "故事", "蓝图"]
    missing = [marker for marker in required_markers if marker not in text]
    return {
        "status": "passed" if not missing else "incomplete",
        "files": [str(path.relative_to(repo)) for path in files],
        "missing": missing,
    }


def _attempt_key(module: str, stage: str) -> str:
    return f"{_module_slug(module)}:{stage}"


def _reset_attempt_if_fingerprint_changed(
    state: dict[str, Any],
    attempt_key: str,
    fingerprint: str,
) -> dict[str, Any] | None:
    attempts = state.setdefault("attempts", {})
    attempt_fingerprints = state.setdefault("attempt_fingerprints", {})
    previous_fingerprint = str(attempt_fingerprints.get(attempt_key) or "")
    if not attempts.get(attempt_key):
        attempt_fingerprints[attempt_key] = fingerprint
        return None
    if previous_fingerprint == fingerprint:
        return None
    previous_attempts = int(attempts.get(attempt_key, 0))
    attempts[attempt_key] = 0
    attempt_fingerprints[attempt_key] = fingerprint
    return {
        "stage": attempt_key,
        "previous_attempts": previous_attempts,
        "previous_fingerprint": previous_fingerprint,
        "current_fingerprint": fingerprint,
        "reason": "progress fingerprint changed",
    }


def _plain_status_label(mode: str, active: list[dict[str, Any]], blocked_reason: str) -> str:
    if blocked_reason:
        return "已阻塞"
    if active:
        return "正在推进"
    if mode in {"dispatched", "waiting_on_active_task"}:
        return "已派发"
    if mode in {"guard_blocked", "guard_policy_gap", "frozen", "external_blocked"}:
        return "已阻塞"
    if mode == "all_green":
        return "全部当前门禁已绿"
    return "等待下一轮"


def _render_visible_status(heartbeat: dict[str, Any]) -> str:
    target = heartbeat.get("target") or {}
    active = heartbeat.get("active_tasks") or []
    parked_external = heartbeat.get("parked_external_blockers") or []
    supervised_ready = heartbeat.get("supervised_ready_modules") or []
    blocked_reviews = heartbeat.get("blocked_review_tasks") or []
    closure_recoveries = heartbeat.get("closure_recoveries") or []
    attempt_resets = heartbeat.get("attempt_resets") or []
    created = heartbeat.get("created_tasks") or []
    guards = heartbeat.get("guards") or {}
    diagnosis = guards.get("diagnosis") or {}
    diff = guards.get("diff") or {}
    external = guards.get("external") or {}
    blocked_reason = str(heartbeat.get("blocked_reason") or "")
    status = _plain_status_label(str(heartbeat.get("mode") or ""), active, blocked_reason)
    if heartbeat.get("mode") == "all_green" and supervised_ready:
        status = "SPEC/门禁已完成，待受监督Code/PR"
    lines = [
        "# Hermes Fesun 实时状态",
        "",
        f"- 状态：{status}",
        f"- 更新时间(UTC)：{heartbeat.get('updated_at') or 'unknown'}",
        f"- Watchdog mode：{heartbeat.get('mode') or 'unknown'}",
        f"- 当前目标：{target.get('module') or 'none'} / {target.get('stage') or 'none'}",
        f"- 下一合法动作：{target.get('next_action') or 'none'}",
        f"- 受监督 Code/PR 就绪：{len(supervised_ready)}",
        f"- 停靠外部阻塞：{len(parked_external)}",
        f"- no-progress：{heartbeat.get('no_progress_count', 0)}/{heartbeat.get('no_progress_threshold', 0)}",
        f"- 阻塞原因：{blocked_reason or '无'}",
        f"- token：{heartbeat.get('token_policy') or 'local scan'}",
        "",
        "## 当前 Worker",
    ]
    if active:
        for task in active:
            lines.append(
                f"- {task.get('id')} · {task.get('status')} · pid={task.get('worker_pid') or 0} · "
                f"heartbeat={task.get('last_heartbeat_at') or 0} · {task.get('title')}"
            )
    else:
        lines.append("- 无 active worker")
    lines.extend(["", "## 受监督 Code/PR 就绪"])
    if supervised_ready:
        for item in supervised_ready:
            lines.append(
                f"- {item.get('module')} · {item.get('stage')} · "
                f"next={item.get('next_action') or 'none'}"
            )
    else:
        lines.append("- 无")
    lines.extend(["", "## 停靠外部阻塞"])
    if parked_external:
        for blocker in parked_external:
            lines.append(
                f"- {blocker.get('module')} / {blocker.get('stage')} · "
                f"{blocker.get('kind')} · {blocker.get('summary')} · "
                f"next={blocker.get('next_action') or 'none'}"
            )
    else:
        lines.append("- 无停靠阻塞项")
    lines.extend(["", "## Blocked Reviewer Recovery"])
    if blocked_reviews:
        for task in blocked_reviews:
            lines.append(
                f"- {task.get('id')} · failures={task.get('consecutive_failures') or 0} · "
                f"{task.get('title')} · error={task.get('last_failure_error') or 'unknown'}"
            )
    else:
        lines.append("- 无待补审查")
    lines.extend(["", "## 收尾卡死恢复"])
    if closure_recoveries:
        for item in closure_recoveries:
            commit = item.get("commit") or {}
            reason = commit.get("reason") or ""
            command = commit.get("command") if isinstance(commit.get("command"), dict) else {}
            stderr = str(command.get("stderr") or "").strip().splitlines()
            detail = ""
            if reason:
                detail = f" · reason={reason}"
            if stderr:
                detail += f" · stderr={stderr[0][:160]}"
            lines.append(
                f"- {item.get('task_id')} · {item.get('status')} · "
                f"{item.get('module')}/{item.get('stage')} · "
                f"commit={commit.get('commit') or commit.get('status') or 'none'} · "
                f"files={len(item.get('paths') or [])}{detail}"
            )
    else:
        lines.append("- 本轮无")
    lines.extend(["", "## Attempt Reset"])
    if attempt_resets:
        for item in attempt_resets:
            lines.append(
                f"- {item.get('stage')} · attempts {item.get('previous_attempts')} -> 0 · "
                f"{item.get('reason')}"
            )
    else:
        lines.append("- 本轮无")
    lines.extend(["", "## 新派发"])
    if created:
        for task in created:
            lines.append(
                f"- {task.get('id')} · created={task.get('created')} · "
                f"{task.get('module')} / {task.get('stage')}"
            )
    else:
        lines.append("- 本轮无新任务")
    lines.extend(
        [
            "",
            "## Guard",
            f"- diff passed：{diff.get('passed')}",
            f"- diagnosis：{diagnosis.get('kind') or 'none'} / {diagnosis.get('summary') or 'clear'}",
            f"- external：{external.get('kind') or 'none'} / {external.get('summary') or 'clear'}",
            "",
            "## 怎么判断",
            "- `正在推进` + heartbeat 持续更新：不用管。",
            "- `等待下一轮`：本轮没有 worker，Watchdog 会继续检查。",
            "- `已阻塞`：需要看阻塞原因，通常是 guard 或协议失败。",
            "- `当前目标` 是下一步真实阶段；不能只看聊天窗口是否在动。",
            "",
        ]
    )
    return "\n".join(lines)


def _write_visible_status(heartbeat: dict[str, Any]) -> None:
    text = _render_visible_status(heartbeat)
    _write_text_best_effort(visible_status_path(), text)
    _write_text_best_effort(desktop_visible_status_path(), text)


def _worker_body(repo: Path, runbook: Path, target: dict[str, Any], guards: dict[str, Any]) -> str:
    module = target["module"]
    stage = target["stage"]
    row = target["row"]
    next_action = target.get("next_action") or ""
    return (
        "你是九模块 SPEC 专属 worker。严格执行 SPEC-only，不写业务代码、不开 PR、不碰 CI/bench/staging/部署/secrets。\n\n"
        "开工前只读这些绝对路径和必要切片：\n"
        + "\n".join(f"- {item}" for item in REQUIRED_READING)
        + "\n\n"
        f"当前 repo: {repo}\n"
        f"runbook: {runbook}\n"
        f"目标模块: {module}\n"
        f"当前阶段: {stage}\n"
        f"runbook 下一合法动作: {next_action or '未显式写明'}\n"
        f"runbook §E 当前行: {json.dumps(row, ensure_ascii=False)}\n\n"
        "允许写入范围仅限：docs/specs/{模块}/、docs/stories/、docs/flows/、docs/contracts/、docs/plans/、docs/reviews/、docs/handoff/、docs/ai-memory/sessions/、runbook §E/§F。\n"
        "目录纪律：Story/Flow 阶段写 docs/stories/ 与 docs/flows/；Spec 阶段写 docs/specs/{模块}/；Contract 阶段写 docs/contracts/；Plan 阶段写 docs/plans/；PR拆解/Code前准备阶段只写 docs/plans/、docs/reviews/、docs/handoff/、docs/ai-memory/；Code前门禁/外部预检阶段只写核验报告、门禁清单、handoff 或 ai-memory 证据；审查写 docs/reviews/；护栏补充写 docs/handoff/；不要写业务代码、不要真正开 PR、不要触发 CI、部署、secrets 或其他目录。\n"
        "若 runbook 开发栏是待开发、待开发拆PR、待真缺口拆分或待缺口拆分，本轮目标是补齐 Plan/PR拆解/code_preflight 到受监督 Code/PR ready；仍然只写文档和门禁证据，不写业务代码。\n"
        "Code前门禁如需 Linear UI、Golden fixture schema 或外部系统而工具不可用，写 external_preflight_block 证据并标明缺口，不得伪造通过、不得静默等待。\n"
        "每条故事/spec/contract 必须挂出处；找不到依据就停在待审/待决，不编。\n"
        "必须生成或维护蓝图逐条映射表；蓝图侧每条需求都要对应故事+出处。\n"
        "独立审未过不得回写 §E 为通过；模块产物完成后按用户铁律准备/执行单模块 commit，commit message 必须含 FSN 编号。\n"
        "真待决只允许上报：合同中心D1/D3/D4、提成口径、CFO oracle、specific_factory。\n\n"
        f"当前 guard: {json.dumps(guards, ensure_ascii=False)}"
    )


def _create_project_task(
    *,
    repo: Path,
    runbook: Path,
    board: str,
    assignee: str | None,
    goal_max_turns: int,
    target: dict[str, Any],
    guards: dict[str, Any],
) -> tuple[str, bool, dict[str, Any]]:
    module = target["module"]
    stage = target["stage"]
    key = f"fesun-nine-spec:{_module_slug(module)}:{stage}"
    title = f"Fesun SPEC watchdog: {module} {stage}"
    return _create_goal_task(
        title=title,
        body=_worker_body(repo, runbook, target, guards),
        board=board,
        assignee=assignee,
        goal_max_turns=goal_max_turns,
        workspace_path=str(repo),
        idempotency_key=key,
    )


def tick(options: FesunTickOptions | None = None) -> dict[str, Any]:
    opts = options or FesunTickOptions()
    repo = Path(opts.repo).expanduser().resolve()
    runbook = Path(opts.runbook).expanduser().resolve()
    state = load_state()
    state["repo"] = str(repo)
    state["runbook"] = str(runbook)
    state["board"] = opts.board
    state["no_progress_threshold"] = int(opts.no_progress_threshold)
    state["max_stage_attempts"] = int(opts.max_stage_attempts)
    state["closure_stall_seconds"] = int(opts.closure_stall_seconds)

    attempt_resets: list[dict[str, Any]] = []
    runbook_data = parse_runbook(runbook)
    supervised_ready = _supervised_ready_modules(runbook_data["modules"])
    attempt_resets.extend(_clear_satisfied_attempts(state, supervised_ready))
    active = _active_tasks(opts.board)
    blocked_reviews = _blocked_review_tasks(opts.board, repo)
    eligible = _eligible_modules(runbook_data["modules"])
    target, parked_external_blockers, external_blocker = _select_dispatch_target(eligible)
    diff_guard = _diff_guard(repo, state.get("baseline_out_of_scope", []))
    symlink_guard = _symlink_guard(repo)
    diagnosis = _blocked_diagnosis(diff_guard, symlink_guard)
    closure_recoveries = _recover_closure_stalls(
        repo=repo,
        runbook=runbook,
        runbook_data=runbook_data,
        board=opts.board,
        active=active,
        diff_guard=diff_guard,
        closure_stall_seconds=int(opts.closure_stall_seconds),
        enabled=bool(opts.create_tasks),
    )
    if closure_recoveries:
        runbook_data = parse_runbook(runbook)
        supervised_ready = _supervised_ready_modules(runbook_data["modules"])
        attempt_resets.extend(_clear_satisfied_attempts(state, supervised_ready))
        active = _active_tasks(opts.board)
        blocked_reviews = _blocked_review_tasks(opts.board, repo)
        eligible = _eligible_modules(runbook_data["modules"])
        target, parked_external_blockers, external_blocker = _select_dispatch_target(eligible)
        diff_guard = _diff_guard(repo, state.get("baseline_out_of_scope", []))
        symlink_guard = _symlink_guard(repo)
        diagnosis = _blocked_diagnosis(diff_guard, symlink_guard)
    orphan_recoveries = _recover_orphan_closure_evidence(
        repo=repo,
        runbook=runbook,
        target=target,
        active=active,
        diff_guard=diff_guard,
        enabled=bool(opts.create_tasks),
    )
    if orphan_recoveries:
        closure_recoveries.extend(orphan_recoveries)
        runbook_data = parse_runbook(runbook)
        supervised_ready = _supervised_ready_modules(runbook_data["modules"])
        attempt_resets.extend(_clear_satisfied_attempts(state, supervised_ready))
        active = _active_tasks(opts.board)
        blocked_reviews = _blocked_review_tasks(opts.board, repo)
        eligible = _eligible_modules(runbook_data["modules"])
        target, parked_external_blockers, external_blocker = _select_dispatch_target(eligible)
        diff_guard = _diff_guard(repo, state.get("baseline_out_of_scope", []))
        symlink_guard = _symlink_guard(repo)
        diagnosis = _blocked_diagnosis(diff_guard, symlink_guard)
    mapping = _mapping_validation(repo, target["module"]) if target else {"status": "not_applicable"}
    fingerprint = _progress_fingerprint(repo, runbook_data, active)
    previous = state.get("last_progress_fingerprint", "")
    progressed = fingerprint != previous and bool(previous)
    if progressed:
        no_progress_count = 0
    elif target and not active and external_blocker.get("status") != "blocked":
        no_progress_count = int(state.get("no_progress_count", 0)) + 1
    else:
        no_progress_count = 0
    state["last_progress_fingerprint"] = fingerprint
    state["no_progress_count"] = no_progress_count

    mode = "all_green" if not target and not active and diff_guard["passed"] and symlink_guard["passed"] else "watching"
    created_tasks: list[dict[str, Any]] = []
    blocked_reason = ""
    should_dispatch = False

    if not diff_guard["passed"]:
        mode = "guard_policy_gap" if diagnosis.get("kind") == "probable_policy_gap" else "guard_blocked"
        blocked_reason = str(diagnosis.get("summary") or "new out-of-scope git changes")
    elif not symlink_guard["passed"]:
        mode = "guard_blocked"
        blocked_reason = str(diagnosis.get("summary") or "bad symlink under allowed docs roots")
    elif blocked_reviews and opts.create_tasks:
        original = blocked_reviews[0]
        task_id, created, info = _create_review_recovery_task(
            repo=repo,
            board=opts.board,
            assignee=opts.assignee,
            goal_max_turns=max(1, int(opts.goal_max_turns or DEFAULT_GOAL_TURNS)),
            original=original,
        )
        created_tasks.append(
            {
                "id": task_id,
                "created": created,
                "module": "review-recovery",
                "stage": "blocked_reviewer",
                "blocked_task": original["id"],
                "blocked_title": original["title"],
                "idempotency_key": info.get("idempotency_key", ""),
                "attempt": int(original.get("consecutive_failures") or 0) + 1,
            }
        )
        mode = "review_recovery_dispatched" if created else "review_recovery_waiting"
        should_dispatch = True
    elif blocked_reviews:
        mode = "review_recovery_ready"
        blocked_reason = f"blocked reviewer needs recovery: {blocked_reviews[0]['id']}"
    elif active:
        mode = "active_worker"
    elif target and external_blocker.get("status") == "blocked":
        mode = "external_blocked"
        blocked_reason = str(external_blocker.get("summary") or "external blocker")
    elif target and no_progress_count >= int(opts.no_progress_threshold):
        attempt_key = _attempt_key(target["module"], target["stage"])
        reset = _reset_attempt_if_fingerprint_changed(state, attempt_key, fingerprint)
        if reset:
            attempt_resets.append(reset)
            state["no_progress_count"] = 0
            no_progress_count = 0
        attempts = state.setdefault("attempts", {})
        attempt_count = int(attempts.get(attempt_key, 0))
        if attempt_count >= int(opts.max_stage_attempts):
            mode = "frozen"
            blocked_reason = f"attempt limit reached for {attempt_key}"
        elif opts.create_tasks:
            task_id, created, info = _create_project_task(
                repo=repo,
                runbook=runbook,
                board=opts.board,
                assignee=opts.assignee,
                goal_max_turns=max(1, int(opts.goal_max_turns or DEFAULT_GOAL_TURNS)),
                target=target,
                guards={"diff": diff_guard, "symlink": symlink_guard, "mapping": mapping},
            )
            if created:
                attempts[attempt_key] = attempt_count + 1
                state.setdefault("attempt_fingerprints", {})[attempt_key] = fingerprint
            created_tasks.append(
                {
                    "id": task_id,
                    "created": created,
                    "module": target["module"],
                    "stage": target["stage"],
                    "idempotency_key": info.get("idempotency_key", ""),
                    "attempt": int(attempts.get(attempt_key, attempt_count)),
                }
            )
            mode = "dispatched" if created else "waiting_on_active_task"
            should_dispatch = True
        else:
            mode = "stalled_ready_to_dispatch"

    # Keep the dispatcher warm even when a worker already looks active.  This is
    # the overnight no-idle path: stale "running" tasks must be reclaimed by the
    # kanban dispatcher instead of making the project watchdog think progress is
    # still happening.
    should_maintain_dispatcher = should_dispatch or bool(active)
    dispatch_result = _dispatch_once(opts.board) if opts.dispatch and should_maintain_dispatcher else None
    tick_id = f"fesun-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:8]}"
    heartbeat = {
        "schema": FESUN_SCHEMA,
        "tick_id": tick_id,
        "updated_at": _utcnow(),
        "mode": mode,
        "wake_agent": bool(created_tasks),
        "blocked_reason": blocked_reason,
        "repo": str(repo),
        "runbook": str(runbook),
        "target": target,
        "eligible_count": len(eligible),
        "runnable_count": len(eligible) - len(parked_external_blockers),
        "supervised_ready_modules": supervised_ready,
        "parked_external_blockers": parked_external_blockers,
        "active_tasks": active,
        "blocked_review_tasks": blocked_reviews,
        "closure_recoveries": closure_recoveries,
        "attempt_resets": attempt_resets,
        "created_tasks": created_tasks,
        "no_progress_count": no_progress_count,
        "no_progress_threshold": int(opts.no_progress_threshold),
        "max_stage_attempts": int(opts.max_stage_attempts),
        "guards": {"diff": diff_guard, "symlink": symlink_guard, "mapping": mapping, "diagnosis": diagnosis, "external": external_blocker},
        "dispatch": dispatch_result,
        "token_policy": "local scan only; LLM tokens only when created_tasks is non-empty",
    }
    _atomic_write_json(heartbeat_path(), heartbeat)
    _write_visible_status(heartbeat)
    ticks = state.setdefault("ticks", [])
    ticks.insert(0, heartbeat)
    state["ticks"] = ticks[:50]
    state["last_tick_at"] = heartbeat["updated_at"]
    state["last_mode"] = mode
    state["last_target"] = target
    save_state(state)
    return {"ok": True, "heartbeat": heartbeat, "state": state, "state_path": str(state_path()), "heartbeat_path": str(heartbeat_path())}


def status() -> dict[str, Any]:
    return {
        "ok": True,
        "state": load_state(),
        "heartbeat": _read_json(heartbeat_path(), {}),
        "state_path": str(state_path()),
        "heartbeat_path": str(heartbeat_path()),
        "visible_status_path": str(visible_status_path()),
        "desktop_visible_status_path": str(desktop_visible_status_path()),
    }


def _script_content(payload: dict[str, Any]) -> str:
    return (
        "from __future__ import annotations\n"
        "import json\n"
        "import sys\n\n"
        f"PAYLOAD = {json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        "repo = PAYLOAD.get('hermes_repo') or ''\n"
        "if repo and repo not in sys.path:\n"
        "    sys.path.insert(0, repo)\n"
        "from hermes_fesun_watchdog import FesunTickOptions, tick\n"
        "result = tick(FesunTickOptions(\n"
        "    repo=PAYLOAD['repo'],\n"
        "    runbook=PAYLOAD['runbook'],\n"
        "    board=PAYLOAD.get('board') or 'default',\n"
        "    assignee=PAYLOAD.get('assignee') or None,\n"
        "    goal_max_turns=int(PAYLOAD.get('goal_max_turns') or 120),\n"
        "    create_tasks=True,\n"
        "    dispatch=True,\n"
        "    no_progress_threshold=int(PAYLOAD.get('no_progress_threshold') or 1),\n"
        "    max_stage_attempts=int(PAYLOAD.get('max_stage_attempts') or 3),\n"
        "    closure_stall_seconds=int(PAYLOAD.get('closure_stall_seconds') or 600),\n"
        "))\n"
        "h = result.get('heartbeat', {})\n"
        "created = [t for t in h.get('created_tasks', []) if t.get('created')]\n"
        "diagnosis = h.get('guards', {}).get('diagnosis', {})\n"
        "if not created:\n"
        "    print(json.dumps({'wakeAgent': False, 'mode': h.get('mode'), 'blockedReason': h.get('blocked_reason'), 'diagnosis': diagnosis}, ensure_ascii=False))\n"
        "else:\n"
        "    print(json.dumps({'wakeAgent': True, 'runner': 'fesun-nine-spec', 'mode': h.get('mode'), 'created_tasks': created, 'diagnosis': diagnosis}, ensure_ascii=False))\n"
    )


def install(
    *,
    schedule: str = "every 2m",
    repo: str = DEFAULT_REPO,
    runbook: str = DEFAULT_RUNBOOK,
    board: str = DEFAULT_BOARD,
    assignee: str | None = DEFAULT_ASSIGNEE,
    goal_max_turns: int = DEFAULT_GOAL_TURNS,
    no_progress_threshold: int = NO_PROGRESS_THRESHOLD,
    max_stage_attempts: int = MAX_STAGE_ATTEMPTS,
    closure_stall_seconds: int = CLOSURE_STALL_SECONDS,
) -> dict[str, Any]:
    repo_path = str(Path(repo).expanduser().resolve())
    runbook_path = str(Path(runbook).expanduser().resolve())
    state = load_state()
    baseline = _diff_guard(Path(repo_path), [])
    state["baseline_out_of_scope"] = baseline.get("out_of_scope", [])
    state["repo"] = repo_path
    state["runbook"] = runbook_path
    state["board"] = board
    state["no_progress_threshold"] = int(no_progress_threshold)
    state["max_stage_attempts"] = int(max_stage_attempts)
    state["closure_stall_seconds"] = int(closure_stall_seconds)
    save_state(state)

    scripts = _scripts_dir()
    scripts.mkdir(parents=True, exist_ok=True)
    script_path = scripts / FESUN_SCRIPT_NAME
    payload = {
        "hermes_repo": str(Path(__file__).resolve().parent),
        "repo": repo_path,
        "runbook": runbook_path,
        "board": board,
        "assignee": assignee,
        "goal_max_turns": int(goal_max_turns),
        "no_progress_threshold": int(no_progress_threshold),
        "max_stage_attempts": int(max_stage_attempts),
        "closure_stall_seconds": int(closure_stall_seconds),
    }
    script_path.write_text(_script_content(payload), encoding="utf-8")

    from cron import jobs as cron_jobs

    removed: list[str] = []
    for job in cron_jobs.list_jobs(include_disabled=True):
        if job.get("name") == FESUN_JOB_NAME:
            if cron_jobs.remove_job(str(job["id"])):
                removed.append(str(job["id"]))
    job = cron_jobs.create_job(
        prompt="Fesun nine-module SPEC watchdog",
        schedule=schedule,
        name=FESUN_JOB_NAME,
        deliver="local",
        script=FESUN_SCRIPT_NAME,
        workdir=repo_path,
        no_agent=True,
    )
    state["watchdog"] = {
        "installed": True,
        "job_id": job["id"],
        "schedule": schedule,
        "script": str(script_path),
        "installed_at": _utcnow(),
        "removed_job_ids": removed,
    }
    save_state(state)
    return {"ok": True, "job": job, "script": str(script_path), "removed": removed, "state": state}


def watchdog_status() -> dict[str, Any]:
    from cron import jobs as cron_jobs

    jobs = [
        job
        for job in cron_jobs.list_jobs(include_disabled=True)
        if job.get("name") == FESUN_JOB_NAME
    ]
    return {"ok": True, "installed": bool(jobs), "jobs": jobs, "script": str(_scripts_dir() / FESUN_SCRIPT_NAME), "state": load_state()}
