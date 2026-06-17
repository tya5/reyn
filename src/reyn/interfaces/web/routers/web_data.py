"""REST router — GET /api/web/data.

Returns a `ReynUiData`-shaped JSON snapshot that the shell sets as
`window.OPENUI_DATA` before loading the design.

All fields that cannot be populated from live engine state fall back to
safe empty values / hardcoded samples so the shell never crashes.  Real
engine integration is deepened incrementally in later PRs.

Field populate strategy:
    AGENTS            — AgentRegistry.list_names()  (live)
    RECAP             — empty list  (no run-to-recap adapter yet)
    QUICKSTARTS       — hardcoded sample set
    LIBRARY           — skills router _search_roots() (live)
    CONVO_ARIA        — empty list
    CONVO_ARIA_STUDIO — empty list
    SKILL_GRAPH       — default empty graph
    SKILL_MD          — ""
    RUN_EVENTS        — empty list  (EventStore integration deferred)
    RUNS_LIST         — empty list
    PERMISSIONS       — approvals.yaml keys (live)
    COPY              — hardcoded en/ja full I18nKeys

Per P7: no skill-specific strings in this module.  Agent names / skill
names are read from disk and passed through opaquely.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends

from reyn.interfaces.web.deps import get_project_root, get_registry

router = APIRouter(tags=["web"])


# ── i18n copy (hardcoded, all I18nKeys required) ────────────────────────────


_COPY_EN = {
    "today_morning": "Good morning",
    "today_sub": "Here's what's happening",
    "recap_label": "Today",
    "your_agents": "Your agents",
    "quickstarts": "Quickstarts",
    "seeAll": "See all",
    "chat": "Chat",
    "profile": "Profile",
    "open_studio": "Open Studio",
    "back_app": "Back to App",
    "studio": "Studio",
    "app": "App",
    "today": "Today",
    "agents": "Agents",
    "library": "Library",
    "skills": "Skills",
    "runs": "Runs",
    "evals": "Evals",
    "topologies": "Topologies",
    "settings": "Settings",
    "send": "Send",
    "placeholder": "Message…",
    "permissions": "Permissions",
    "new_agent": "New agent",
    "add_agent": "Add agent",
    "library_lead": "Things your agents can do",
    "agents_lead": "Meet your team",
}

_COPY_JA = {
    "today_morning": "おはようございます",
    "today_sub": "今日の状況",
    "recap_label": "今日",
    "your_agents": "エージェント",
    "quickstarts": "クイックスタート",
    "seeAll": "すべて表示",
    "chat": "チャット",
    "profile": "プロフィール",
    "open_studio": "スタジオを開く",
    "back_app": "アプリに戻る",
    "studio": "スタジオ",
    "app": "アプリ",
    "today": "今日",
    "agents": "エージェント",
    "library": "ライブラリ",
    "skills": "スキル",
    "runs": "実行",
    "evals": "評価",
    "topologies": "トポロジー",
    "settings": "設定",
    "send": "送信",
    "placeholder": "メッセージ…",
    "permissions": "権限",
    "new_agent": "新しいエージェント",
    "add_agent": "エージェントを追加",
    "library_lead": "エージェントができること",
    "agents_lead": "チームの紹介",
}


# ── hardcoded quickstarts ────────────────────────────────────────────────────


_QUICKSTARTS = [
    {"id": "qs-research", "icon": "search", "title": "Research a topic",
     "sub": "Deep-dive any subject and get a structured summary"},
    {"id": "qs-draft",    "icon": "pen",    "title": "Draft a document",
     "sub": "Turn bullet points into polished prose"},
    {"id": "qs-review",   "icon": "check",  "title": "Review & improve",
     "sub": "Get feedback and suggestions on any text"},
    {"id": "qs-plan",     "icon": "list",   "title": "Plan a project",
     "sub": "Break goals into phases and actionable tasks"},
]


# ── helpers ──────────────────────────────────────────────────────────────────


_ANIMAL_CYCLE = ["fox", "otter", "crane", "hare", "owl"]
_COLOR_CYCLE  = ["#7C4DFF", "#00BCD4", "#FF6B35", "#4CAF50", "#FF4081"]


def _build_agents(registry) -> list[dict[str, Any]]:
    """Build Agent list from AgentRegistry. Treat profiles as opaque."""
    try:
        names = registry.list_names()
    except Exception:
        return []

    agents = []
    for i, name in enumerate(names):
        try:
            profile = registry.load_profile(name)
            role = getattr(profile, "role", "") or "Assistant"
        except Exception:
            role = "Assistant"

        last_at = None
        try:
            last_at = registry.last_activity_at(name)
        except Exception:
            pass

        agents.append({
            "id": name,
            "name": name,
            "animal": _ANIMAL_CYCLE[i % len(_ANIMAL_CYCLE)],
            "color": _COLOR_CYCLE[i % len(_COLOR_CYCLE)],
            "role": role[:20] if role else "Assistant",
            "blurb": f"Agent {name}",
            "role_prompt": role,
            "allowed_skills": [],
            "last_active": last_at.strftime("%-d %b %H:%M") if last_at else "never",
            "activity": "",
        })
    return agents


def _build_library(project_root: Path) -> list[dict[str, Any]]:
    """Build Library items from skills (project + local + stdlib)."""
    from reyn.skill.skill_paths import stdlib_root
    try:
        sl = stdlib_root()
    except Exception:
        sl = Path("/nonexistent")

    roots = [
        ("project", project_root / "reyn" / "project"),
        ("local",   project_root / "reyn" / "local"),
        ("stdlib",  sl / "skills"),
    ]

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    color_list = ["#7C4DFF", "#00BCD4", "#FF6B35", "#4CAF50", "#FF4081",
                  "#F06292", "#26A69A", "#FFA726"]

    for _source, skills_dir in roots:
        if not skills_dir.is_dir():
            continue
        for skill_dir in sorted(skills_dir.iterdir()):
            if not skill_dir.is_dir() or not (skill_dir / "skill.md").exists():
                continue
            name = skill_dir.name
            if name in seen:
                continue
            seen.add(name)

            # Try to read description from skill.md frontmatter
            description = ""
            try:
                from reyn.core.compiler.parser import _split_frontmatter
                fm, _ = _split_frontmatter((skill_dir / "skill.md").read_text(encoding="utf-8"))
                description = (fm.get("description") or "").strip()
            except Exception:
                pass

            idx = len(items)
            items.append({
                "id": name,
                "title": name.replace("_", " ").replace("-", " ").title(),
                "sub": description or f"Run the {name} skill",
                "icon": "zap",
                "color": color_list[idx % len(color_list)],
                "tag": "Aria",
            })

    return items


def _build_permissions(project_root: Path) -> list[dict[str, Any]]:
    """Return PermissionRule list from approvals.yaml (opaque keys)."""
    approvals_path = project_root / ".reyn" / "approvals.yaml"
    if not approvals_path.exists():
        return []
    try:
        import yaml
        data = yaml.safe_load(approvals_path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            return []
        rules = []
        for key, approved in data.items():
            # Key format is typically "op:glob" — split best-effort
            parts = str(key).split(":", 1)
            op = parts[0] if parts else str(key)
            glob = parts[1] if len(parts) > 1 else "*"
            rules.append({
                "op": op,
                "glob": glob,
                "rule": "allow" if approved else "deny",
                "note": None,
            })
        return rules
    except Exception:
        return []


# ── route ─────────────────────────────────────────────────────────────────────


@router.get("/web/data")
async def web_data(
    project_root: Path = Depends(get_project_root),
) -> dict:
    """Return ReynUiData snapshot for window.OPENUI_DATA."""
    # Registry: best-effort — fall back to empty if not ready
    try:
        registry = get_registry()
        agents = _build_agents(registry)
    except Exception:
        agents = []

    library = _build_library(project_root)
    permissions = _build_permissions(project_root)

    return {
        "AGENTS": agents,
        "RECAP": [],
        "QUICKSTARTS": _QUICKSTARTS,
        "LIBRARY": library,
        "CONVO_ARIA": [],
        "CONVO_ARIA_STUDIO": [],
        "SKILL_GRAPH": {
            "name": "",
            "source": "",
            "entry": "",
            "finish_criteria": "",
            "phases": [],
        },
        "SKILL_MD": "",
        "RUN_EVENTS": [],
        "RUNS_LIST": [],
        "PERMISSIONS": permissions,
        "COPY": {"en": _COPY_EN, "ja": _COPY_JA},
    }
