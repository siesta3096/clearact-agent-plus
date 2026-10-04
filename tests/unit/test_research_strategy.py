from clearact.domain.models import ChatMessage, Run
from clearact.runtime.research_strategy import classify_research_strategy


def test_uploaded_file_uses_local_first_strategy():
    run = Run(
        goal="根据附件中的比赛规则制作材料",
        messages=[ChatMessage(
            role="user",
            content="根据附件中的比赛规则制作材料",
            metadata={"attachments": [{"kind": "file", "path": "rules.pdf"}]},
        )],
    )

    strategy = classify_research_strategy(run)

    assert strategy.mode == "local_first"
    assert strategy.requires_local_evidence
    assert strategy.total_search_limit == 2


def test_explicit_web_research_with_attachment_uses_mixed_strategy():
    run = Run(
        goal="根据附件并联网核实最新规则",
        messages=[ChatMessage(role="user", content="根据附件并联网核实最新规则")],
    )

    strategy = classify_research_strategy(run)

    assert strategy.mode == "mixed"
    assert not strategy.requires_local_evidence


def test_explicit_web_research_reads_newly_uploaded_file_first():
    run = Run(
        goal="根据附件并联网核实最新规则",
        messages=[ChatMessage(
            role="user", content="根据附件并联网核实最新规则",
            metadata={"attachments": [{"kind": "file", "path": "rules.txt"}]},
        )],
    )

    strategy = classify_research_strategy(run)

    assert strategy.mode == "mixed"
    assert strategy.requires_local_evidence


def test_explicit_web_research_inspects_workspace_source_first():
    run = Run(goal="先查看工作区文件，再搜索网页核实缺失信息")

    strategy = classify_research_strategy(run)

    assert strategy.mode == "mixed"
    assert strategy.requires_local_evidence


def test_latest_no_web_instruction_overrides_older_research_goal():
    run = Run(
        goal="调查最新行业资料",
        messages=[ChatMessage(role="user", content="只用本地文件，不要联网")],
    )

    assert classify_research_strategy(run).mode == "local_only"


def test_english_no_browsing_instruction_is_local_only():
    run = Run(goal="Read a workspace file. Do not browse the web.")

    assert classify_research_strategy(run).mode == "local_only"


def test_new_local_followup_does_not_inherit_old_web_requirement():
    run = Run(
        goal="调查最新行业资料",
        messages=[
            ChatMessage(role="user", content="调查最新行业资料"),
            ChatMessage(role="user", content="现在整理工作区中的文件"),
        ],
    )

    assert classify_research_strategy(run).mode == "local_first"


def test_research_without_local_material_keeps_wider_search_window():
    run = Run(goal="调查行业竞争格局")

    strategy = classify_research_strategy(run)

    assert strategy.mode == "research"
    assert strategy.search_streak_limit == 5
    assert strategy.total_search_limit is None


def test_current_workspace_is_not_mistaken_for_web_research():
    run = Run(goal="查看当前工作区中的文件并生成清单")

    strategy = classify_research_strategy(run)

    assert strategy.mode == "local_first"
    assert strategy.requires_local_evidence


def test_folder_contents_are_read_before_optional_web_search():
    run = Run(goal="请处理这个文件夹中的文件，整理一份报告")

    strategy = classify_research_strategy(run)

    assert strategy.mode == "local_first"
    assert strategy.requires_local_evidence
    assert strategy.total_search_limit == 2


def test_new_document_does_not_require_existing_local_evidence():
    run = Run(goal="创建一个简短文档")

    strategy = classify_research_strategy(run)

    assert strategy.mode == "local_first"
    assert not strategy.requires_local_evidence


def test_local_first_feedback_is_not_mistaken_for_a_web_search_request():
    run = Run(
        goal="调查最新比赛规则",
        messages=[
            ChatMessage(role="user", content="调查最新比赛规则"),
            ChatMessage(
                role="user",
                content=(
                    "请优先使用已上传的附件和工作区文件，避免不必要的网页搜索；"
                    "仅在确实缺少外部事实时再联网。"
                ),
                metadata={"attachments": [{"kind": "file", "path": "rules.pdf"}]},
            ),
        ],
    )

    strategy = classify_research_strategy(run)

    assert strategy.mode == "local_first"
    assert strategy.requires_local_evidence
    assert strategy.total_search_limit == 2


def test_finish_from_existing_evidence_overrides_old_research_goal():
    run = Run(
        goal="调查最新比赛规则",
        messages=[
            ChatMessage(role="user", content="调查最新比赛规则"),
            ChatMessage(
                role="user",
                content="请基于已经获取的资料尽快形成可用结果，不要继续重复搜索或阅读相同内容。",
            ),
        ],
    )

    strategy = classify_research_strategy(run)

    assert strategy.mode == "local_only"
    assert strategy.total_search_limit == 0
