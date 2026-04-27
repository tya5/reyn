"""
RichLogger — event subscriber that renders OS events using the Rich library.

Drop-in replacement for ConsoleLogger with styled output.
"""
from __future__ import annotations
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.rule import Rule
from .models import Event

_console = Console()


class RichLogger:
    """Callable subscriber that renders events with Rich styling."""

    def __init__(self, conversation: bool = False) -> None:
        self.conversation = conversation

    def __call__(self, event: Event) -> None:
        handler = getattr(self, f"on_{event.type}", None)
        if handler:
            handler(event.data)

    # ── Workflow ───────────────────────────────────────────────────────────────

    def on_workflow_started(self, data: dict) -> None:
        _console.print(Rule(f"[bold cyan]{data.get('app', '?')}[/bold cyan]", style="cyan"))

    def on_workflow_finished(self, data: dict) -> None:
        total = data.get("total_phase_count", "?")
        _console.print(Rule(f"[bold green]finished[/bold green]  ({total} phase steps)", style="green"))

    def on_workflow_terminated(self, data: dict) -> None:
        _console.print(Panel(
            data.get("reason", ""),
            title="[bold yellow]loop limit reached[/bold yellow]",
            border_style="yellow",
        ))

    def on_workflow_aborted(self, data: dict) -> None:
        _console.print(Panel(
            data.get("reason", ""),
            title="[bold red]workflow aborted[/bold red]",
            border_style="red",
        ))

    # ── Phase lifecycle ────────────────────────────────────────────────────────

    def on_phase_started(self, data: dict) -> None:
        phase = data["phase"]
        visit = data.get("visit_count", 1)
        visit_str = f"  [dim]visit #{visit}[/dim]" if visit > 1 else ""
        _console.print(f"\n[bold blue]▶ {phase}[/bold blue]{visit_str}")

    # ── Shell ─────────────────────────────────────────────────────────────────

    def on_shell_started(self, data: dict) -> None:
        cmd = data.get("cmd", "")
        timeout = data.get("timeout", 120)
        _console.print(f"  [dim]$ {cmd[:120]}[/dim]  [dim](timeout={timeout}s)[/dim]")

    def on_shell_completed(self, data: dict) -> None:
        rc = data.get("returncode", "?")
        stdout_len = data.get("stdout_len", 0)
        stderr_len = data.get("stderr_len", 0)
        style = "green" if rc == 0 else "red"
        _console.print(
            f"  [dim]shell[/dim] [{style}][rc={rc}][/{style}]"
            f"  [dim]stdout={stdout_len}chars  stderr={stderr_len}chars[/dim]"
        )

    def on_shell_timeout(self, data: dict) -> None:
        _console.print(f"  [bold red]shell TIMEOUT[/bold red] after {data.get('timeout', '?')}s")

    # ── LLM ───────────────────────────────────────────────────────────────────

    def on_llm_called(self, data: dict) -> None:
        model = data.get("model", "?")
        _console.print(f"  [dim]calling LLM ({model})…[/dim]")

    def on_context_built(self, data: dict) -> None:
        if not self.conversation:
            return
        import json
        from rich.syntax import Syntax
        from rich.columns import Columns
        from rich.table import Table

        frame = data.get("frame", {})
        phase = frame.get("current_phase", data.get("phase", "?"))
        role = frame.get("current_phase_role") or ""
        execution = frame.get("execution", {})
        visit = execution.get("current_visit", 1)
        total = execution.get("total_steps", 0)
        path = execution.get("path", [])

        body = Text()

        # execution context
        body.append(f"visit={visit}  total_steps={total}", style="dim")
        if role:
            body.append(f"  role=", style="dim")
            body.append(role, style="dim italic")
        body.append("\n")
        if path:
            body.append("path: ", style="bold")
            body.append(" → ".join(path) + "\n", style="dim")

        # instructions
        instructions = frame.get("instructions", "")
        if instructions:
            body.append("\ninstructions\n", style="bold underline")
            body.append(instructions + "\n")

        # input_artifact
        artifact = frame.get("input_artifact", {})
        if artifact:
            body.append("\ninput_artifact\n", style="bold underline")
            _console.print(Panel(
                body,
                title=f"[bold cyan]LLM INPUT[/bold cyan]  [dim]{phase}[/dim]",
                border_style="cyan",
                padding=(0, 1),
            ))
            _console.print(Panel(
                Syntax(json.dumps(artifact, ensure_ascii=False, indent=2),
                       "json", theme="monokai", word_wrap=True),
                title="[dim]input_artifact[/dim]",
                border_style="cyan",
                padding=(0, 1),
            ))
            body = Text()
        else:
            _console.print(Panel(
                body,
                title=f"[bold cyan]LLM INPUT[/bold cyan]  [dim]{phase}[/dim]",
                border_style="cyan",
                padding=(0, 1),
            ))
            body = Text()

        # candidates
        candidates = frame.get("candidate_outputs", [])
        if candidates:
            table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
            table.add_column("next_phase", style="cyan")
            table.add_column("schema")
            table.add_column("description", style="dim")
            for c in candidates:
                table.add_row(
                    c.get("next_phase", "?"),
                    c.get("schema_name", "?"),
                    c.get("description", ""),
                )
            _console.print(Panel(table, title="[dim]candidates[/dim]",
                                 border_style="cyan", padding=(0, 1)))

        # finish_criteria
        finish_criteria = frame.get("finish_criteria", [])
        if finish_criteria:
            fc_text = Text()
            for fc in finish_criteria:
                fc_text.append(f"• {fc}\n", style="dim")
            _console.print(Panel(fc_text, title="[dim]finish_criteria[/dim]",
                                 border_style="cyan", padding=(0, 1)))

        # control_ir_results (act re-call)
        ir_results = frame.get("control_ir_results", [])
        if ir_results:
            _console.print(Panel(
                Syntax(json.dumps(ir_results, ensure_ascii=False, indent=2),
                       "json", theme="monokai", word_wrap=True),
                title="[bold yellow]control_ir_results[/bold yellow]  [dim](act re-call)[/dim]",
                border_style="yellow",
                padding=(0, 1),
            ))

    def on_llm_response_received(self, data: dict) -> None:
        if not self.conversation:
            return
        import json
        from rich.syntax import Syntax
        phase = data.get("phase", "?")
        raw = data.get("raw", {})
        body = Syntax(
            json.dumps(raw, ensure_ascii=False, indent=2),
            "json",
            theme="monokai",
            word_wrap=True,
        )
        _console.print(Panel(
            body,
            title=f"[bold green]LLM OUTPUT[/bold green]  [dim]{phase}[/dim]  [yellow]{data.get('response_type', '?')}[/yellow]",
            border_style="green",
        ))

    def on_phase_retry(self, data: dict) -> None:
        attempt = data.get("attempt", "?")
        max_r = data.get("max_retries", "?")
        error = (data.get("error") or "")[:120]
        _console.print(
            f"  [yellow]⟳ retry {attempt}/{max_r}[/yellow]  [dim]{error}[/dim]"
        )

    def on_phase_completed(self, data: dict) -> None:
        phase = data["phase"]
        next_phase = data.get("next", "?")
        confidence = data.get("confidence", 0.0)
        retries = data.get("retries", 0)
        artifact_path = data.get("artifact_path", "")

        arrow = "[bold green]→[/bold green]" if next_phase != "end" else "[bold green]✓ end[/bold green]"
        next_str = f" [cyan]{next_phase}[/cyan]" if next_phase != "end" else ""
        retry_str = f"  [yellow][{retries} retr{'y' if retries == 1 else 'ies'}][/yellow]" if retries else ""
        conf_str = f"  [dim]confidence={confidence:.2f}[/dim]"
        path_str = f"  [dim]→ {artifact_path}[/dim]" if artifact_path else ""

        _console.print(f"  {arrow}{next_str}{retry_str}{conf_str}{path_str}")

    def on_artifact_created(self, data: dict) -> None:
        artifact_type = data.get("artifact_type", "?")
        path = data.get("path", "")
        keys = data.get("keys", [])
        keys_str = f"  [dim]{keys}[/dim]" if keys else ""
        _console.print(f"  [dim]artifact[/dim] [green]{artifact_type}[/green]{keys_str}  [dim]→ {path}[/dim]")

    # ── Act turn ──────────────────────────────────────────────────────────────

    def on_act_executed(self, data: dict) -> None:
        turn = data.get("act_turn", "?")
        ops = data.get("ops", [])
        results = data.get("results", [])

        lines = Text()
        for op, result in zip(ops, results):
            kind = op.get("kind", "?")
            status = result.get("status", "?")
            status_style = "green" if status == "ok" else "red"

            if kind == "file":
                file_op = op.get("op", "?")
                path = op.get("path", "?")
                if file_op == "read":
                    content_len = len(result.get("content", ""))
                    lines.append(f"  {file_op} ", style="dim")
                    lines.append(path, style="cyan")
                    lines.append(f"  [{status}]", style=status_style)
                    lines.append(f" ({content_len} chars)\n", style="dim")
                elif file_op == "glob":
                    count = result.get("count", 0)
                    lines.append(f"  glob ", style="dim")
                    lines.append(path, style="cyan")
                    lines.append(f"  [{status}]", style=status_style)
                    lines.append(f" ({count} matches)\n", style="dim")
                else:
                    lines.append(f"  {file_op} ", style="dim")
                    lines.append(path, style="cyan")
                    lines.append(f"  [{status}]\n", style=status_style)
            elif kind == "ask_user":
                answer = (result.get("answer") or "")
                lines.append("  ask_user", style="dim")
                lines.append(f"  [{status}]", style=status_style)
                lines.append(f"  → {answer!r}\n", style="yellow")
            else:
                lines.append(f"  {kind}  [{status}]\n", style="dim")

        _console.print(Panel(
            lines,
            title=f"[bold]act turn #{turn}[/bold]",
            border_style="blue",
            padding=(0, 1),
        ))

    # ── User intervention ──────────────────────────────────────────────────────

    def on_user_intervention_requested(self, data: dict) -> None:
        question = data.get("question", "")
        suggestions = data.get("suggestions") or []

        body = Text(question + "\n")
        if suggestions:
            body.append("\n  " + "  /  ".join(suggestions), style="dim cyan")

        _console.print(Panel(
            body,
            title="[bold yellow]ask_user[/bold yellow]",
            border_style="yellow",
        ))
