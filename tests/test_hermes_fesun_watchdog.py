from __future__ import annotations

import importlib
import json
import subprocess


def _modules(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    import hermes_fesun_watchdog
    from hermes_cli import kanban_db

    return importlib.reload(hermes_fesun_watchdog), importlib.reload(kanban_db)


def _repo(tmp_path):
    repo = tmp_path / "fesun-platform"
    runbook = repo / "docs" / "plans" / "00_九模块开发SOP与状态台账_权威runbook.md"
    runbook.parent.mkdir(parents=True)
    runbook.write_text(
        """# runbook

## §A 不可变流程
第1步 brief

## §C 九模块依据表
| 模块 | 蓝图补丁(设计依据) | 源文件(需求) | 代码现状关键 |
|---|---|---|---|
| ⑥销售B2B | 补丁F部分 | B2B制度 | grep |

## §D 顺序
再并行: ③ ④ ⑤ ⑥ ①

## §E 状态台账
| 模块 | 当前步 | 故事 | spec | contract | Linear | 开发 | 真待决 |
|---|---|---|---|---|---|---|---|
| ⑥销售B2B | 可进入Spec层 | ✅FSN-553 Story | — | — | ✅FSN-553 | — | — |

## §F 边界台账
| 逻辑 | 唯一的家 | 其他模块 |
|---|---|---|
| 成本计算 | ⑤产品/成本 | ⑨引用 |
""",
        encoding="utf-8",
    )
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo, runbook


def test_fesun_tick_dispatches_on_first_no_progress_tick_by_default(tmp_path, monkeypatch):
    hermes_fesun_watchdog, kanban_db = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)

    first = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=True,
            board="default",
            assignee="default",
        )
    )

    assert first["heartbeat"]["mode"] in {"dispatched", "waiting_on_active_task"}
    assert first["heartbeat"]["created_tasks"][0]["module"] == "⑥销售B2B"
    with kanban_db.connect(board="default") as conn:
        row = conn.execute(
            "SELECT goal_mode, idempotency_key FROM tasks WHERE id = ?",
            (first["heartbeat"]["created_tasks"][0]["id"],),
        ).fetchone()
    assert row["goal_mode"] == 1
    assert str(row["idempotency_key"]).startswith("fesun-nine-spec:销售B2B:spec")
    visible = hermes_fesun_watchdog.visible_status_path()
    assert visible.exists()
    text = visible.read_text(encoding="utf-8")
    assert "Hermes Fesun 实时状态" in text
    assert "当前目标：⑥销售B2B / spec" in text


def test_fesun_tick_blocks_dispatch_when_kgctl_does_not_authorize(tmp_path, monkeypatch):
    hermes_fesun_watchdog, kanban_db = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    monkeypatch.setattr(
        hermes_fesun_watchdog,
        "_kgctl_control_status",
        lambda _repo: {
            "status": "blocked",
            "enforced": True,
            "dispatch_authorized": False,
            "control_blockers": [
                "flat queue requires KGCTL_MACHINE_TASK dispatch_authorized: true"
            ],
            "current_task": "FSN-553",
            "queue_remaining": 1,
        },
    )
    monkeypatch.setattr(
        hermes_fesun_watchdog,
        "_dispatch_once",
        lambda _board: (_ for _ in ()).throw(AssertionError("dispatcher must not run")),
    )

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=True,
            dispatch=True,
            board="default",
        )
    )

    heartbeat = result["heartbeat"]
    assert heartbeat["mode"] == "control_blocked"
    assert heartbeat["runnable_count"] == 0
    assert heartbeat["created_tasks"] == []
    assert heartbeat["dispatch"] is None
    assert heartbeat["no_progress_count"] == 0
    assert heartbeat["guards"]["control"]["current_task"] == "FSN-553"
    with kanban_db.connect(board="default") as conn:
        task_count = conn.execute("SELECT COUNT(*) AS count FROM tasks").fetchone()["count"]
    assert task_count == 0


def test_fesun_tick_fails_closed_when_kgctl_is_unavailable(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    monkeypatch.setattr(
        hermes_fesun_watchdog,
        "_kgctl_control_status",
        lambda _repo: {
            "status": "unavailable",
            "enforced": True,
            "dispatch_authorized": False,
            "control_blockers": ["kgctl status FESUN unavailable: timeout"],
        },
    )

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=True,
            dispatch=True,
            board="default",
        )
    )

    heartbeat = result["heartbeat"]
    assert heartbeat["mode"] == "control_unavailable"
    assert heartbeat["runnable_count"] == 0
    assert heartbeat["created_tasks"] == []
    assert heartbeat["dispatch"] is None


def test_fesun_spec_watchdog_does_not_requeue_module_after_pr_merge(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    module = {
        "模块": "③财务",
        "当前步": "code_preflight 2/3 PASS；PR1 已合并；PR2 仅在独立授权后可开始",
        "故事": "✅Story",
        "spec": "✅Spec Review Final PASS",
        "contract": "✅Contract Gate Final PASS",
        "Linear": "✅FSN-551",
        "开发": "PR1 已合并；PR2 未开始；未部署",
        "真待决": "H-FIN-01/02 pending_by_decision",
    }

    assert hermes_fesun_watchdog._is_supervised_code_pr_ready(module) is True
    assert hermes_fesun_watchdog._eligible_modules([module]) == []


def test_fesun_control_gate_blocks_false_green_when_current_task_has_no_target(
    tmp_path, monkeypatch
):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    guarded = hermes_fesun_watchdog._apply_control_target_guard(
        {
            "status": "authorized",
            "enforced": True,
            "dispatch_authorized": True,
            "control_blockers": [],
            "current_task": "FSN-551",
            "queue_remaining": 1,
        },
        None,
    )

    assert guarded["status"] == "target_mismatch"
    assert guarded["dispatch_authorized"] is False
    assert guarded["control_blockers"] == [
        "kgctl current task FSN-551 has no matching watchdog target"
    ]


def test_fesun_control_gate_fails_closed_when_authorized_task_is_missing(
    tmp_path, monkeypatch
):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    guarded = hermes_fesun_watchdog._apply_control_target_guard(
        {
            "status": "authorized",
            "enforced": True,
            "dispatch_authorized": True,
            "control_blockers": [],
            "current_task": None,
            "queue_remaining": 1,
        },
        {"module": "③财务", "stage": "code_preflight", "row": {"Linear": "FSN-551"}},
    )

    assert guarded["status"] == "target_mismatch"
    assert guarded["dispatch_authorized"] is False


def test_gateway_status_write_skips_desktop_mirror(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    desktop_status = tmp_path / "Desktop" / "status.md"
    monkeypatch.setattr(
        hermes_fesun_watchdog,
        "desktop_visible_status_path",
        lambda: desktop_status,
    )
    monkeypatch.setenv("_HERMES_GATEWAY", "1")

    hermes_fesun_watchdog._write_visible_status({"mode": "watching"})

    assert hermes_fesun_watchdog.visible_status_path().exists()
    assert not desktop_status.exists()


def test_headless_cron_status_write_skips_desktop_mirror(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    desktop_status = tmp_path / "Desktop" / "status.md"
    monkeypatch.setattr(
        hermes_fesun_watchdog,
        "desktop_visible_status_path",
        lambda: desktop_status,
    )
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
    monkeypatch.setenv("HERMES_FESUN_HEADLESS", "1")

    hermes_fesun_watchdog._write_visible_status({"mode": "watching"})

    assert hermes_fesun_watchdog.visible_status_path().exists()
    assert not desktop_status.exists()


def test_fesun_parse_rescues_misplaced_status_rows_from_section_c(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    text = runbook.read_text(encoding="utf-8")
    text = text.replace(
        "| ⑥销售B2B | 补丁F部分 | B2B制度 | grep |",
        "| ⑥销售B2B | 补丁F部分 | B2B制度 | grep |\n"
        "| ①生产/交付 | 第6步✅Spec Round1闭合(PASS) | ✅Story | ✅Spec Round1闭合 | — | ✅FSN-556 | 可进入Contract层；仍禁止Plan/代码/PR/CI/staging/部署 | Contract必带 API facade/adapter 契约 |\n"
        "| ②人力/KPI | code_preflight 2/3 PASS；下一合法动作=处理外部护栏 blocker | ✅Story | ✅Spec | ✅Contract+Plan PASS | 待按Plan粒度建票 | Code前门禁仍BLOCKED | 外部护栏 blocker |\n"
        "| ⑦门店B2C | 第4步✅Story/Flow Gate 3/3 PASS；下一合法动作=Spec | ✅Story | — | ✅Brief三方审查PASS | ✅FSN-559 | 下一合法动作仅为 Spec | B2C 门店提成主线 |",
    )
    text = text.replace(
        "| ⑥销售B2B | 可进入Spec层 | ✅FSN-553 Story | — | — | ✅FSN-553 | — | — |",
        "| ⑥销售B2B | 已闭合 | ✅Story | ✅Spec | ✅Contract | ✅FSN-553 | ✅Done | — |",
    )
    runbook.write_text(text, encoding="utf-8")

    data = hermes_fesun_watchdog.parse_runbook(runbook)
    rescued = set(data["rescued_modules"])

    assert {"①生产/交付", "②人力/KPI", "⑦门店B2C"} <= rescued
    modules = {row["模块"]: row for row in data["modules"]}
    assert "①生产/交付" in modules
    assert "②人力/KPI" in modules
    assert "⑦门店B2C" in modules

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=False,
            board="default",
        )
    )

    assert result["heartbeat"]["target"]["module"] == "①生产/交付"
    assert result["heartbeat"]["target"]["stage"] == "contract"


def test_fesun_external_guardrail_blocker_is_parked(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    blocker = hermes_fesun_watchdog._external_blocker(
        {
            "module": "②人力/KPI",
            "stage": "code_preflight",
            "next_action": "处理外部护栏 blocker，仍禁止Code/PR/CI/staging/部署",
            "row": {
                "模块": "②人力/KPI",
                "当前步": "code_preflight 2/3 PASS；下一合法动作=处理外部护栏 blocker",
                "开发": "Code前门禁仍BLOCKED；未进Code/PR/CI",
                "真待决": "外部护栏 blocker：A/D/E/F/I3/I5/J/K",
            },
        }
    )

    assert blocker["status"] == "blocked"
    assert blocker["kind"] == "external_permission_required"
    assert "外部护栏" in blocker["markers"]


def test_fesun_tick_blocks_new_out_of_scope_diff(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    state = hermes_fesun_watchdog.load_state()
    state["baseline_out_of_scope"] = []
    hermes_fesun_watchdog.save_state(state)
    (repo / "apps").mkdir()
    (repo / "apps" / "business.py").write_text("print('no')\n", encoding="utf-8")

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=True,
            board="default",
        )
    )

    assert result["heartbeat"]["mode"] == "guard_blocked"
    assert result["heartbeat"]["guards"]["diagnosis"]["kind"] == "hard_violation"
    assert result["heartbeat"]["guards"]["diagnosis"]["severity"] == "critical"
    assert "apps/business.py" in result["heartbeat"]["guards"]["diff"]["new_out_of_scope"]
    assert result["heartbeat"]["created_tasks"] == []


def test_fesun_tick_diagnoses_docs_only_policy_gap(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    (repo / "docs" / "unknown-stage").mkdir(parents=True)
    (repo / "docs" / "unknown-stage" / "FSN-552_notes.md").write_text(
        "# Unknown docs landing zone\n",
        encoding="utf-8",
    )

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=True,
            board="default",
        )
    )

    diagnosis = result["heartbeat"]["guards"]["diagnosis"]
    assert result["heartbeat"]["mode"] == "guard_policy_gap"
    assert diagnosis["kind"] == "probable_policy_gap"
    assert diagnosis["severity"] == "warning"
    assert diagnosis["paths"] == ["docs/unknown-stage/FSN-552_notes.md"]
    assert result["heartbeat"]["created_tasks"] == []


def test_fesun_classifies_contract_stage(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    text = runbook.read_text(encoding="utf-8")
    runbook.write_text(
        text.replace(
            "| ⑥销售B2B | 可进入Spec层 | ✅FSN-553 Story | — | — | ✅FSN-553 | — | — |",
            "| ⑥销售B2B | 第4步✅Spec Round1三方PASS | ✅FSN-553 Story | ✅Spec Round1闭合 | — | ✅FSN-553 | 可进入Contract层 | — |",
        ),
        encoding="utf-8",
    )

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=False,
            board="default",
        )
    )

    assert result["heartbeat"]["target"]["module"] == "⑥销售B2B"
    assert result["heartbeat"]["target"]["stage"] == "contract"
    assert result["heartbeat"]["mode"] != "frozen"


def test_fesun_classifies_pending_spec_review_before_contract(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    text = runbook.read_text(encoding="utf-8")
    runbook.write_text(
        text.replace(
            "| ⑥销售B2B | 可进入Spec层 | ✅FSN-553 Story | — | — | ✅FSN-553 | — | — |",
            "| ⑥销售B2B | 第6步✅Spec v1 drafted，待三方审查 | ✅FSN-553 Story | ✅Spec v1=`docs/specs/Tier1_⑥/spec.md` | — | ✅FSN-553 | Spec Round1 待三方审查；仍禁止 Contract/Plan/代码 | — |",
        ),
        encoding="utf-8",
    )

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=False,
            board="default",
        )
    )

    assert result["heartbeat"]["target"]["module"] == "⑥销售B2B"
    assert result["heartbeat"]["target"]["stage"] == "spec_review"
    assert result["heartbeat"]["target"]["stage"] != "contract"


def test_fesun_classifies_contract_pass_next_action_plan(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    text = runbook.read_text(encoding="utf-8")
    runbook.write_text(
        text.replace(
            "| ⑥销售B2B | 可进入Spec层 | ✅FSN-553 Story | — | — | ✅FSN-553 | — | — |",
            "| ⑧专项制度 | 第6步✅Contract三方审查PASS(3/3：Codex retry1/DeepSeek/Zhipu-Qwen；P0=0/P1=0/P2=0)；Contract=`docs/contracts/Tier1_⑧/专项制度_contract.md`；Final=`docs/reviews/contract/Tier1_⑧_gate/contract_review_final_pass_2026-07-06.md`；下一合法动作=Plan；仍禁止Code/PR/CI/staging/部署 | ✅Story | ✅Spec repair三方PASS | ✅Contract Gate Final=`docs/reviews/contract/Tier1_⑧_gate/contract_review_final_pass_2026-07-06.md`；Contract已显式保留边界；未进入Plan/Code | ✅FSN-561 | 下一合法动作=Plan；仍禁止Code/PR/CI/staging/部署 | Contract Gate已三方确认PASS(3/3) |",
        ),
        encoding="utf-8",
    )

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=False,
            board="default",
        )
    )

    assert result["heartbeat"]["target"]["module"] == "⑧专项制度"
    assert result["heartbeat"]["target"]["stage"] == "plan"
    assert result["heartbeat"]["target"]["stage"] != "contract_review"


def test_fesun_blocked_tasks_do_not_count_as_active(tmp_path, monkeypatch):
    hermes_fesun_watchdog, kanban_db = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    with kanban_db.connect(board="default") as conn:
        conn.execute(
            """
            INSERT INTO tasks (id, title, status, assignee, idempotency_key, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "t_blocked",
                "blocked fesun task",
                "blocked",
                "default",
                "fesun-nine-spec:销售B2B:spec",
                1,
            ),
        )
        conn.commit()

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=False,
            board="default",
        )
    )

    assert result["heartbeat"]["active_tasks"] == []
    assert result["heartbeat"]["mode"] != "active_worker"


def test_fesun_tick_allows_story_flow_and_session_docs(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    (repo / "docs" / "stories" / "Tier1_⑤⑥").mkdir(parents=True)
    (repo / "docs" / "flows").mkdir(parents=True)
    (repo / "docs" / "ai-memory" / "decisions").mkdir(parents=True)
    (repo / "docs" / "ai-memory" / "sessions").mkdir(parents=True)
    (repo / "docs" / "stories" / "Tier1_⑤⑥" / "FSN-552_⑤产品成本_蓝图映射表.md").write_text(
        "# 蓝图映射表\n",
        encoding="utf-8",
    )
    (repo / "docs" / "flows" / "Tier1_⑤⑥_story_flow_2026-07-01.md").write_text(
        "# Flow\n",
        encoding="utf-8",
    )
    (repo / "docs" / "ai-memory" / "sessions" / "2026-07-01-2351.md").write_text(
        "# session\n",
        encoding="utf-8",
    )
    (repo / "docs" / "ai-memory" / "decisions" / "2026-07-02-fsn-556.md").write_text(
        "# decision\n",
        encoding="utf-8",
    )

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=False,
            board="default",
        )
    )

    assert result["heartbeat"]["guards"]["diff"]["new_out_of_scope"] == []
    assert result["heartbeat"]["guards"]["diff"]["passed"] is True
    assert result["heartbeat"]["guards"]["diagnosis"]["kind"] == "none"
    assert result["heartbeat"]["mode"] != "guard_blocked"


def test_fesun_tick_allows_contract_and_handoff_docs(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    (repo / "docs" / "contracts" / "Tier1_⑤").mkdir(parents=True)
    (repo / "docs" / "handoff").mkdir(parents=True)
    (repo / "docs" / "contracts" / "Tier1_⑤" / "FSN-552_contract.md").write_text(
        "# Contract draft\n",
        encoding="utf-8",
    )
    (repo / "docs" / "handoff" / "护栏清单.md").write_text(
        "# Guard update\n",
        encoding="utf-8",
    )

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=False,
            board="default",
        )
    )

    assert result["heartbeat"]["guards"]["diff"]["new_out_of_scope"] == []
    assert result["heartbeat"]["guards"]["diff"]["passed"] is True
    assert result["heartbeat"]["guards"]["diagnosis"]["kind"] == "none"


def test_fesun_classifies_pending_contract_review(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    text = runbook.read_text(encoding="utf-8")
    runbook.write_text(
        text.replace(
            "| ⑥销售B2B | 可进入Spec层 | ✅FSN-553 Story | — | — | ✅FSN-553 | — | — |",
            "| ⑥销售B2B | 第5步✅Contract v1 drafted，待三方审查 | ✅FSN-553 Story | ✅Spec Round1闭合 | ✅Contract v1覆盖 | ✅FSN-553 | 待Contract三方审查，未进Plan/Code | — |",
        ),
        encoding="utf-8",
    )

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=False,
            board="default",
        )
    )

    assert result["heartbeat"]["target"]["module"] == "⑥销售B2B"
    assert result["heartbeat"]["target"]["stage"] == "contract_review"


def test_fesun_does_not_treat_contract_column_review_pass_as_done_when_dev_says_enter_contract(
    tmp_path, monkeypatch
):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    text = runbook.read_text(encoding="utf-8")
    runbook.write_text(
        text.replace(
            "| ⑥销售B2B | 可进入Spec层 | ✅FSN-553 Story | — | — | ✅FSN-553 | — | — |",
            "| ⑥销售B2B | 第6步✅Spec Round2三方PASS | ✅FSN-553 Story | ✅Spec Round2闭合 | ✅三方PASS | ✅FSN-553 | 可进入Contract层 | — |",
        ),
        encoding="utf-8",
    )

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=False,
            board="default",
        )
    )

    assert result["heartbeat"]["target"]["module"] == "⑥销售B2B"
    assert result["heartbeat"]["target"]["stage"] == "contract"
    assert result["heartbeat"]["mode"] != "all_green"


def test_fesun_does_not_treat_review_evidence_column_as_contract_done(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    text = runbook.read_text(encoding="utf-8")
    runbook.write_text(
        text.replace(
            "| ⑥销售B2B | 可进入Spec层 | ✅FSN-553 Story | — | — | ✅FSN-553 | — | — |",
            "| ⑦门店B2C | 第4步✅Story/Flow Gate 3/3 PASS(P0=0)；Spec Draft v1 已创建；下一合法动作=Spec三方审查 | ✅Story | ✅Spec Draft v1=`docs/specs/Tier1_⑦/门店B2C_spec.md` | ✅Brief三方审查PASS；✅Story/Flow三方审查PASS | — | 仅允许派发/回收Spec三方审查；Spec 3/3 PASS(P0=0)前仍禁止Contract/Code | — |",
        ),
        encoding="utf-8",
    )

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=False,
            board="default",
        )
    )

    assert result["heartbeat"]["target"]["module"] == "⑦门店B2C"
    assert result["heartbeat"]["target"]["stage"] == "spec_review"
    assert result["heartbeat"]["mode"] != "all_green"


def test_fesun_marks_waiting_contract_review_as_pending_even_with_checkmarks(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    text = runbook.read_text(encoding="utf-8")
    runbook.write_text(
        text.replace(
            "| ⑥销售B2B | 可进入Spec层 | ✅FSN-553 Story | — | — | ✅FSN-553 | — | — |",
            "| ⑤产品/成本 | 第5步✅Contract v1 drafted，Contract Round1 已回收 DeepSeek PASS with P1 + Codex PASS；等智谱/Qwen `deleg_66a082fd` | ✅FSN-552 Story | ✅Spec Round1闭合(P0=0/P1=0/P2=3) | ✅Contract v1覆盖；pending=`docs/reviews/contract/Tier1_⑤/pending.md` | ✅FSN-552 | 待智谱/Qwen回收 + DeepSeek P1吸收，未进Plan/Code | — |",
        ),
        encoding="utf-8",
    )

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=False,
            board="default",
        )
    )

    assert result["heartbeat"]["target"]["module"] == "⑤产品/成本"
    assert result["heartbeat"]["target"]["stage"] == "contract_review"
    assert result["heartbeat"]["mode"] != "all_green"


def test_fesun_classifies_plan_after_contract_pass(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    state = hermes_fesun_watchdog.load_state()
    state["attempts"] = {"产品成本:contract_review": 99}
    hermes_fesun_watchdog.save_state(state)
    text = runbook.read_text(encoding="utf-8")
    runbook.write_text(
        text.replace(
            "| ⑥销售B2B | 可进入Spec层 | ✅FSN-553 Story | — | — | ✅FSN-553 | — | — |",
            "| ⑤产品/成本 | 第5步✅Contract Round1 3/3 PASS；下一合法动作=Plan；仍禁止Code/PR/CI/staging/部署 | ✅FSN-552 Story | ✅Spec Round1闭合(P0=0/P1=0/P2=3) | ✅Contract v1.1三方门禁PASS：DeepSeek PASS(P1 absorbed)、Codex PASS、智谱/Qwen mini-pack `deleg_8ed060fc` PASS(P0=0/P1=0/P2=0) | ✅FSN-552 | 下一步仅允许Plan；未进Code/PR/CI | Plan必带P2 |",
        ),
        encoding="utf-8",
    )

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=False,
            board="default",
            no_progress_threshold=0,
        )
    )

    assert result["heartbeat"]["target"]["module"] == "⑤产品/成本"
    assert result["heartbeat"]["target"]["stage"] == "plan"
    assert result["heartbeat"]["mode"] != "frozen"


def test_fesun_freezes_same_stage_when_attempt_limit_reached_without_new_fingerprint(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    text = runbook.read_text(encoding="utf-8")
    runbook.write_text(
        text.replace(
            "| ⑥销售B2B | 可进入Spec层 | ✅FSN-553 Story | — | — | ✅FSN-553 | — | — |",
            "| ⑤产品/成本 | 第5步🔄Contract Draft已产出，待Contract三方审查；下一合法动作=Contract三方审查 | ✅FSN-552 Story | ✅Spec Round1闭合(P0=0/P1=0/P2=3) | 🔄Contract Draft v1=`docs/contracts/Tier1_⑤/FSN-552_contract.md`；Gate未闭合 | ✅FSN-552 | 仍禁止Plan/Code/PR/CI/staging/部署 | 待审 |",
        ),
        encoding="utf-8",
    )
    fingerprint = hermes_fesun_watchdog._progress_fingerprint(
        repo,
        hermes_fesun_watchdog.parse_runbook(runbook),
        [],
    )
    state = hermes_fesun_watchdog.load_state()
    state["attempts"] = {"产品成本:contract_review": 1}
    state["attempt_fingerprints"] = {"产品成本:contract_review": fingerprint}
    hermes_fesun_watchdog.save_state(state)

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=False,
            board="default",
            no_progress_threshold=0,
            max_stage_attempts=1,
        )
    )

    assert result["heartbeat"]["target"]["stage"] == "contract_review"
    assert result["heartbeat"]["mode"] == "frozen"
    assert result["heartbeat"]["attempt_resets"] == []


def test_fesun_resets_attempt_limit_when_stage_fingerprint_changes(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    text = runbook.read_text(encoding="utf-8")
    runbook.write_text(
        text.replace(
            "| ⑥销售B2B | 可进入Spec层 | ✅FSN-553 Story | — | — | ✅FSN-553 | — | — |",
            "| ⑤产品/成本 | 第5步🔄Contract Draft已产出，待Contract三方审查；下一合法动作=Contract三方审查 | ✅FSN-552 Story | ✅Spec Round1闭合(P0=0/P1=0/P2=3) | 🔄Contract Draft v1=`docs/contracts/Tier1_⑤/FSN-552_contract.md`；Gate未闭合 | ✅FSN-552 | 仍禁止Plan/Code/PR/CI/staging/部署 | 待审 |",
        ),
        encoding="utf-8",
    )
    state = hermes_fesun_watchdog.load_state()
    state["attempts"] = {"产品成本:contract_review": 1}
    state["attempt_fingerprints"] = {"产品成本:contract_review": "old-fingerprint"}
    hermes_fesun_watchdog.save_state(state)

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=False,
            board="default",
            no_progress_threshold=0,
            max_stage_attempts=1,
        )
    )

    heartbeat = result["heartbeat"]
    assert heartbeat["target"]["stage"] == "contract_review"
    assert heartbeat["mode"] != "frozen"
    assert heartbeat["attempt_resets"][0]["stage"] == "产品成本:contract_review"
    assert result["state"]["attempts"]["产品成本:contract_review"] == 0


def test_fesun_classifies_plan_when_contract_gate_closed_with_p1_absorption_history(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    text = runbook.read_text(encoding="utf-8")
    runbook.write_text(
        text.replace(
            "| ⑥销售B2B | 可进入Spec层 | ✅FSN-553 Story | — | — | ✅FSN-553 | — | — |",
            "| ②人力/KPI | 第5步✅Contract Round1 repair三方PASS，Contract Gate闭合；Final=`docs/reviews/contract/Tier1_②/final.md`；下一合法动作=进入Plan层；仍禁止Code/PR/CI/staging/部署 | ✅Story | ✅Spec Draft v3 | ✅Contract Draft v1.1；Round1：Qwen PASS(P0=0/P1=1/P2=1)；P1吸收=`docs/reviews/contract/Tier1_②/absorb.md`；Repair三方PASS：DeepSeek PASS(P0=0/P1=0/P2=1)、Qwen PASS(P0=0/P1=0/P2=1)、Zhipu PASS(P0=0/P1=0/P2=0)；Final=`docs/reviews/contract/Tier1_②/final.md` | — | 可进入Plan层；Plan三方门禁闭合前仍禁止Code/PR/CI/staging/部署 | Plan必须携带P2 |",
        ),
        encoding="utf-8",
    )

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=False,
            board="default",
        )
    )

    assert result["heartbeat"]["target"]["module"] == "②人力/KPI"
    assert result["heartbeat"]["target"]["stage"] == "plan"


def test_fesun_classifies_plan_review_after_plan_draft_with_contract_p1_history(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    text = runbook.read_text(encoding="utf-8")
    runbook.write_text(
        text.replace(
            "| ⑥销售B2B | 可进入Spec层 | ✅FSN-553 Story | — | — | ✅FSN-553 | — | — |",
            "| ②人力/KPI | 第6步✅Plan Draft v1已产出；Plan=`docs/plans/Tier1_②/人力KPI_plan.md`；下一合法动作=Plan三方审查；仍禁止Code/PR/CI/staging/部署 | ✅Story | ✅Spec Draft v3 | ✅Contract Draft v1.1；Round1：Qwen PASS(P0=0/P1=1/P2=1)；P1吸收=`docs/reviews/contract/Tier1_②/absorb.md`；Repair三方PASS：DeepSeek PASS(P0=0/P1=0/P2=1)、Qwen PASS(P0=0/P1=0/P2=1)、Zhipu PASS(P0=0/P1=0/P2=0)；Final=`docs/reviews/contract/Tier1_②/final.md`；✅Plan Draft v1=`docs/plans/Tier1_②/人力KPI_plan.md` | 待按Plan粒度建票 | 仅允许Plan三方审查；Plan 3/3 PASS(P0=0/P1=0)前仍禁止Code/PR/CI/staging/部署 | Plan已携带P2 |",
        ),
        encoding="utf-8",
    )

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=False,
            board="default",
        )
    )

    assert result["heartbeat"]["target"]["module"] == "②人力/KPI"
    assert result["heartbeat"]["target"]["stage"] == "plan_review"
    assert result["heartbeat"]["mode"] != "frozen"


def test_fesun_classifies_pr_breakdown_after_plan_pass(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    state = hermes_fesun_watchdog.load_state()
    state["attempts"] = {"产品成本:inspect": 99, "产品成本:plan": 1}
    hermes_fesun_watchdog.save_state(state)
    text = runbook.read_text(encoding="utf-8")
    runbook.write_text(
        text.replace(
            "| ⑥销售B2B | 可进入Spec层 | ✅FSN-553 Story | — | — | ✅FSN-553 | — | — |",
            "| ⑤产品/成本 | 第6步✅Plan三方审查3/3 PASS(P0=0/P1=0/P2=0)；下一合法动作=PR拆解/Code前准备；仍禁止Code/PR/CI/staging/部署 | ✅FSN-552 Story | ✅Spec Round1闭合 | ✅Contract v1.1三方门禁PASS；Plan Draft v1=`docs/plans/Tier1_⑤/FSN-552_产品成本_plan.md`；Plan Review final=`docs/reviews/plan/Tier1_⑤/FSN-552_plan_round1_final_pass_2026-07-05.md` | ✅FSN-552 | 下一步仅允许PR拆解/Code前准备；未进Code/PR/CI | Code前硬门禁：FSN-554/555 Linear UI核验 + Golden fixture schema落地review + 护栏清单A-K全绿 |",
        ),
        encoding="utf-8",
    )

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=False,
            board="default",
            no_progress_threshold=0,
        )
    )

    assert result["heartbeat"]["target"]["module"] == "⑤产品/成本"
    assert result["heartbeat"]["target"]["stage"] == "pr_breakdown"
    assert result["heartbeat"]["mode"] != "frozen"


def test_fesun_prioritizes_current_next_action_over_stale_dev_column(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    state = hermes_fesun_watchdog.load_state()
    state["attempts"] = {"产品成本:pr_breakdown": 99}
    hermes_fesun_watchdog.save_state(state)
    text = runbook.read_text(encoding="utf-8")
    runbook.write_text(
        text.replace(
            "| ⑥销售B2B | 可进入Spec层 | ✅FSN-553 Story | — | — | ✅FSN-553 | — | — |",
            "| ⑤产品/成本 | 第6步✅Plan三方审查3/3 PASS(P0=0/P1=0/P2=0)+PR拆解卡已产出；下一合法动作=Code前门禁核验(FSN-554/555 Linear UI + Golden fixture schema落地review + 护栏清单A-K全绿)；仍禁止Code/PR/CI/staging/部署 | ✅FSN-552 Story | ✅Spec Round1闭合 | ✅Contract v1.1三方门禁PASS；Plan Draft v1=`docs/plans/Tier1_⑤/FSN-552_产品成本_plan.md`；Plan Review final=`docs/reviews/plan/Tier1_⑤/FSN-552_plan_round1_final_pass_2026-07-05.md` | ✅FSN-552 | 下一步仅允许PR拆解/Code前准备；未进Code/PR/CI | PR拆解卡+追溯矩阵已产出；Code前硬门禁：FSN-554/555 Linear UI核验 + Golden fixture schema落地review + 护栏清单A-K全绿 |",
        ),
        encoding="utf-8",
    )

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=False,
            board="default",
            no_progress_threshold=0,
        )
    )

    assert result["heartbeat"]["target"]["module"] == "⑤产品/成本"
    assert result["heartbeat"]["target"]["next_action"].startswith("Code前门禁核验")
    assert result["heartbeat"]["target"]["stage"] == "code_preflight"
    assert result["heartbeat"]["mode"] != "frozen"


def test_fesun_unknown_next_action_gets_stable_non_inspect_stage(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    state = hermes_fesun_watchdog.load_state()
    state["attempts"] = {"产品成本:inspect": 99}
    hermes_fesun_watchdog.save_state(state)
    text = runbook.read_text(encoding="utf-8")
    runbook.write_text(
        text.replace(
            "| ⑥销售B2B | 可进入Spec层 | ✅FSN-553 Story | — | — | ✅FSN-553 | — | — |",
            "| ⑤产品/成本 | 第6步✅Plan三方审查PASS；下一合法动作=供应链旁路核验X；仍禁止Code/PR/CI/staging/部署 | ✅FSN-552 Story | ✅Spec Round1闭合 | ✅Contract v1.1三方门禁PASS | ✅FSN-552 | — | — |",
        ),
        encoding="utf-8",
    )

    first = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=False,
            board="default",
            no_progress_threshold=0,
        )
    )
    second = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=False,
            board="default",
            no_progress_threshold=0,
        )
    )

    assert first["heartbeat"]["target"]["stage"].startswith("next_action_")
    assert first["heartbeat"]["target"]["stage"] != "inspect"
    assert second["heartbeat"]["target"]["stage"] == first["heartbeat"]["target"]["stage"]
    assert second["heartbeat"]["mode"] != "frozen"


def test_fesun_external_upstream_blocker_does_not_dispatch_or_freeze(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    state = hermes_fesun_watchdog.load_state()
    state["attempts"] = {"产品成本:next_action_6a660d93": 99}
    state["no_progress_count"] = 99
    hermes_fesun_watchdog.save_state(state)
    text = runbook.read_text(encoding="utf-8")
    runbook.write_text(
        text.replace(
            "| ⑥销售B2B | 可进入Spec层 | ✅FSN-553 Story | — | — | ✅FSN-553 | — | — |",
            "| ⑤产品/成本 | guardrail cleanup进行中：H1-H4反缩水机器闸已绿；护栏清单A-K仍BLOCKED（J `pip-audit`剩Starlette 5漏洞，A/D/E/F/I3/I5仍需外部取证/权限处理）；下一合法动作=处理J上游/外部权限护栏未绿项；仍禁止Code/PR/CI/staging/部署 | ✅FSN-552 Story | ✅Spec Round1闭合 | ✅Contract v1.1三方门禁PASS | ✅FSN-552 | Code前门禁仍BLOCKED（H1-H4已绿；J/A/D/E/F/I3/I5未全绿）；未进Code/PR/CI | J仍BLOCKED于`starlette 0.49.3` 5个上游未发布修复漏洞；A/D/E/F/I3/I5仍需外部权限/取证处理 |",
        ),
        encoding="utf-8",
    )

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=True,
            board="default",
            no_progress_threshold=0,
            max_stage_attempts=1,
        )
    )

    heartbeat = result["heartbeat"]
    assert heartbeat["target"]["module"] == "⑤产品/成本"
    assert heartbeat["target"]["next_action"] == "处理J上游/外部权限护栏未绿项"
    assert heartbeat["mode"] == "external_blocked"
    assert heartbeat["created_tasks"] == []
    assert heartbeat["wake_agent"] is False
    assert heartbeat["no_progress_count"] == 0
    assert heartbeat["guards"]["external"]["kind"] == "upstream_unavailable"
    assert "upstream" in heartbeat["blocked_reason"].lower()


def test_fesun_upstream_risk_exception_allows_supervised_dispatch(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    text = runbook.read_text(encoding="utf-8")
    runbook.write_text(
        text.replace(
            "| ⑥销售B2B | 可进入Spec层 | ✅FSN-553 Story | — | — | ✅FSN-553 | — | — |",
            "| ⑤产品/成本 | guardrail cleanup已完成：护栏supervised clearance已完成（A/D/E/F/I3/I5/K已闭合；J `pip-audit`剩Starlette 5漏洞，按上游未发布例外/等待处理）；下一合法动作=进入受监督Code/PR准备；部署仍禁止，J生产发布前复核 | ✅FSN-552 Story | ✅Spec Round1闭合 | ✅Contract v1.1三方门禁PASS | ✅FSN-552 | Code前门禁supervised已解锁（A/D/E/F/I3/I5/K已闭合，J例外/等待）；未进Code/PR/CI | J例外/等待；生产发布前复核 |",
        ),
        encoding="utf-8",
    )

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=True,
            board="default",
            no_progress_threshold=0,
        )
    )

    heartbeat = result["heartbeat"]
    assert heartbeat["target"]["module"] == "⑤产品/成本"
    assert heartbeat["mode"] in {"dispatched", "waiting_on_active_task"}
    assert heartbeat["created_tasks"][0]["module"] == "⑤产品/成本"
    assert heartbeat["guards"]["external"]["kind"] == "upstream_exception_recorded"
    assert heartbeat["blocked_reason"] == ""


def test_fesun_supervised_code_pr_ready_does_not_retry_next_action(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    state = hermes_fesun_watchdog.load_state()
    state["attempts"] = {"产品成本:next_action_8fdb1fbc": 3}
    state["attempt_fingerprints"] = {"产品成本:next_action_8fdb1fbc": "old"}
    state["no_progress_count"] = 151
    hermes_fesun_watchdog.save_state(state)
    text = runbook.read_text(encoding="utf-8")
    runbook.write_text(
        text.replace(
            "| ⑥销售B2B | 可进入Spec层 | ✅FSN-553 Story | — | — | ✅FSN-553 | — | — |",
            "| ⑤产品/成本 | guardrail cleanup已完成：code_preflight 2/3 PASS（FSN-554/555 Linear实时核验✅；Golden fixture schema设计级review✅）+ H1-H4反缩水机器闸已绿；护栏supervised clearance已完成（A/D/E/F/I3/I5/K已闭合；J `pip-audit`剩Starlette 5漏洞，按上游未发布例外/等待处理）；下一合法动作=进入受监督Code/PR准备；部署仍禁止，J生产发布前复核 | ✅FSN-552 Story+Flow+蓝图映射表 | ✅Spec Round1闭合(P0=0/P1=0/P2=3) | ✅Contract v1.1三方门禁PASS；Plan Review final=`docs/reviews/plan/Tier1_⑤/FSN-552_plan_round1_final_pass_2026-07-05.md`；next_action_8fdb1fbc code-ready gate=`docs/reviews/plan/Tier1_⑤/FSN-552_next_action_8fdb1fbc_gate_2026-07-07.md`（supersedes earlier blocked stance for supervised Code/PR readiness only） | ✅FSN-552 | Code前门禁supervised已解锁（H1-H4已绿；A/D/E/F/I3/I5/K已闭合，J例外/等待）；未进Code/PR/CI | 下一合法动作只能是振英或振英盯着的 supervised 会话，按 PR1→PR6 执行 |",
        ),
        encoding="utf-8",
    )

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=True,
            board="default",
            no_progress_threshold=0,
            max_stage_attempts=1,
        )
    )

    heartbeat = result["heartbeat"]
    assert heartbeat["mode"] == "all_green"
    assert heartbeat["target"] is None
    assert heartbeat["created_tasks"] == []
    assert heartbeat["supervised_ready_modules"] == [
        {
            "module": "⑤产品/成本",
            "stage": "supervised_code_pr_ready",
            "next_action": "进入受监督Code/PR准备",
        }
    ]
    assert heartbeat["attempt_resets"][0]["stage"] == "产品成本:next_action_8fdb1fbc"
    assert heartbeat["attempt_resets"][0]["reason"] == "supervised Code/PR handoff ready"
    assert "产品成本:next_action_8fdb1fbc" not in result["state"]["attempts"]
    status_text = hermes_fesun_watchdog.visible_status_path().read_text(encoding="utf-8")
    assert "受监督 Code/PR 就绪：1" in status_text
    assert "SPEC/门禁已完成，待受监督Code/PR" in status_text


def test_fesun_legacy_biz_row_waiting_development_needs_plan(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    text = runbook.read_text(encoding="utf-8")
    runbook.write_text(
        text.replace(
            "| ⑥销售B2B | 可进入Spec层 | ✅FSN-553 Story | — | — | ✅FSN-553 | — | — |",
            "| ④商务 | 第3步✅三模型外审PASS(`docs/specs/商务/`五件套) | ✅12条(BS-01~BS-12) | ✅含故事/进入路径/边界 | ✅C-01~C-13 | ✅FSN-496~FSN-507 | 待开发 | attribution分摊原则已定 |",
        ),
        encoding="utf-8",
    )

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=False,
            board="default",
        )
    )

    assert result["heartbeat"]["target"]["module"] == "④商务"
    assert result["heartbeat"]["target"]["stage"] == "plan"
    assert result["heartbeat"]["mode"] != "all_green"


def test_fesun_finance_true_gap_split_needs_pr_breakdown(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    text = runbook.read_text(encoding="utf-8")
    runbook.write_text(
        text.replace(
            "| ⑥销售B2B | 可进入Spec层 | ✅FSN-553 Story | — | — | ✅FSN-553 | — | — |",
            "| ③财务 | 第0步✅Layer Audit完成·非绿地收口+验四关PASS | — | ✅code_audit/coverage/brief | ✅本仓=ERPNext执行端+ACL | ✅FSN-551 | 待真缺口拆分 | 真缺口=签收事件落点、determineOrderNature、外置BASE_URL联调、持久幂等 |",
        ),
        encoding="utf-8",
    )

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=False,
            board="default",
        )
    )

    assert result["heartbeat"]["target"]["module"] == "③财务"
    assert result["heartbeat"]["target"]["stage"] == "pr_breakdown"
    assert result["heartbeat"]["mode"] != "all_green"


def test_fesun_contract_center_waiting_pr_split_is_not_done(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    text = runbook.read_text(encoding="utf-8")
    runbook.write_text(
        text.replace(
            "| ⑥销售B2B | 可进入Spec层 | ✅FSN-553 Story | — | — | ✅FSN-553 | — | — |",
            "| ⑨合同中心 | 第4步✅已建故事子票(FSN-510~542，父FSN-508) | ✅33条 | ✅含故事/验四关PASS | ✅三模型PASS | ✅FSN-508 + 33子票 | 待开发拆PR | D1/D3/D4·告警vs硬拦·CRM API·staging实测 |",
        ),
        encoding="utf-8",
    )

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=False,
            board="default",
        )
    )

    assert result["heartbeat"]["target"]["module"] == "⑨合同中心"
    assert result["heartbeat"]["target"]["stage"] == "pr_breakdown"
    assert result["heartbeat"]["mode"] != "all_green"


def test_fesun_parks_external_blocker_and_dispatches_next_runnable_module(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    text = runbook.read_text(encoding="utf-8")
    runbook.write_text(
        text.replace(
            "| ⑥销售B2B | 可进入Spec层 | ✅FSN-553 Story | — | — | ✅FSN-553 | — | — |",
            "| ⑤产品/成本 | 护栏清单A-K仍BLOCKED（J `pip-audit`剩Starlette 5漏洞）；下一合法动作=处理J上游/外部权限护栏未绿项；仍禁止Code/PR/CI/staging/部署 | ✅FSN-552 Story | ✅Spec Round1闭合 | ✅Contract v1.1三方门禁PASS | ✅FSN-552 | Code前门禁仍BLOCKED | J仍BLOCKED于`starlette 0.49.3` 5个上游未发布修复漏洞 |\n"
            "| ⑥销售B2B | 可进入Spec层 | ✅FSN-553 Story | — | — | ✅FSN-553 | — | — |\n"
            "| ②人力/KPI | code_preflight 2/3 PASS；下一合法动作=处理外部护栏 blocker | ✅Story | ✅Spec | ✅Contract+Plan PASS | 待按Plan粒度建票 | Code前门禁仍BLOCKED | 外部护栏 blocker |",
        ),
        encoding="utf-8",
    )

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=True,
            board="default",
            no_progress_threshold=0,
        )
    )

    heartbeat = result["heartbeat"]
    assert heartbeat["target"]["module"] == "⑥销售B2B"
    assert heartbeat["target"]["stage"] == "spec"
    assert heartbeat["mode"] in {"dispatched", "waiting_on_active_task"}
    assert heartbeat["created_tasks"][0]["module"] == "⑥销售B2B"
    assert heartbeat["parked_external_blockers"][0]["module"] == "⑤产品/成本"
    assert heartbeat["parked_external_blockers"][0]["kind"] == "upstream_unavailable"
    assert heartbeat["parked_external_blockers"][1]["module"] == "②人力/KPI"
    assert heartbeat["parked_external_blockers"][1]["kind"] == "external_permission_required"
    assert heartbeat["runnable_count"] == 1
    status_text = hermes_fesun_watchdog.visible_status_path().read_text(encoding="utf-8")
    assert "停靠外部阻塞：2" in status_text
    assert "当前目标：⑥销售B2B / spec" in status_text


def test_fesun_external_permission_blocker_does_not_dispatch(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    text = runbook.read_text(encoding="utf-8")
    runbook.write_text(
        text.replace(
            "| ⑥销售B2B | 可进入Spec层 | ✅FSN-553 Story | — | — | ✅FSN-553 | — | — |",
            "| ⑤产品/成本 | 下一合法动作=GitHub服务端Settings/API取证与token收窄；仍禁止Code/PR/CI/staging/部署 | ✅FSN-552 Story | ✅Spec Round1闭合 | ✅Contract v1.1三方门禁PASS | ✅FSN-552 | 需要外部权限处理 | A/I3需服务端取证 |",
        ),
        encoding="utf-8",
    )

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=True,
            board="default",
            no_progress_threshold=0,
        )
    )

    heartbeat = result["heartbeat"]
    assert heartbeat["mode"] == "external_blocked"
    assert heartbeat["created_tasks"] == []
    assert heartbeat["guards"]["external"]["kind"] == "external_permission_required"


def test_fesun_staging_forbidden_text_is_not_external_blocker(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    text = runbook.read_text(encoding="utf-8")
    runbook.write_text(
        text.replace(
            "| ⑥销售B2B | 可进入Spec层 | ✅FSN-553 Story | — | — | ✅FSN-553 | — | — |",
            "| ⑦门店B2C | 第4步✅Story/Flow Gate 3/3 PASS；下一合法动作=Spec三方审查 | ✅Story | ✅Spec Draft v1 | ✅Brief三方审查PASS | — | 仅允许派发/回收Spec三方审查；Spec 3/3 PASS前仍禁止Contract/Code/PR/CI/staging/部署 | — |",
        ),
        encoding="utf-8",
    )

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=False,
            board="default",
        )
    )

    heartbeat = result["heartbeat"]
    assert heartbeat["target"]["module"] == "⑦门店B2C"
    assert heartbeat["target"]["stage"] == "spec_review"
    assert heartbeat["guards"]["external"]["kind"] == "none"
    assert heartbeat["parked_external_blockers"] == []


def test_fesun_parks_authority_required_review_and_continues_queue(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    text = runbook.read_text(encoding="utf-8")
    runbook.write_text(
        text.replace(
            "| ⑥销售B2B | 可进入Spec层 | ✅FSN-553 Story | — | — | ✅FSN-553 | — | — |",
            "| ②人力/KPI | 第3步⚠️Spec Round2 watchdog BLOCKED：Zhipu/Qwen PASS但来源未证；只能由振英直接触发/回收权威 Round2 Spec 三方审查；仍禁止Contract/Code/PR/CI/staging/部署 | ✅Story | ✅Spec Draft v2 | ✅Story/Flow PASS | — | 权威 Round2 3/3 前不得进Contract | Codex/DeepSeek Round2缺失 |\n"
            "| ⑦门店B2C | 第4步✅Story/Flow Gate 3/3 PASS；下一合法动作=Spec三方审查 | ✅Story | ✅Spec Draft v1 | ✅Brief三方审查PASS | — | 仅允许派发/回收Spec三方审查；Spec 3/3 PASS前仍禁止Contract/Code/PR/CI/staging/部署 | — |",
        ),
        encoding="utf-8",
    )

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=False,
            board="default",
        )
    )

    heartbeat = result["heartbeat"]
    assert heartbeat["target"]["module"] == "⑦门店B2C"
    assert heartbeat["target"]["stage"] == "spec_review"
    assert heartbeat["parked_external_blockers"][0]["module"] == "②人力/KPI"
    assert heartbeat["parked_external_blockers"][0]["kind"] == "external_authority_required"


def test_fesun_tick_allows_plan_docs(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    (repo / "docs" / "plans" / "Tier1_⑤").mkdir(parents=True)
    (repo / "docs" / "plans" / "Tier1_⑤" / "FSN-552_plan.md").write_text(
        "# Plan\n",
        encoding="utf-8",
    )

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=False,
            board="default",
        )
    )

    assert "docs/plans/Tier1_⑤/FSN-552_plan.md" in result["heartbeat"]["guards"]["diff"]["changed"]
    assert result["heartbeat"]["guards"]["diff"]["new_out_of_scope"] == []
    assert result["heartbeat"]["guards"]["diagnosis"]["kind"] == "none"


def test_fesun_tick_allows_unicode_runbook_update(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    runbook.write_text(runbook.read_text(encoding="utf-8") + "\n<!-- §E update -->\n", encoding="utf-8")

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=False,
            board="default",
        )
    )

    assert "docs/plans/00_九模块开发SOP与状态台账_权威runbook.md" in result["heartbeat"]["guards"]["diff"]["changed"]
    assert result["heartbeat"]["guards"]["diff"]["new_out_of_scope"] == []
    assert result["heartbeat"]["guards"]["diagnosis"]["kind"] == "none"


def test_fesun_install_records_existing_out_of_scope_baseline(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    (repo / ".github").mkdir()
    (repo / ".github" / "copilot-instructions.md").write_text("existing\n", encoding="utf-8")

    data = hermes_fesun_watchdog.install(
        repo=str(repo),
        runbook=str(runbook),
        board="default",
        assignee="default",
        create_tasks=False,
        dispatch=False,
    )

    assert data["ok"] is True
    assert ".github/copilot-instructions.md" in data["state"]["baseline_out_of_scope"]
    assert (tmp_path / "home" / "scripts" / "fesun_nine_spec_watchdog.py").exists()
    jobs_file = tmp_path / "home" / "cron" / "jobs.json"
    assert jobs_file.exists()
    jobs = json.loads(jobs_file.read_text(encoding="utf-8"))["jobs"]
    assert [job["id"] for job in jobs] == [data["job"]["id"]]
    assert jobs[0]["workdir"] == str(repo.resolve())
    script = (tmp_path / "home" / "scripts" / "fesun_nine_spec_watchdog.py").read_text(encoding="utf-8")
    assert '"create_tasks": false' in script
    assert '"dispatch": false' in script
    assert "os.environ['HERMES_FESUN_HEADLESS'] = '1'" in script


def test_fesun_closure_stall_commits_docs_and_completes_running_task(tmp_path, monkeypatch):
    hermes_fesun_watchdog, kanban_db = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    runbook.write_text(
        runbook.read_text(encoding="utf-8").replace(
            "| ⑥销售B2B | 可进入Spec层 | ✅FSN-553 Story | — | — | ✅FSN-553 | — | — |",
            "| ②人力/KPI | code_preflight 2/3 PASS；下一合法动作=处理外部权限护栏未绿项；仍禁止Code/PR/CI/staging/部署 | ✅Story | ✅Spec Draft v3 | ✅Contract+Plan PASS | 待按Plan粒度建票 | Code前门禁仍BLOCKED | 外部权限/I3/I5/J/K still blocked |",
        ),
        encoding="utf-8",
    )
    review_dir = repo / "docs" / "reviews" / "plan" / "Tier1_②"
    review_dir.mkdir(parents=True)
    review_file = review_dir / "人力KPI_code_preflight_guardrail_audit_2026-07-06.md"
    review_file.write_text("# ②人力/KPI code_preflight\n\n护栏 A-K BLOCKED。\n", encoding="utf-8")
    with kanban_db.connect(board="default") as conn:
        conn.execute(
            """
            INSERT INTO tasks (
                id, title, status, assignee, created_at, started_at,
                last_heartbeat_at, worker_pid, idempotency_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "t_close",
                "Fesun SPEC watchdog: ②人力/KPI code_preflight",
                "running",
                "default",
                1,
                1,
                2,
                999999,
                "fesun-nine-spec:人力KPI:code_preflight",
            ),
        )
        conn.commit()
    log_path = kanban_db.worker_log_path("t_close", board="default")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "任务 t_close（②人力/KPI code_preflight）已完成了 SPEC worker 能做的一切。\n"
        "Three review documents were written the results to disk, but closure did not happen.\n",
        encoding="utf-8",
    )

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=True,
            board="default",
            no_progress_threshold=999,
            closure_stall_seconds=0,
        )
    )

    recovery = result["heartbeat"]["closure_recoveries"][0]
    assert recovery["task_id"] == "t_close"
    assert recovery["status"] == "completed"
    assert recovery["completion"]["completed"] is True
    assert recovery["commit"]["status"] == "committed"
    assert "docs/reviews/plan/Tier1_②/人力KPI_code_preflight_guardrail_audit_2026-07-06.md" in recovery["paths"]
    assert result["heartbeat"]["active_tasks"] == []
    with kanban_db.connect(board="default") as conn:
        row = conn.execute("SELECT status, result FROM tasks WHERE id = ?", ("t_close",)).fetchone()
    assert row["status"] == "done"
    assert "closure-stall recovered" in row["result"]
    latest = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert latest == "docs(fesun): 人力KPI close code_preflight gate"


def test_fesun_closure_stall_requires_completion_log_signal(tmp_path, monkeypatch):
    hermes_fesun_watchdog, kanban_db = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    review_dir = repo / "docs" / "reviews" / "plan" / "Tier1_②"
    review_dir.mkdir(parents=True)
    (review_dir / "人力KPI_code_preflight_guardrail_audit_2026-07-06.md").write_text(
        "# Draft still in progress\n",
        encoding="utf-8",
    )
    with kanban_db.connect(board="default") as conn:
        conn.execute(
            """
            INSERT INTO tasks (
                id, title, status, assignee, created_at, started_at,
                last_heartbeat_at, worker_pid, idempotency_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "t_no_close",
                "Fesun SPEC watchdog: ②人力/KPI code_preflight",
                "running",
                "default",
                1,
                1,
                2,
                999999,
                "fesun-nine-spec:人力KPI:code_preflight",
            ),
        )
        conn.commit()
    log_path = kanban_db.worker_log_path("t_no_close", board="default")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("仍在读取资料和思考，尚未形成结论。\n", encoding="utf-8")

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=True,
            board="default",
            no_progress_threshold=999,
            closure_stall_seconds=0,
        )
    )

    assert result["heartbeat"]["closure_recoveries"] == []
    assert result["heartbeat"]["mode"] == "active_worker"
    with kanban_db.connect(board="default") as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", ("t_no_close",)).fetchone()
    assert row["status"] == "running"
    latest = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert latest == "init"


def test_fesun_closure_stall_closes_when_runbook_stage_advanced(tmp_path, monkeypatch):
    hermes_fesun_watchdog, kanban_db = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    runbook.write_text(
        runbook.read_text(encoding="utf-8").replace(
            "| ⑥销售B2B | 可进入Spec层 | ✅FSN-553 Story | — | — | ✅FSN-553 | — | — |",
            "| ⑧专项制度 | 第6步✅Contract Draft v1已产出；Contract=`docs/contracts/Tier1_⑧/专项制度_contract.md`；下一合法动作=Contract三方审查；仍禁止Plan/Code/PR/CI/staging/部署 | ✅Story | ✅Spec repair三方PASS | ✅Contract Draft v1；待三方Contract审查 | ✅FSN-561 | 下一合法动作=Contract三方审查 | 待审查 |",
        ),
        encoding="utf-8",
    )
    contract_dir = repo / "docs" / "contracts" / "Tier1_⑧"
    contract_dir.mkdir(parents=True)
    contract_file = contract_dir / "专项制度_contract.md"
    contract_file.write_text("# ⑧专项制度 Contract Draft v1\n", encoding="utf-8")
    review_dir = repo / "docs" / "reviews" / "contract" / "Tier1_⑧_gate"
    review_dir.mkdir(parents=True)
    (review_dir / "contract_review_prompt_2026-07-06.md").write_text(
        "# Contract Review Prompt\n",
        encoding="utf-8",
    )
    with kanban_db.connect(board="default") as conn:
        conn.execute(
            """
            INSERT INTO tasks (
                id, title, status, assignee, created_at, started_at,
                last_heartbeat_at, worker_pid, idempotency_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "t_stage_advanced",
                "Fesun SPEC watchdog: ⑧专项制度 contract",
                "running",
                "default",
                1,
                1,
                2,
                999999,
                "fesun-nine-spec:专项制度:contract",
            ),
        )
        conn.commit()
    log_path = kanban_db.worker_log_path("t_stage_advanced", board="default")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("正在整理上下文，没有显式完成句。\n", encoding="utf-8")

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=True,
            board="default",
            no_progress_threshold=999,
            closure_stall_seconds=0,
        )
    )

    recovery = result["heartbeat"]["closure_recoveries"][0]
    assert recovery["task_id"] == "t_stage_advanced"
    assert recovery["signals"] == {"completion_log": False, "stage_advanced": True}
    assert recovery["status"] == "completed"
    assert "docs/contracts/Tier1_⑧/专项制度_contract.md" in recovery["paths"]
    with kanban_db.connect(board="default") as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", ("t_stage_advanced",)).fetchone()
    assert row["status"] == "done"
    latest = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert latest == "docs(fesun): FSN-561 close contract gate"


def test_fesun_orphan_closure_evidence_commits_before_next_dispatch(tmp_path, monkeypatch):
    hermes_fesun_watchdog, _ = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    runbook.write_text(
        runbook.read_text(encoding="utf-8").replace(
            "| ⑥销售B2B | 可进入Spec层 | ✅FSN-553 Story | — | — | ✅FSN-553 | — | — |",
            "| ⑧专项制度 | 第6步✅Contract Draft v1已产出；Contract=`docs/contracts/Tier1_⑧/专项制度_contract.md`；下一合法动作=Contract三方审查；仍禁止Plan/Code/PR/CI/staging/部署 | ✅Story | ✅Spec repair三方PASS | ✅Contract Draft v1；待三方Contract审查 | ✅FSN-561 | 下一合法动作=Contract三方审查 | 待审查 |",
        ),
        encoding="utf-8",
    )
    contract_dir = repo / "docs" / "contracts" / "Tier1_⑧"
    contract_dir.mkdir(parents=True)
    contract_file = contract_dir / "专项制度_contract.md"
    contract_file.write_text("# ⑧专项制度 Contract Draft v1\n", encoding="utf-8")
    review_dir = repo / "docs" / "reviews" / "contract" / "Tier1_⑧_gate"
    review_dir.mkdir(parents=True)
    (review_dir / "contract_review_prompt_2026-07-06.md").write_text(
        "# Contract Review Prompt\n",
        encoding="utf-8",
    )
    old_review_dir = repo / "docs" / "reviews" / "brief" / "Tier1_⑧_gate"
    old_review_dir.mkdir(parents=True)
    (old_review_dir / "brief_review_with_old_trailing_space.md").write_text(
        "# Old brief review  \n",
        encoding="utf-8",
    )

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=True,
            board="default",
            no_progress_threshold=999,
        )
    )

    recovery = result["heartbeat"]["closure_recoveries"][0]
    assert recovery["task_id"] == "orphan:专项制度:contract"
    assert recovery["signals"] == {"orphan_evidence": True}
    assert recovery["status"] == "completed"
    assert recovery["commit"]["status"] == "committed"
    assert "docs/reviews/brief/Tier1_⑧_gate/brief_review_with_old_trailing_space.md" not in recovery["paths"]
    assert result["heartbeat"]["created_tasks"] == []
    latest = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert latest == "docs(fesun): FSN-561 close contract gate"


def test_fesun_orphan_closure_evidence_commits_while_next_review_active(tmp_path, monkeypatch):
    hermes_fesun_watchdog, kanban_db = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    runbook.write_text(
        runbook.read_text(encoding="utf-8").replace(
            "| ⑥销售B2B | 可进入Spec层 | ✅FSN-553 Story | — | — | ✅FSN-553 | — | — |",
            "| ⑧专项制度 | 第6步✅Contract Draft v1已产出；Contract=`docs/contracts/Tier1_⑧/专项制度_contract.md`；下一合法动作=Contract三方审查；仍禁止Plan/Code/PR/CI/staging/部署 | ✅Story | ✅Spec repair三方PASS | ✅Contract Draft v1；待三方Contract审查 | ✅FSN-561 | 下一合法动作=Contract三方审查 | 待审查 |",
        ),
        encoding="utf-8",
    )
    contract_dir = repo / "docs" / "contracts" / "Tier1_⑧"
    contract_dir.mkdir(parents=True)
    (contract_dir / "专项制度_contract.md").write_text("# ⑧专项制度 Contract Draft v1\n", encoding="utf-8")
    review_dir = repo / "docs" / "reviews" / "contract" / "Tier1_⑧_gate"
    review_dir.mkdir(parents=True)
    (review_dir / "contract_review_prompt_2026-07-06.md").write_text(
        "# Contract Review Prompt\n",
        encoding="utf-8",
    )
    with kanban_db.connect(board="default") as conn:
        conn.execute(
            """
            INSERT INTO tasks (
                id, title, status, assignee, created_at, started_at,
                last_heartbeat_at, worker_pid, idempotency_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "t_review_active",
                "Fesun SPEC watchdog: ⑧专项制度 contract_review",
                "running",
                "default",
                1,
                1,
                2,
                999999,
                "fesun-nine-spec:专项制度:contract_review",
            ),
        )
        conn.commit()

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=True,
            board="default",
            no_progress_threshold=999,
        )
    )

    recovery = result["heartbeat"]["closure_recoveries"][0]
    assert recovery["task_id"] == "orphan:专项制度:contract"
    assert recovery["status"] == "completed"
    assert result["heartbeat"]["active_tasks"][0]["id"] == "t_review_active"
    with kanban_db.connect(board="default") as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", ("t_review_active",)).fetchone()
    assert row["status"] == "running"
    latest = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert latest == "docs(fesun): FSN-561 close contract gate"


def test_fesun_orphan_closure_skips_review_only_changes_while_review_active(tmp_path, monkeypatch):
    hermes_fesun_watchdog, kanban_db = _modules(tmp_path, monkeypatch)
    repo, runbook = _repo(tmp_path)
    runbook.write_text(
        runbook.read_text(encoding="utf-8").replace(
            "| ⑥销售B2B | 可进入Spec层 | ✅FSN-553 Story | — | — | ✅FSN-553 | — | — |",
            "| ⑧专项制度 | 第6步✅Contract Draft v1已产出；Contract=`docs/contracts/Tier1_⑧/专项制度_contract.md`；下一合法动作=Contract三方审查；仍禁止Plan/Code/PR/CI/staging/部署 | ✅Story | ✅Spec repair三方PASS | ✅Contract Draft v1；待三方Contract审查 | ✅FSN-561 | 下一合法动作=Contract三方审查 | 待审查 |",
        ),
        encoding="utf-8",
    )
    contract_dir = repo / "docs" / "contracts" / "Tier1_⑧"
    contract_dir.mkdir(parents=True)
    (contract_dir / "专项制度_contract.md").write_text("# already committed\n", encoding="utf-8")
    review_dir = repo / "docs" / "reviews" / "contract" / "Tier1_⑧_gate"
    review_dir.mkdir(parents=True)
    script = review_dir / "run_contract_reviews_2026-07-06.py"
    script.write_text("print('ready')\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "seed contract review"], cwd=repo, check=True, capture_output=True)
    script.write_text("print('worker touched review script')\n", encoding="utf-8")
    with kanban_db.connect(board="default") as conn:
        conn.execute(
            """
            INSERT INTO tasks (
                id, title, status, assignee, created_at, started_at,
                last_heartbeat_at, worker_pid, idempotency_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "t_review_active",
                "Fesun SPEC watchdog: ⑧专项制度 contract_review",
                "running",
                "default",
                1,
                1,
                2,
                999999,
                "fesun-nine-spec:专项制度:contract_review",
            ),
        )
        conn.commit()

    result = hermes_fesun_watchdog.tick(
        hermes_fesun_watchdog.FesunTickOptions(
            repo=str(repo),
            runbook=str(runbook),
            create_tasks=True,
            board="default",
            no_progress_threshold=999,
        )
    )

    assert result["heartbeat"]["closure_recoveries"] == []
    latest = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert latest == "seed contract review"
