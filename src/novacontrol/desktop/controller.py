"""Desktop automation controller."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Protocol, runtime_checkable

from novacontrol.core.audit import AutomationAuditLog, InMemoryAutomationAuditLog
from novacontrol.core.security import (
    ApprovalGateway,
    ApprovalRequest,
    DenyByDefaultApprovalGateway,
    PermissionScope,
    RiskLevel,
)
from novacontrol.desktop.models import (
    DesktopAction,
    DesktopActionResult,
    DesktopActionStatus,
    DesktopActionType,
    DesktopWorkflow,
)


@dataclass(frozen=True, slots=True)
class DesktopStep:
    """One parsed step of a JARVIS command.

    Owns both its human label and how it is planned, so a new step kind is a
    single addition here plus one parser branch in parse_desktop_command — both
    in this module — instead of a parallel edit in application.py.
    """

    kind: str
    target: str = ""
    text: str = ""

    def describe(self) -> str:
        if self.kind == "open":
            return f"Open {self.target}"
        if self.kind == "open_folder":
            return f"Open folder {self.target}"
        if self.kind == "type":
            return f"Type '{self.text}'"
        if self.kind == "search":
            return f"Search the web for '{self.text}'"
        if self.kind == "press":
            return f"Press {self.text}"
        if self.kind == "click":
            return f"Click '{self.text}' (vision-guided)"
        if self.kind == "screenshot":
            return "Take a screenshot"
        if self.kind == "game_launch":
            return f"Launch {self.target} in Steam"
        if self.kind == "navigate":
            return f"Go to {self.text} in {self.target}"
        if self.kind == "stop":
            return f"Stop {self.target}" if self.target else "Stop the last app"
        if self.kind == "execute":
            return f"Execute: {self.target}"
        return f"Run {self.kind}"

    def plan(self, controller: "DesktopAutomationController") -> DesktopWorkflow:
        """Build the workflow fragment for this step through the controller seam."""
        if self.kind == "open":
            return controller.plan_open_application(self.target)
        if self.kind == "open_folder":
            return controller.plan_open_folder(self.target)
        if self.kind == "type":
            return controller.plan_type_text(self.text)
        if self.kind == "search":
            return controller.plan_web_search(self.text)
        if self.kind == "press":
            return controller.plan_keyboard_shortcut(self.text)
        if self.kind == "click":
            return controller.plan_vision_click(self.text)
        if self.kind == "screenshot":
            return controller.plan_screenshot()
        if self.kind == "game_launch":
            return controller.plan_game_launch(self.target)
        if self.kind == "navigate":
            return controller.plan_app_navigate(self.target, self.text)
        if self.kind == "stop":
            return controller.plan_stop(self.target)
        if self.kind == "execute":
            return controller.plan_execute_script(self.target)
        raise ValueError(f"Unknown desktop step kind: {self.kind}")


def parse_desktop_command(command: str) -> list[DesktopStep]:
    """Parse a natural language JARVIS command into typed steps.

    Returns DesktopStep objects; every step knows how to describe and plan
    itself, so planning a parsed command is one loop with no kind dispatch in
    the caller.

    Examples:
        "open notepad" -> [open notepad]
        "open notepad and type hello world" -> [open notepad, type 'hello world']
        "open folder C:\\Users" -> [open folder C:\\Users]
        "open chrome and search for cats" -> [open chrome, search 'cats']
        "open steam and go to library" -> [open steam, navigate steam→library]
        "open steam and go library and launch gta v" -> [open steam, navigate, click 'play/launch gta v']
        "stop that" -> [stop '']
    """
    lower = command.lower().strip()
    steps: list[DesktopStep] = []

    # 'stop that' / 'stop' / 'close it' — cancel the last launched app. Handled
    # before any splitting so it survives chains like '... and then stop'.
    if re.fullmatch(r"(?:stop|cancel)(?:\s+(?:that|it|this|the app|everything))?[.!]?,?", lower) or re.fullmatch(
        r"(?:stop|close|kill)(?:\s+that)?", lower
    ):
        return [DesktopStep(kind="stop")]

    # Split on every ' and ' into sequential steps. A fragment that does not
    # start with a step verb (e.g. the 'dogs' in 'search for cats and dogs')
    # is re-merged into the previous fragment, so payloads containing 'and'
    # survive while real chains ('type hello world and press enter') split.
    fragments = re.split(r"\s+and\s+", lower)
    merged: list[str] = []
    verb_prefix = re.compile(
        r"^(?:type|write|enter|input|put|press|hit|click|tap|search|google|look up|find|browse|open|launch|start|run|go|stop|switch|take|capture|screenshot)\b"
    )
    for fragment in fragments:
        if merged and not verb_prefix.match(fragment.strip()):
            merged[-1] = f"{merged[-1]} and {fragment}"
        else:
            merged.append(fragment.strip())
    main_part = merged[0] if merged else lower
    rest_parts = merged[1:]

    # --- Main action ---
    folder_match = re.match(r"(?:open|show|go to|explore)\s+(?:folder|directory|dir|file explorer)\s+(.+)", main_part)
    if not folder_match:
        folder_match = re.match(r"(?:open|show|go to|explore)\s+([a-z]:\\\\.+|~/.+|/home/.+|/Users/.+)", main_part)
    known_folder = (
        None
        if folder_match
        else re.match(
            r"(?:open|show|go to)\s+(?:my\s+)?(downloads|documents|pictures|music|videos|desktop|drive c|c drive)\b(?:\s+folder)?",
            main_part,
        )
    )
    # 'open the games folder' / 'open my projects' — a loose spoken folder name
    # is NOT an app name: it is searched for on disk at execution time, so the
    # parser must not swallow it into the app branch. Excluded app-ish shapes:
    # known apps/URLs (the resolver is authoritative for those) and words that
    # are actually apps' UI sections (library/store are in-app navigation).
    loose_folder = None
    if not folder_match and not known_folder:
        loose_match = re.match(r"(?:open|show|go to)\s+(?:the\s+|my\s+)?(.+?)\s*(?:folder|directory)\b", main_part)
        if loose_match:
            loose_folder = loose_match.group(1).strip()
        else:
            # A project is named in prose, not by extension, and opening one
            # means opening its FOLDER: "open my NovaControl project". Without
            # this the app branch below reads the whole phrase as a program
            # called "my novacontrol project" and tries to launch it. The
            # lookahead keeps the bare "open my project" out (nothing named
            # there) so it stays a reference rather than a folder called "my".
            project_match = re.match(
                r"(?:open|show|go to|launch)\s+(?:(?:my|our|the)\s+)?"
                r"(?!(?:my|our|the|a|an|new|that|this|those|these|it)\s+project\b)"
                r"(.+?)\s+project\b",
                main_part,
            )
            if project_match:
                loose_folder = project_match.group(1).strip()
    if folder_match:
        steps.append(DesktopStep(kind="open_folder", target=folder_match.group(1).strip()))
    elif known_folder:
        # Spoken user folders resolve to real shell folders, not app names.
        name = known_folder.group(1).lower()
        special = {"drive c": "C:\\", "c drive": "C:\\"}
        steps.append(DesktopStep(kind="open_folder", target=special.get(name, f"%USERPROFILE%\\{name.capitalize()}")))
    elif loose_folder:
        steps.append(DesktopStep(kind="open_folder", target=loose_folder))
    else:
        # Standalone screenshot: 'take a screenshot', 'capture my screen',
        # bare 'screenshot'.
        if re.search(
            r"(?:\bscreenshot\b|\b(?:take|capture|grab|snip)\b.*\bscreen\b|\bscreen(?:shot|capture|cap)\b)",
            main_part,
        ):
            steps.append(DesktopStep(kind="screenshot"))
        else:
            app_match = re.match(r"(?:open|launch|start|run)\s+(.+)", main_part)
            if app_match:
                target = app_match.group(1).strip()
                target = re.sub(r"\s*(please|for me|on my computer|on my laptop|on the computer)\s*$", "", target)
                if target:
                    steps.append(DesktopStep(kind="open", target=target))
            elif re.match(r"click\s+(?:on\s+)?(.+)", main_part):
                # Standalone vision-guided click ("click file", "click the submit button").
                click_match = re.match(r"click\s+(?:on\s+)?(.+)", main_part)
                if click_match:
                    steps.append(DesktopStep(kind="click", text=click_match.group(1).strip()))
            elif re.search(r"\b(?:search|google|look up|find|browse)\b", main_part):
                # A standalone web search ("search for cats") goes through the browser
                # too — the old fall-through fed the whole sentence to execute_script.
                steps.append(DesktopStep(kind="search", text=re.sub(r"^.*?\b(?:search|google|look up|find|browse)\b(?:\s+for\s+)?", "", main_part).strip() or main_part))
            else:
                steps.append(DesktopStep(kind="execute", target=command))

    # --- Following actions, in spoken order ---
    last_app: str = ""
    for prior in steps:
        if prior.kind == "open":
            last_app = prior.target
    for second_part in rest_parts:
        press_match = re.match(r"(?:press|hit|click|tap)\s+(.+)", second_part)
        type_match = re.match(r"(?:type|write|enter|input|put)\s+[\"']?(.+?)[\"']?\s*$", second_part)
        # 'go to library' / 'go library' / 'open the store' inside the running app.
        nav_match = re.match(r"(?:go(?:\s+to)?|open|show|switch to|navigate to)\s+(?:the\s+|my\s+)?(library|store|downloads|settings|friends|home)\b", second_part)
        # 'take a screenshot' / 'capture the screen' as a chain step.
        shot_match = re.match(r"(?:take|capture|screenshot)\b.*\bscreenshot\b|^(?:capture|screenshot)\b", second_part)
        stop_match = re.match(r"(?:stop|close|kill)(?:\s+(?:that|it|this|everything))?", second_part)
        if shot_match:
            steps.append(DesktopStep(kind="screenshot"))
        elif stop_match:
            steps.append(DesktopStep(kind="stop", target=last_app))
        elif press_match:
            steps.append(DesktopStep(kind="press", text=press_match.group(1).strip()))
        elif type_match:
            steps.append(DesktopStep(kind="type", text=type_match.group(1).strip()))
        elif nav_match and last_app:
            steps.append(DesktopStep(kind="navigate", target=last_app, text=nav_match.group(1)))
        elif nav_match:
            # Named app navigation without a preceding open: plan it anyway.
            steps.append(DesktopStep(kind="navigate", target="steam", text=nav_match.group(1)))
        elif re.match(r"^(?:launch|play|start|run)\s+(.+)", second_part) and last_app:
            # 'launch gta v' after opening Steam: a KNOWN game launches directly
            # via steam://rungameid (Steam's own protocol — reliable, no
            # guessing a Play-button coordinate); an UNKNOWN title falls back
            # to a vision-guided click on its Play control.
            launch_match = re.match(r"^(?:launch|play|start|run)\s+(.+)", second_part)
            if launch_match:
                game = launch_match.group(1).strip()
                if last_app.strip().lower() in ("steam",) and _steam_game_id(game):
                    steps.append(DesktopStep(kind="game_launch", target=game))
                else:
                    steps.append(DesktopStep(kind="click", text=f"play {game}".strip()))
        else:
            search_match = re.match(r"(?:search|google|look up|find|browse)\s+(?:for\s+)?(.+)", second_part)
            if search_match:
                steps.append(DesktopStep(kind="search", text=search_match.group(1).strip()))
            else:
                steps.append(DesktopStep(kind="type", text=second_part))

    return steps or [DesktopStep(kind="execute", target=command)]

@runtime_checkable
class DesktopCommandRunner(Protocol):
    # Optional multimodal vision provider used by vision-guided actions; the
    # application hot-swaps it at runtime (set/clear vision model).
    vision_provider: object | None

    async def run(self, action: DesktopAction) -> Mapping[str, Any]:
        """Run an approved desktop action."""


class VerificationError(RuntimeError):
    """An action ran, but its effect on the desktop could not be confirmed.

    JARVIS must not report success for actions that never took effect (an app
    that failed to show a window, text that had no focused window to land in).
    """


def _window_stem(target: str) -> str:
    """Reduce an app target to a matchable name: 'notepad' or 'C:\\...\\notepad.exe' -> 'notepad'.

    PureWindowsPath (not Path) so Windows-style paths parse identically on
    every platform — verification code must not silently change meaning when
    the suite runs on CI's Linux runners.
    """
    value = target.strip().lower()
    if "\\" in value or "/" in value or value.endswith((".exe", ".lnk", ".app", ".bat", ".cmd")):
        value = PureWindowsPath(value).stem.lower()
    return value


def _window_launch_command(resolved: str) -> list[str]:
    r"""argv that launches a resolved target through the Windows shell.

    UWP/Store targets (an AppsFolder AUMID like shell:AppsFolder\App_abc!App) go
    through explorer — the
    documented launcher for AppsFolder paths; `start` treats shell: as a
    folder to open rather than the app to activate. Everything else (URI
    schemes like steam:/ms-settings:/mailto:, exes, paths) goes through
    `cmd /c start`, whose protocol-handler and file-association registries
    resolve the target. Pure function so the routing decision stays testable
    on platforms that cannot spawn these processes.
    """
    if resolved.startswith("shell:"):
        return ["explorer", resolved]
    return ["cmd", "/c", "start", "", resolved]


def _window_matches(process_name: str, title: str, stem: str) -> bool:
    """A window matches when its process name or title contains the target name."""
    name = (process_name or "").strip().lower()
    window = (title or "").strip().lower()
    return bool(stem) and (name == stem or stem in name or stem in window)


def _window_stems(target: str, resolved: str | None = None) -> list[str]:
    """Stems a launch may verify against: the spoken target, its resolved form,
    and — for http(s) URL launches — any common browser process.

    'Open the browser' resolves to a neutral URL and is launched through the
    shell's protocol handler, so the window that appears belongs to whichever
    browser is the user's default (chrome/msedge/...), never the URL itself.
    Only http(s) qualifies: steam://open/main must NOT match a Chrome window.
    """
    stems = [_window_stem(target)]
    is_http = resolved is not None and resolved.strip().lower().startswith(("http://", "https://"))
    if resolved and _norm_label(resolved) != _norm_label(target):
        resolved_stem = _window_stem(resolved)
        if resolved_stem and resolved_stem not in stems:
            stems.append(resolved_stem)
    if is_http:
        stems.extend(
            s
            for s in ("chrome", "msedge", "firefox", "brave", "opera", "vivaldi", "browser")
            if s not in stems
        )
    return stems


# ---------------------------------------------------------------------------
# Universal installed-app resolution: ANY installed program must be openable
# by its spoken name, not just the handful in APP_ALIASES. Three fast sources
# (Start Menu .lnk, registry App Paths, PATH) feed a cached index; the UWP
# catalog (Get-StartApps) is consulted separately and only on a miss, in a
# worker thread, because that probe shells out to PowerShell.
# ---------------------------------------------------------------------------


def _norm_label(value: str) -> str:
    """Normalize a label for matching: collapse whitespace/underscores, casefold."""
    return re.sub(r"[\s_-]+", " ", value).strip().lower()


def _index_lookup(index: Mapping[str, str], spoken: str) -> str | None:
    """Look a spoken name up in an index: exact first, then whole-word match.

    The whole-word pass makes 'code' find 'Visual Studio Code' and 'chrome'
    find 'Google Chrome'. Ranking: entries ENDING in the spoken word beat
    prefix matches ('google chrome' beats 'chrome canary' for 'chrome' —
    people say the brand word last), then the shortest name wins so matches
    stay deterministic.
    """
    value = _norm_label(spoken)
    if not value:
        return None
    if value in index:
        return index[value]
    best: tuple[int, int, str] | None = None
    for name in index:
        if re.search(rf"\b{re.escape(value)}\b", _norm_label(name)):
            key = (0 if _norm_label(name).endswith(value) else 1, len(name), name)
            if best is None or key < best:
                best = key
    return index[best[2]] if best else None


def _build_app_index(start_menu_roots: list[Path]) -> dict[str, str]:
    """installed-app name (casefolded) -> launchable target.

    Sources, in priority order (first source wins per name):
      1. Start Menu shortcuts (*.lnk) — covers every installed desktop app.
      2. Registry 'App Paths' (HKLM + HKCU) — 'start <name>.exe' resolves
         through it even when the exe is not on PATH (chrome, firefox, ...).
    """
    index: dict[str, str] = {}
    for root in start_menu_roots:
        if not root.is_dir():
            continue
        try:
            for lnk in root.rglob("*.lnk"):
                index.setdefault(_norm_label(lnk.stem), str(lnk))
        except OSError:
            continue
    if sys.platform.startswith("win"):
        try:
            import winreg

            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(
                        hive, r"Software\Microsoft\Windows\CurrentVersion\App Paths"
                    ) as key:
                        position = 0
                        while True:
                            try:
                                subkey = winreg.EnumKey(key, position)
                            except OSError:
                                break
                            position += 1
                            if subkey.lower().endswith(".exe"):
                                # 'start <name>.exe' finds App Paths entries.
                                index.setdefault(_norm_label(subkey[:-4]), subkey)
                except OSError:
                    continue
        except ImportError:
            pass  # non-Windows: Start Menu scan already returned what it has
    return index


_app_index_cache: dict[str, str] | None = None


def _app_index(*, force: bool = False) -> dict[str, str]:
    """The machine's installed-app index, built once and cached."""
    global _app_index_cache
    if _app_index_cache is None or force:
        profile = Path(os.environ.get("USERPROFILE", str(Path.home())))
        roots = [
            Path(os.environ.get("APPDATA", str(profile / "AppData" / "Roaming")))
            / "Microsoft" / "Windows" / "Start Menu" / "Programs",
            Path(os.environ.get("PROGRAMDATA", "C:\\ProgramData"))
            / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        ]
        _app_index_cache = _build_app_index(roots)
    return _app_index_cache


_uwp_index_cache: dict[str, str] | None = None


def _uwp_index(*, force: bool = False) -> dict[str, str]:
    """UWP/Store app name -> AUMID launch target (built once, on first miss).

    Get-StartApps lists desktop entries too; only true AUMIDs (contain '!')
    are indexable here — desktop apps are already covered by _app_index.
    """
    global _uwp_index_cache
    if _uwp_index_cache is not None and not force:
        return _uwp_index_cache
    index: dict[str, str] = {}
    if sys.platform.startswith("win"):
        try:
            out = subprocess.run(
                [
                    "powershell", "-NoProfile", "-Command",
                    "Get-StartApps | ForEach-Object { $_.Name + '|' + $_.AppID }",
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            for line in out.stdout.splitlines():
                name, sep, appid = line.rpartition("|")
                appid = appid.strip()
                if sep and name.strip() and appid and "!" in appid and not re.search(r"\s", appid):
                    index[_norm_label(name)] = f"shell:AppsFolder\\{appid}"
        except (OSError, subprocess.SubprocessError):
            pass  # probe unavailable; UWP launch just stays unresolved
    _uwp_index_cache = index
    return index


def _uwp_lookup(spoken: str) -> str | None:
    return _index_lookup(_uwp_index(), spoken)


# Spoken folder name -> real location. Checked before any search; 'shell:'
# targets are launched by explorer directly.
_KNOWN_FOLDERS: dict[str, str] = {
    "documents": "%USERPROFILE%\\Documents",
    "downloads": "%USERPROFILE%\\Downloads",
    "pictures": "%USERPROFILE%\\Pictures",
    "music": "%USERPROFILE%\\Music",
    "videos": "%USERPROFILE%\\Videos",
    "desktop": "%USERPROFILE%\\Desktop",
    "recycle bin": "shell:RecycleBinFolder",
    "trash": "shell:RecycleBinFolder",
}


def _folder_search_roots() -> list[Path]:
    """Directories whose immediate children are searched for spoken folders:
    the user profile and its common locations, OneDrive when present, and the
    root of every mounted drive (games on the D drive, tools on E, ...)."""
    profile = Path(os.environ.get("USERPROFILE", str(Path.home())))
    roots = [profile, profile / "Desktop", profile / "Documents", profile / "Downloads"]
    onedrive = os.environ.get("OneDrive")
    if onedrive:
        base = Path(onedrive)
        roots.extend([base, base / "Desktop", base / "Documents"])
    for letter in "CDEFGH":
        drive = Path(f"{letter}:\\")
        if drive.is_dir():
            roots.append(drive)
    return roots


def _find_folder_by_name(name: str) -> Path | None:
    """Search the roots for a directory matching the spoken name.

    Exact (normalized) match first, then whole-word containment ('my games
    folder' finds 'SteamGames'); shortest name wins so matches are stable.
    """
    wanted = _norm_label(name)
    if not wanted:
        return None
    best: tuple[int, int, str, Path] | None = None
    for root in _folder_search_roots():
        try:
            children = [child for child in root.iterdir() if child.is_dir()]
        except OSError:
            continue
        for child in children:
            stem = _norm_label(child.name)
            if stem == wanted:
                score = 0
            elif re.search(rf"\b{re.escape(wanted)}\b", stem):
                score = 1
            elif (wanted.startswith(stem) or wanted.endswith(stem)) and len(stem) > 2:
                score = 2
            else:
                continue
            key = (score, len(stem), str(child).lower(), child)
            if best is None or key[:3] < best[:3]:
                best = key
    return best[3] if best else None


def _resolve_folder(value: str) -> str:
    """Resolve a spoken or typed folder to a real openable path.

    Real paths (and env templates) pass through expanded; known shell folders
    map directly; anything else is searched across the user profile and drive
    roots. When nothing matches, the input is returned honestly — explorer
    will report the missing path rather than the wrong folder opening.
    """
    text = value.strip().strip('"')
    if "%" in text:
        text = os.path.expandvars(text)
    lower = re.sub(r"^(?:the|my)\s+", "", text.lower())
    lower = re.sub(r"\s+folders?$", "", lower).strip()
    if lower in _KNOWN_FOLDERS:
        known = _KNOWN_FOLDERS[lower]
        return os.path.expandvars(known) if "%" in known else known
    path = Path(text).expanduser()
    if path.exists():
        return str(path)
    if path.anchor:  # an absolute path that does not exist: keep it honest
        return str(path)
    match = _find_folder_by_name(lower)
    return str(match) if match else str(path.resolve())


class NoopDesktopRunner:
    """Safe runner that records intent without changing the desktop."""

    def __init__(self, *, command_timeout_seconds: float = 60, vision_provider: object | None = None) -> None:
        # Same constructor surface as LocalDesktopRunner so the application can
        # wire the vision provider identically for both (the noop ignores it).
        self.command_timeout_seconds = command_timeout_seconds
        self.vision_provider = vision_provider

    async def run(self, action: DesktopAction) -> Mapping[str, Any]:
        return {"would_run": action.to_dict()}


class LocalDesktopRunner:
    """Runs approved desktop actions on the local machine."""

    def __init__(
        self,
        *,
        command_timeout_seconds: float = 60,
        vision_provider: object | None = None,
        windows_type_paste: bool | None = None,
    ) -> None:
        self.command_timeout_seconds = command_timeout_seconds
        # Multimodal LLM for vision-guided element location (None = OCR/landmarks only).
        self.vision_provider = vision_provider
        # Whether typed text goes through the Windows clipboard-paste path.
        # Defaults to the real platform; an override lets the verification
        # logic (staging + focus checks) be tested on any OS without spawning
        # PowerShell. Keep the default None distinct from False so tests can
        # also pin "the default really follows the platform".
        self._windows_type_paste = windows_type_paste

    def _uses_windows_type_paste(self) -> bool:
        if self._windows_type_paste is not None:
            return self._windows_type_paste
        return sys.platform.startswith("win")

    async def run(self, action: DesktopAction) -> Mapping[str, Any]:
        if action.type is DesktopActionType.OPEN_APPLICATION:
            return await self._open_application(action.target)
        if action.type is DesktopActionType.APP_NAVIGATE:
            return await self._app_navigate(
                str(action.parameters.get("app", action.target)), action.target
            )
        if action.type is DesktopActionType.VISION_CLICK:
            return await self._vision_click(
                action.target,
                screenshot_path=_optional_str(action.parameters.get("screenshot_path")),
            )
        if action.type is DesktopActionType.GAME_LAUNCH:
            return await self._game_launch(action.target, str(action.parameters.get("app", "steam")))
        if action.type is DesktopActionType.STOP_APP:
            return await self._stop_app(action.target)
        if action.type is DesktopActionType.CLOSE_APPLICATION:
            return await self._close_application(action.target)
        if action.type is DesktopActionType.EXECUTE_SCRIPT:
            return await self._execute_script(
                action.target,
                working_directory=_optional_str(action.parameters.get("working_directory")),
            )
        if action.type is DesktopActionType.ORGANIZE_FILES:
            return self._organize_files(
                action.target,
                extension=str(action.parameters["extension"]),
                destination=str(action.parameters["destination"]),
            )
        if action.type is DesktopActionType.TAKE_SCREENSHOT:
            return await self._take_screenshot(
                save_path=_optional_str(action.parameters.get("save_path")) or "screenshot.png",
            )
        if action.type is DesktopActionType.LIST_PROCESSES:
            return await self._list_processes(
                filter_name=_optional_str(action.parameters.get("filter_name")),
            )
        if action.type is DesktopActionType.LIST_FILES:
            return self._list_files(
                action.target,
                recursive=bool(action.parameters.get("recursive", False)),
                pattern=_optional_str(action.parameters.get("pattern")),
            )
        if action.type is DesktopActionType.READ_FILE:
            return self._read_file(action.target)
        if action.type is DesktopActionType.WRITE_FILE:
            return self._write_file(
                action.target,
                content=str(action.parameters.get("content", "")),
            )
        if action.type is DesktopActionType.SYSTEM_INFO:
            return await self._system_info()
        if action.type is DesktopActionType.KEYBOARD_SHORTCUT:
            return await self._keyboard_shortcut(action.target)
        if action.type is DesktopActionType.TYPE_TEXT:
            return await self._type_text(
                action.target,
                delay_ms=int(action.parameters.get("delay_ms", 30)),
            )
        if action.type is DesktopActionType.OPEN_FOLDER:
            return await self._open_folder(action.target)
        if action.type is DesktopActionType.SEARCH_START_MENU:
            return await self._search_start_menu(action.target)
        if action.type is DesktopActionType.WEB_SEARCH:
            return await self._web_search(
                action.target,
                browser=_optional_str(action.parameters.get("browser")) or None,
            )
        raise ValueError(f"Unsupported desktop action type: {action.type}")

    async def _open_application(self, target: str) -> Mapping[str, Any]:
        resolved = self._resolve_app_target(target)
        pid = await self._launch_open(target)
        verified = await self._wait_for_window(target, resolved=resolved)
        output: dict[str, Any] = {"adapter": "local-desktop", "pid": pid, "target": target, "resolved": resolved}
        if verified is None:
            # Probe unavailable (non-Windows or probe failed): don't fail a launch we
            # cannot check, but never claim it was verified.
            output["verified"] = None
            output["verification"] = "window check unavailable on this platform"
        elif verified:
            output["verified"] = True
            output["window"] = verified["window"]
            output["verification"] = f"Window appeared: {verified['window']}"
        else:
            raise VerificationError(
                f"Launched '{target}' but no window appeared within 8 seconds — "
                "the app may not have launched."
            )
        return output

    # Map common spoken app names to the exact command/URI Windows can launch.
    # 'cmd /c start' handles plain exe names, but Steam, Spotify, and the Mail
    # app are NOT on PATH — steam: and spotify: are registered URI schemes and
    # 'start' opens them through the shell's protocol handlers.
    APP_ALIASES: dict[str, str] = {
        "steam": "steam://open/main",
        "spotify": "spotify:",
        "mail": "mailto:",
        "outlook": "outlookmail:",
        "calculator": "calc.exe",
        "notepad": "notepad.exe",
        "paint": "mspaint.exe",
        "explorer": "explorer.exe",
        "file explorer": "explorer.exe",
        "files": "explorer.exe",
        "folder": "explorer.exe",
        "my computer": "explorer.exe",
        "this pc": "explorer.exe",
        "settings": "ms-settings:",
        "task manager": "taskmgr.exe",
        "word": "winword.exe",
        "excel": "excel.exe",
        "powerpoint": "powerpnt.exe",
    }

    @staticmethod
    def _resolve_app_target(target: str) -> str:
        """Resolve a spoken app name to a launchable Windows target.

        Resolution order: paths/explicit file names pass through; the curated
        alias table (URI schemes like steam:, spotify:); the installed-app
        index (Start Menu shortcuts + registry App Paths — ANY installed app,
        not just the aliases); the UWP/Store catalog; finally the raw name,
        which `start` still resolves through file association.
        """
        value = target.strip().lower()
        if "\\" in value or "/" in value or "." in value:
            return target  # a path or explicit file/exe name — launch as-is
        # Strip leading articles so "the browser" resolves like "browser";
        # paths were already returned above so they are never mangled. Only
        # true articles: "my computer" / "my files" are real target names.
        value = re.sub(r"^(the|a|an)\s+", "", value).strip()
        if value in LocalDesktopRunner.APP_ALIASES:
            return LocalDesktopRunner.APP_ALIASES[value]
        if value in ("browser", "web browser", "default browser"):
            # No single "browser app": the default browser is whatever the OS
            # has registered for http:. Launching a neutral URL through the
            # protocol handler opens it on every platform and every browser.
            return "http://localhost"
        indexed = _index_lookup(_app_index(), value)
        if indexed:
            return indexed
        uwp = _uwp_lookup(value)
        if uwp:
            return uwp
        return target

    async def _launch_open(self, target: str) -> int:
        resolved = self._resolve_app_target(target)
        if sys.platform.startswith("win"):
            process = subprocess.Popen(
                _window_launch_command(resolved),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif sys.platform == "darwin":
            process = subprocess.Popen(
                ["open", resolved],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            process = subprocess.Popen(
                ["xdg-open", resolved],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return process.pid

    async def _powershell(self, script: str, *, timeout: float = 10.0) -> str:
        process = await asyncio.create_subprocess_exec(
            "powershell", "-NoProfile", "-Command", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
        return stdout.decode("utf-8", errors="replace")

    async def _window_lines(self) -> list[str] | None:
        """Visible top-level windows as 'processname|title' lines; None when the
        platform probe is unavailable so callers skip rather than fail."""
        if not sys.platform.startswith("win"):
            return None
        try:
            out = await self._powershell(
                "Get-Process | Where-Object { $_.MainWindowHandle -ne 0 } "
                "| ForEach-Object { $_.ProcessName + '|' + $_.MainWindowTitle }",
                timeout=5,
            )
            return [line for line in out.splitlines() if "|" in line]
        except Exception:
            return None

    async def _wait_for_window(
        self, target: str, *, resolved: str | None = None, timeout_seconds: float = 8.0
    ) -> dict[str, Any] | None:
        """Poll for a visible window matching the target (or its resolved form).

        Returns a dict with the matched title when found, an empty dict when the probe ran
        but nothing appeared, and None when the probe is unavailable.
        """
        stems = _window_stems(target, resolved)
        deadline = time.monotonic() + timeout_seconds
        while True:
            lines = await self._window_lines()
            if lines is None:
                return None
            for line in lines:
                name, sep, title = line.partition("|")
                if any(_window_matches(name, title if sep else "", stem) for stem in stems):
                    return {"window": (title if sep else name).strip()}
            if time.monotonic() >= deadline:
                return {}
            await asyncio.sleep(0.5)

    async def _execute_script(
        self,
        command: str,
        *,
        working_directory: str | None,
    ) -> Mapping[str, Any]:
        # NO-SHELL INVARIANT: commands run exec-form only — shlex.split turns the command into
        # literal argv tokens and create_subprocess_exec never interprets shell metacharacters.
        # Do not switch to shell=True / create_subprocess_shell / os.system here; the shell would
        # re-interpret those tokens. DesktopNoShellInvariantTests guards this contract.
        import shlex
        parts = shlex.split(command)
        process = await asyncio.create_subprocess_exec(
            *parts,
            cwd=working_directory,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self.command_timeout_seconds,
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise TimeoutError(f"Command timed out after {self.command_timeout_seconds:g}s") from exc

        return {
            "adapter": "local-desktop",
            "command": command,
            "working_directory": working_directory,
            "exit_code": process.returncode,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
        }

    def _organize_files(
        self,
        source_directory: str,
        *,
        extension: str,
        destination: str,
    ) -> Mapping[str, Any]:
        source = Path(source_directory).expanduser().resolve()
        destination_path = Path(destination).expanduser()
        target_directory = (
            destination_path.resolve()
            if destination_path.is_absolute()
            else (source / destination_path).resolve()
        )
        if not source.is_dir():
            raise FileNotFoundError(f"Source directory does not exist: {source}")
        if not str(target_directory).startswith(str(source)):
            raise ValueError(f"Destination escapes source directory: {destination}")

        target_directory.mkdir(parents=True, exist_ok=True)
        normalized_extension = extension if extension.startswith(".") else f".{extension}"
        moved: list[str] = []
        for child in source.iterdir():
            if not child.is_file() or child.suffix.lower() != normalized_extension.lower():
                continue
            destination_file = target_directory / child.name
            shutil.move(str(child), str(destination_file))
            moved.append(str(destination_file))

        return {
            "adapter": "local-desktop",
            "source_directory": str(source),
            "destination": str(target_directory),
            "extension": normalized_extension,
            "moved": moved,
            "moved_count": len(moved),
        }

    async def _type_text(self, text: str, *, delay_ms: int = 30) -> Mapping[str, Any]:
        """Type text into the currently focused window, then verify it landed.

        On Windows the paste is staged through the clipboard and Ctrl+V; verification
        confirms the exact payload reached the clipboard AND a window was focused to
        receive it before reporting success. Other platforms keep the raw keystroke
        behavior (unverified).
        """
        if self._uses_windows_type_paste():
            await self._stage_and_paste_windows(text)
            verification = await self._verify_type_windows(text)
            output: dict[str, Any] = {
                "adapter": "local-desktop",
                "action": "type_text",
                "text": text,
                "char_count": len(text),
            }
            if verification is None:
                output["verified"] = None
                output["verification"] = "paste verification unavailable on this platform"
            elif verification.get("clipboard_match") and verification.get("focused_window"):
                output["verified"] = True
                output["verification"] = {
                    "clipboard_staged": True,
                    "focused_window": verification.get("window_title") or "(unlabeled window)",
                }
            else:
                reason = (
                    "the text never reached the clipboard (staging failed)"
                    if not verification.get("clipboard_match")
                    else "no window is focused — the paste had nowhere to land"
                )
                raise VerificationError(f"Typed text was not verified: {reason}.")
            return output
        if sys.platform == "darwin":
            process = await asyncio.create_subprocess_exec(
                "osascript", "-e", f'tell application "System Events" to keystroke "{text}"',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(process.communicate(), timeout=10)
        else:
            process = await asyncio.create_subprocess_exec(
                "xdotool", "type", "--delay", str(delay_ms), text,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(process.communicate(), timeout=10)
        return {
            "adapter": "local-desktop",
            "action": "type_text",
            "text": text,
            "char_count": len(text),
        }

    async def _stage_and_paste_windows(self, text: str) -> None:
        """Stage the exact text on the clipboard and paste it into the focused window."""
        quoted = "'" + text.replace("'", "''") + "'"
        script = (
            f"Set-Clipboard -Value {quoted}; "
            "Add-Type -AssemblyName System.Windows.Forms; "
            "[System.Windows.Forms.SendKeys]::SendWait('^v')"
        )
        await self._powershell(script, timeout=10)

    async def _verify_type_windows(self, text: str) -> dict[str, Any] | None:
        """Confirm the staged text reached the clipboard and a window was focused.

        Returns None when the probe itself fails, so an unverifiable paste is not
        wrongly reported as success or failure.
        """
        quoted = "'" + text.replace("'", "''") + "'"
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; "
            "using System.Text; public class FG { "
            "[DllImport(\"user32.dll\")] public static extern IntPtr GetForegroundWindow(); "
            "[DllImport(\"user32.dll\")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n); }'; "
            "$h = [FG]::GetForegroundWindow(); "
            "$sb = New-Object System.Text.StringBuilder 512; "
            "[void][FG]::GetWindowText($h, $sb, 512); "
            "$cb = [System.Windows.Forms.Clipboard]::GetText(); "
            f"Write-Output ('CBOK|' + ($cb -eq {quoted})); "
            "Write-Output ('FGW|' + ($h -ne 0)); "
            "Write-Output ('FGTTL|' + $sb.ToString())"
        )
        try:
            out = await self._powershell(script, timeout=10)
        except Exception:
            return None
        parsed: dict[str, Any] = {"clipboard_match": False, "focused_window": False, "window_title": ""}
        for line in out.splitlines():
            key, sep, value = line.partition("|")
            if key == "CBOK":
                parsed["clipboard_match"] = value.strip() == "True"
            elif key == "FGW":
                parsed["focused_window"] = value.strip() == "True"
            elif key == "FGTTL":
                parsed["window_title"] = value.strip()
        return parsed

    async def _open_folder(self, folder_path: str) -> Mapping[str, Any]:
        """Open a folder in the system file manager.

        The target goes through _resolve_folder first: spoken names ('games',
        'projects', known shell folders) resolve to real locations across the
        user profile and drive roots; real paths and env templates pass
        through expanded. A name that matches nothing is resolved as-is (the
        file manager will report it) but never silently created here.
        """
        target = _resolve_folder(folder_path)
        if sys.platform.startswith("win"):
            process = subprocess.Popen(
                ["explorer", target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif sys.platform == "darwin":
            process = subprocess.Popen(
                ["open", target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            process = subprocess.Popen(
                ["xdg-open", target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return {"adapter": "local-desktop", "action": "open_folder", "pid": process.pid, "path": target}

    async def _web_search(
        self,
        query: str,
        *,
        browser: str | None = None,
    ) -> Mapping[str, Any]:
        """Open a browser tab pointed at a search-results URL for the query.

        This is the whole fix for "open chrome and search for cats": instead of
        keystroking the query into the Windows Start Menu (where it searched the
        PC, not the web), the query is URL-encoded into a real search URL and
        launched like any other target — the browser opens a tab already showing
        the results. No keystroke timing races, no focus stealing.
        """
        from urllib.parse import quote_plus
        url = f"https://www.google.com/search?q={quote_plus(query)}"
        if sys.platform.startswith("win"):
            args = ["cmd", "/c", "start", ""]
            if browser:
                args.append(browser)  # "chrome" / "msedge" / "firefox"
            args.append(url)
            process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform == "darwin":
            args = ["open"]
            if browser:
                args.extend(["-a", browser])
            args.append(url)
            process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            process = subprocess.Popen(
                ["xdg-open", url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return {
            "adapter": "local-desktop",
            "action": "web_search",
            "query": query,
            "url": url,
            "browser": browser or "default",
            "pid": process.pid,
        }

    async def _search_start_menu(self, query: str) -> Mapping[str, Any]:
        """Open Start Menu and search for an application on Windows."""
        if sys.platform.startswith("win"):
            # Press Win key, wait, then type the query
            script = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "[System.Windows.Forms.SendKeys]::SendWait('^{ESC}'); "
                "Start-Sleep -Milliseconds 500; "
                f"[System.Windows.Forms.SendKeys]::SendWait('{query}')"
            )
            await self._powershell(script, timeout=10)
        elif sys.platform == "darwin":
            process = await asyncio.create_subprocess_exec(
                "open", "-a", "Spotlight",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(process.communicate(), timeout=5)
            process = await asyncio.create_subprocess_exec(
                "osascript", "-e",
                f'tell application "System Events" to keystroke "{query}"',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(process.communicate(), timeout=5)
        else:
            # Linux: use dmenu or rofi
            process = await asyncio.create_subprocess_exec(
                "bash", "-c", f"echo '{query}' | rofi -dmenu",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(process.communicate(), timeout=5)
        return {
            "adapter": "local-desktop",
            "action": "search_start_menu",
            "query": query,
        }

    async def _focus_window(self, target: str) -> Mapping[str, Any]:
        """Bring an application's window to the foreground before in-app steps.

        On Windows this uses the shell's AppActivate (matches by window title or
        process name stem — "steam" finds "Steam", "notepad" finds the Untitled
        Notepad); a leading app-launch step usually did the focusing already, and
        this re-anchors it when other windows appeared in between.
        """
        if not sys.platform.startswith("win"):
            return {"focused": False, "reason": "window focus unsupported on this platform"}
        stem = _window_stem(target)
        quoted = stem.replace("'", "''")
        script = (
            "Add-Type -AssemblyName Microsoft.VisualBasic;"
            "$found = $false;"
            "Get-Process | Where-Object { $_.MainWindowTitle } | ForEach-Object {"
            f"  if (-not $found -and $_.MainWindowTitle -like '*{quoted}*') {{"
            "    [Microsoft.VisualBasic.Interaction]::AppActivate($_.Id); $found = $true"
            "  }"
            "};"
            "if ($found) { 'FOCUSED' } else { 'NOT_FOUND' }"
        )
        try:
            out = await self._powershell(script, timeout=10)
        except Exception:
            out = ""
        focused = "FOCUSED" in out
        if not focused:
            # Fallback: try the spoken name itself (AppActivate also matches by
            # title substring; the stem may have been too short).
            try:
                alt = target.strip().replace("'", "''")
                await self._powershell(
                    "Add-Type -AssemblyName Microsoft.VisualBasic;"
                    f"[Microsoft.VisualBasic.Interaction]::AppActivate('{alt}')",
                    timeout=8,
                )
                focused = True
            except Exception:
                focused = False
        return {"focused": focused, "target": target}

    async def _app_navigate(self, app: str, destination: str) -> Mapping[str, Any]:
        """Navigate inside an app via its URI scheme (steam://open/library, ...).

        Keeps 'open steam and go to library' off the Start-Menu surface: the app
        is already running, and the deep link moves it to the right screen.
        """
        value = app.strip().lower()
        route = _APP_NAVIGATION_ROUTES.get(value)
        if route is None:
            raise ValueError(
                f"I don't know how to navigate inside '{app}'. "
                f"Known apps: {', '.join(sorted(_APP_NAVIGATION_ROUTES))}."
            )
        uri = route.format(destination=destination.strip().lower())
        if not sys.platform.startswith("win"):
            raise ValueError("In-app navigation via URI scheme requires Windows.")
        process = subprocess.Popen(
            ["cmd", "/c", "start", "", uri],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        await asyncio.sleep(1.5)
        return {
            "adapter": "local-desktop",
            "action": "app_navigate",
            "app": app,
            "destination": destination,
            "uri": uri,
            "pid": process.pid,
        }

    async def _game_launch(self, game: str, app: str = "steam") -> Mapping[str, Any]:
        """Launch a known game through its platform's deep link.

        'launch gta v' -> steam://rungameid/3240220 — Steam's own protocol,
        which starts or focuses the game regardless of which screen Steam is
        showing. Far more reliable than clicking a guessed Play-button spot.
        Recovery: when the deep link is rejected, fall back to a vision-guided
        click on the game's Play control (spec: retry an alternate method).
        """
        app_id = _steam_game_id(game) if app == "steam" else None
        if app_id is None:
            raise ValueError(
                f"I don't know '{game}'s game ID yet — falling back to a vision-guided click."
            )
        uri = f"steam://rungameid/{app_id}"
        if not sys.platform.startswith("win"):
            raise ValueError("Game deep-link launch requires Windows.")
        process = subprocess.Popen(
            ["cmd", "/c", "start", "", uri],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Big titles spawn slowly (GTA V: 30-60s+ through the Rockstar
        # launcher) and their WINDOW TITLES do not match the spoken name — and
        # launcher processes (PlayGTAV) can run with NO window at all. Verify
        # with a dual probe: window titles OR known game process names.
        running: dict[str, Any] | None = None
        process_names = _game_process_names(game)
        window_stems = _game_window_stems(game)
        deadline = time.monotonic() + 90.0
        while time.monotonic() < deadline:
            # Window probe first (strongest evidence).
            for stem in window_stems:
                running = await self._wait_for_window(stem, timeout_seconds=2.0)
                if running:
                    break
            if running:
                break
            # Process probe: PlayGTAV etc. run windowless while loading.
            probe = await self._powershell(
                "Get-Process | Where-Object {"
                + " -or ".join(f"$_.ProcessName -like '*{name}*'" for name in process_names)
                + "} | Select-Object -First 1 -ExpandProperty ProcessName",
                timeout=10,
            )
            if probe and str(probe).strip():
                running = {"window": f"process {str(probe).strip()}"}
                break
            await asyncio.sleep(2.0)
        result: dict[str, Any] = {
            "adapter": "local-desktop",
            "action": "game_launch",
            "game": game,
            "uri": uri,
            "pid": process.pid,
        }
        if running:
            result["verified"] = True
            result["window"] = running["window"]
            result["verification"] = f"Game is running: {running['window']}"
            return result
        # RECOVERY: deep link ran but nothing verifiable — try the
        # vision-guided click on the game's Play control.
        try:
            click_result = await self._vision_click(f"play {game}")
        except Exception as exc:
            raise VerificationError(
                f"Launched '{game}' via {uri} but no game process or window appeared within 90s, "
                f"and the vision-guided fallback failed ({exc})."
            ) from exc
        return {
            **result,
            "verified": click_result.get("located_by") is not None,
            "verification": "Deep link produced no window; vision-guided fallback used.",
            "fallback": click_result,
        }

    async def _vision_click(self, label: str, *, screenshot_path: str | None = None) -> Mapping[str, Any]:
        """Click a UI element found by the vision model (or text heuristics).

        Locate → move the cursor → click → verify via before/after screenshots.
        Fails honestly when the element cannot be located; never guesses a
        coordinate blindly.
        """
        from novacontrol.desktop.vision_guide import locate_element

        path = screenshot_path or "vision_locate.png"
        await self._take_screenshot(save_path=path)
        # The wired vision provider (if any) participates in location; without
        # one, locate_element still runs real Windows OCR + landmarks.
        location = await locate_element(
            path, label, llm_provider=getattr(self, "vision_provider", None)
        )
        if location is None:
            raise VerificationError(
                f"Vision could not locate '{label}' on screen — nothing was clicked."
            )
        x, y, how = location
        if not sys.platform.startswith("win"):
            raise ValueError("Vision-guided clicking currently requires Windows.")
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$sig = '[DllImport(\"user32.dll\")] public static extern bool SetProcessDPIAware();';"
            "$t = Add-Type -MemberDefinition $sig -Name Dpi -Namespace W -PassThru;"
            "$t::SetProcessDPIAware() | Out-Null;"
            f"[System.Windows.Forms.Cursor]::Position = New-Object System.Drawing.Point({x}, {y});"
            "Start-Sleep -Milliseconds 120;"
            "[System.Windows.Forms.UserControl]::MouseButtons;"  # no-op read keeps the assembly warm
            "[System.Windows.Forms.SendKeys]::Flush();"
            "$sig = '[DllImport(\"user32.dll\")] public static extern void mouse_event(uint f, uint x, uint y, uint d, int e);';"
            "$t = Add-Type -MemberDefinition $sig -Name ME -Namespace W -PassThru;"
            "$t::mouse_event(2, 0, 0, 0, 0); Start-Sleep -Milliseconds 40;"
            "$t::mouse_event(4, 0, 0, 0, 0)"
        )
        await self._powershell(script, timeout=10)
        await asyncio.sleep(1.0)
        after = "vision_after.png"
        await self._take_screenshot(save_path=after)
        # The verification pass (VisionController._verify_after_click) diffs
        # around this point — remember the last click site.
        self._last_click_point = (x, y)
        return {
            "adapter": "local-desktop",
            "action": "vision_click",
            "label": label,
            "coordinates": [x, y],
            "located_by": how,
            "before": path,
            "after": after,
            # Real key names (not positional) so the verification pass can
            # diff the two captures without re-capturing.
            "before_path": path,
            "after_path": after,
        }

    async def _stop_app(self, target: str) -> Mapping[str, Any]:
        """Stop the most recently launched/focused app gracefully (WM_CLOSE).

        Uses the same window-matching stem as verification; safer than
        process-kill (unsaved documents get their normal save prompts).
        """
        stem = _window_stem(target)
        if sys.platform.startswith("win"):
            quoted = stem.replace("'", "''")
            script = (
                "$target = Get-Process | Where-Object { $_.MainWindowTitle } | "
                f"Where-Object {{ $_.MainWindowTitle -like '*{quoted}*' }} | "
                "Sort-Object StartTime -Descending | Select-Object -First 1;"
                "if ($target) {"
                "  $sig = '[DllImport(\"user32.dll\")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);';"
                "  $t = Add-Type -MemberDefinition $sig -Name PM -Namespace W2 -PassThru;"
                "  $t::PostMessage($target.MainWindowHandle, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero) | Out-Null;"
                "  Write-Output ('STOPPED|' + $target.ProcessName)"
                "} else { Write-Output 'NOT_FOUND' }"
            )
            out = await self._powershell(script, timeout=10)
            if "STOPPED" not in out:
                raise VerificationError(
                    f"No window matching '{stem}' is running — nothing was stopped."
                )
            who = out.split("|", 1)[1].strip()
            await asyncio.sleep(1.0)
            return {"adapter": "local-desktop", "action": "stop_app", "process": who, "stopped": True}
        raise ValueError("Stop requires a window manager probe (Windows for now).")

    async def _close_application(self, target: str) -> Mapping[str, Any]:
        if sys.platform.startswith("win"):
            process = subprocess.Popen(
                ["taskkill", "/IM", target, "/F"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, stderr = process.communicate()
            return {
                "adapter": "local-desktop",
                "action": "close_application",
                "target": target,
                "exit_code": process.returncode,
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
            }
        elif sys.platform == "darwin":
            import shlex
            safe_target = shlex.quote(target)
            process = subprocess.Popen(
                ["osascript", "-e", f"quit app {safe_target}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, stderr = process.communicate()
            return {
                "adapter": "local-desktop",
                "action": "close_application",
                "target": target,
                "exit_code": process.returncode,
            }
        else:
            process = subprocess.Popen(
                ["pkill", "-f", target],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, stderr = process.communicate()
            return {
                "adapter": "local-desktop",
                "action": "close_application",
                "target": target,
                "exit_code": process.returncode,
            }

    async def _take_screenshot(self, save_path: str = "screenshot.png") -> Mapping[str, Any]:
        """Capture a screenshot using platform-native tools."""
        if sys.platform.startswith("win"):
            # Use PowerShell to take screenshot. The shell is DPI-unaware by
            # default, so Screen.PrimaryScreen.Bounds reports the virtualized
            # (logical) size — on a high-DPI display that is HALF the physical
            # resolution, and Windows OCR then reads far less text (a real bug
            # this caught: 66 words vs 171, missing small labels like "Vision").
            # SetProcessDPIAware() makes the bounds report PHYSICAL pixels so
            # the captured image carries the same detail the eye sees.
            script = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "Add-Type -AssemblyName System.Drawing; "
                "$sig = '[DllImport(\"user32.dll\")] public static extern bool SetProcessDPIAware();'; "
                "$t = Add-Type -MemberDefinition $sig -Name Dpi -Namespace W -PassThru; "
                "$t::SetProcessDPIAware() | Out-Null; "
                "$bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds; "
                "$bmp = [System.Drawing.Bitmap]::new($bounds.Width, $bounds.Height); "
                "$g = [System.Drawing.Graphics]::FromImage($bmp); "
                "$g.CopyFromScreen(0, 0, 0, 0, $bmp.Size); "
                f"$bmp.Save('{save_path}'); "
                "Write-Output 'screenshot_saved'"
            )
            await self._powershell(script, timeout=15)
        elif sys.platform == "darwin":
            process = await asyncio.create_subprocess_exec(
                "screencapture", "-x", save_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(process.communicate(), timeout=15)
        else:
            process = await asyncio.create_subprocess_exec(
                "scrot", save_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(process.communicate(), timeout=15)

        saved_path = Path(save_path).resolve()
        return {
            "adapter": "local-desktop",
            "action": "take_screenshot",
            "path": str(saved_path),
            "exists": saved_path.exists(),
            "size_bytes": saved_path.stat().st_size if saved_path.exists() else 0,
        }

    async def _list_processes(self, filter_name: str | None = None) -> Mapping[str, Any]:
        """List running processes."""
        if sys.platform.startswith("win"):
            cmd = ["tasklist", "/FO", "CSV"]
        else:
            cmd = ["ps", "aux"]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
        output = stdout.decode("utf-8", errors="replace")

        processes: list[dict[str, str]] = []
        for line in output.strip().splitlines():
            if filter_name and filter_name.lower() not in line.lower():
                continue
            processes.append({"line": line})

        return {
            "adapter": "local-desktop",
            "action": "list_processes",
            "count": len(processes),
            "processes": processes[:50],
        }

    def _list_files(
        self,
        directory: str,
        *,
        recursive: bool = False,
        pattern: str | None = None,
    ) -> Mapping[str, Any]:
        """List files in a directory."""
        path = Path(directory).expanduser().resolve()
        if not path.is_dir():
            raise FileNotFoundError(f"Directory not found: {path}")

        if recursive:
            files = list(path.rglob(pattern or "*"))
        else:
            files = list(path.glob(pattern or "*"))

        entries = []
        for f in sorted(files)[:200]:
            entries.append({
                "path": str(f),
                "name": f.name,
                "type": "directory" if f.is_dir() else "file",
                "size": f.stat().st_size if f.is_file() else 0,
            })

        return {
            "adapter": "local-desktop",
            "action": "list_files",
            "directory": str(path),
            "count": len(entries),
            "entries": entries,
        }

    def _read_file(self, file_path: str) -> Mapping[str, Any]:
        """Read a file's content."""
        path = Path(file_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {path}")

        content = path.read_text(encoding="utf-8", errors="replace")
        return {
            "adapter": "local-desktop",
            "action": "read_file",
            "path": str(path),
            "size_bytes": len(content.encode()),
            "content": content[:10000],  # Limit for safety
            "truncated": len(content) > 10000,
        }

    def _write_file(self, file_path: str, content: str) -> Mapping[str, Any]:
        """Write content to a file."""
        path = Path(file_path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {
            "adapter": "local-desktop",
            "action": "write_file",
            "path": str(path),
            "size_bytes": len(content.encode()),
        }

    async def _system_info(self) -> Mapping[str, Any]:
        """Get system information."""
        import platform
        info: dict[str, Any] = {
            "adapter": "local-desktop",
            "action": "system_info",
            "platform": platform.platform(),
            "python": platform.python_version(),
            "machine": platform.machine(),
            "processor": platform.processor(),
        }
        try:
            import psutil
            info["cpu_count"] = psutil.cpu_count()
            mem = psutil.virtual_memory()
            info["ram_total_gb"] = round(mem.total / (1024**3), 1)
            info["ram_used_percent"] = mem.percent
            disk = psutil.disk_usage("/")
            info["disk_total_gb"] = round(disk.total / (1024**3), 1)
            info["disk_used_percent"] = round(disk.percent, 1)
        except ImportError:
            info["note"] = "psutil not installed; limited system info available"
        return info

    async def _keyboard_shortcut(self, shortcut: str) -> Mapping[str, Any]:
        """Simulate a keyboard shortcut (e.g., 'ctrl+c')."""
        # Parse shortcut like 'ctrl+shift+s'
        keys = [k.strip().lower() for k in shortcut.split("+")]

        if sys.platform.startswith("win"):
            # Use PowerShell with SendKeys
            send_keys_map = {
                "ctrl": "^",
                "alt": "%",
                "shift": "+",
                "enter": "{ENTER}",
                "tab": "{TAB}",
                "esc": "{ESC}",
                "delete": "{DELETE}",
                "backspace": "{BACKSPACE}",
            }
            keys_str = "".join(send_keys_map.get(k, k) for k in keys)
            script = f"Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.SendKeys]::SendWait('{keys_str}')"
            await self._powershell(script, timeout=5)
        else:
            # Use xdotool on Linux
            keys_str = "+".join(keys)
            process = await asyncio.create_subprocess_exec(
                "xdotool", "key", keys_str,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(process.communicate(), timeout=5)

        return {
            "adapter": "local-desktop",
            "action": "keyboard_shortcut",
            "shortcut": shortcut,
            "keys": keys,
        }


# Spoken browser name -> the process name `cmd /c start` can target a specific
# browser for web searches: "open chrome and search for cats" then opens the
# results tab in Chrome itself instead of the default browser.
_BROWSER_LAUNCH_NAMES: dict[str, str] = {
    "chrome": "chrome",
    "google chrome": "chrome",
    "edge": "msedge",
    "microsoft edge": "msedge",
    "firefox": "firefox",
    "brave": "brave",
}


# In-app destinations reachable through the app's own URI scheme — the reliable
# way to "go to library" / "go to settings" in a running app without fragile
# keystrokes.
_APP_NAVIGATION_ROUTES: dict[str, str] = {
    "steam": "steam://open/{destination}",
    "spotify": "spotify://page/{destination}",
    "settings": "ms-settings:{destination}",
    "ms-settings": "ms-settings:{destination}",
    "discord": "discord://-/library/{destination}",
}

# Well-known Steam AppIDs for direct game launches. "launch gta v" resolves
# to steam://rungameid/<appid> — Steam's own protocol — instead of a blind
# vision click on a guessed Play-button coordinate.
_STEAM_APP_IDS: dict[str, str] = {
    "gta v": "3240220",          # Grand Theft Auto V Enhanced
    "grand theft auto v": "3240220",
    "gta 5": "3240220",
    "gta online": "3240220",
    "counter-strike 2": "730",
    "cs2": "730",
    "dota 2": "570",
    "team fortress 2": "440",
    "portal 2": "620",
    "left 4 dead 2": "550",
    "half-life alyx": "546560",
    "stardew valley": "413150",
    "terraria": "105600",
    "rocket league": "252950",
    "pubg": "578080",
    "rust": "252490",
    "ark": "346110",
    "fifa": "2195250",
    "ea fc 24": "2195250",
    "elden ring": "1245620",
    "cyberpunk 2077": "1091500",
    "the witcher 3": "292030",
    "red dead redemption 2": "1174180",
    "rdr2": "1174180",
    "baldurs gate 3": "1086940",
    "it takes two": "1426210",
    "among us": "945360",
    "fall guys": "1097150",
    "destiny 2": "1085660",
    "warframe": "230410",
    "path of exile": "238960",
    "apex legends": "1172470",
}


def _installed_steam_games() -> dict[str, str]:
    """Games actually installed on this machine: spoken name -> Steam AppID.

    Reads Steam's own appmanifest_*.acf files from the standard library
    locations. This is what makes 'launch <any installed game>' work without a
    hard-coded table: Steam records the exact appid and display name for every
    installed title. Cached once per process — installs don't change mid-run.
    Returns {} on any failure (no Steam, unusual layout) — callers fall back.
    """
    global _installed_games_cache  # noqa: PLW0603
    if _installed_games_cache is not None:
        return _installed_games_cache
    games: dict[str, str] = {}
    if sys.platform.startswith("win"):
        import os

        candidates: list[Path] = [
            Path(os.environ.get("PROGRAMFILES(X86)", "C:\\Program Files (x86)"))
            / "Steam" / "steamapps",
            Path(os.environ.get("PROGRAMFILES", "C:\\Program Files")) / "Steam" / "steamapps",
        ]
        # The registry is the truth for custom install drives ('E:\\steam',
        # per-user installs) — HKCU\\Software\\Valve\\Steam\\SteamPath.
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\\Valve\\Steam") as key:
                steam_path, _kind = winreg.QueryValueEx(key, "SteamPath")
                if steam_path:
                    candidates.insert(0, Path(steam_path.replace("/", "\\")) / "steamapps")
        except OSError:
            pass  # no Steam registry entry; env-path candidates still apply
        # Extra libraries live in libraryfolders.vdf — parse them so games on
        # other drives are found too.
        for base in list(candidates):
            vdf = base.parent / "steamapps" / "libraryfolders.vdf"
            if vdf.exists():
                try:
                    for match in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8", errors="replace")):
                        candidates.append(Path(match.group(1).replace("\\\\", "\\")) / "steamapps")
                except OSError:
                    pass
        for folder in candidates:
            try:
                for manifest in folder.glob("appmanifest_*.acf"):
                    text = manifest.read_text(encoding="utf-8", errors="replace")
                    id_match = re.search(r'"appid"\s+"(\d+)"', text)
                    name_match = re.search(r'"name"\s+"([^"]+)"', text)
                    if id_match and name_match:
                        games[name_match.group(1).strip().lower()] = id_match.group(1)
            except OSError:
                continue
    _installed_games_cache = games
    return games


_installed_games_cache: dict[str, str] | None = None


def _steam_game_id(game: str) -> str | None:
    """Resolve a spoken game name to a Steam AppID (None = unknown game).

    Two registries, static first: the well-known table, then the games Steam
    reports as INSTALLED on this machine (appmanifest_*.acf) — so a user's own
    library works even when the title is not in the table.
    """
    value = game.strip().lower()
    value = re.sub(r"\s*(game|please|now)\s*$", "", value)
    value = re.sub(r"\s+", " ", value)
    if not value:
        return None  # empty input matches nothing — the reversed word-boundary
        # check below would otherwise match EVERY name against an empty string
    if value in _STEAM_APP_IDS:
        return _STEAM_APP_IDS[value]
    # Substring match with WORD BOUNDARIES: 'launch grand theft auto v
    # enhanced' -> 'gta v' key, but 'supermarket together' must NOT match the
    # 'ark' key inside 'superm*ark*et' (a real bug this guard fixed).
    for name, appid in _STEAM_APP_IDS.items():
        if re.search(rf"\b{re.escape(name)}\b", value):
            return appid
    # Installed-library resolution: exact manifest name, then whole-word
    # substring in either direction ('grand theft auto v enhanced' matches the
    # installed 'Grand Theft Auto V Enhanced' manifest name).
    installed = _installed_steam_games()
    if value in installed:
        return installed[value]
    for name, appid in installed.items():
        if re.search(rf"\b{re.escape(name)}\b", value) or re.search(rf"\b{re.escape(value)}\b", name):
            return appid
    return None


# Spoken game name -> the window titles the game process actually shows.
# 'gta v' never matches: the window is 'Grand Theft Auto V' (and the launcher
# process is PlayGTAV). Big titles also take 30-60s to show their window.
_GAME_WINDOW_TITLES: dict[str, tuple[str, ...]] = {
    "gta v": ("grand theft auto v", "gtav", "gta v", "playgtav"),
    "grand theft auto v": ("grand theft auto v", "gtav", "gta v", "playgtav"),
    "counter-strike 2": ("counter-strike 2", "cs2"),
    "dota 2": ("dota 2",),
    "rocket league": ("rocket league",),
    "cyberpunk 2077": ("cyberpunk 2077", "cyberpunk"),
    "elden ring": ("elden ring",),
}


def _game_window_stems(game: str) -> tuple[str, ...]:
    """Window-title stems to verify a game launch against, most likely first."""
    value = game.strip().lower()
    for name, titles in _GAME_WINDOW_TITLES.items():
        if name in value:
            return titles
    return (_window_stem(game),)


# Spoken game -> the PROCESS names its launcher actually runs (a launcher can
# run windowless for a long time before the game window exists).
_GAME_PROCESS_NAMES: dict[str, tuple[str, ...]] = {
    "gta v": ("PlayGTAV", "GrandTheftAutoV"),
    "grand theft auto v": ("PlayGTAV", "GrandTheftAutoV"),
    "cyberpunk 2077": ("Cyberpunk2077",),
    "elden ring": ("eldenring",),
    "rocket league": ("RocketLeague",),
}


def _game_process_names(game: str) -> tuple[str, ...]:
    """Process names that prove a game is launching (windowless launchers)."""
    value = game.strip().lower()
    for name, procs in _GAME_PROCESS_NAMES.items():
        if name in value:
            return procs
    return ()

# Spoken destinations -> canonical deep-link slugs per app.
_APP_NAVIGATION_WORDS: dict[str, str] = {
    "library": "library",
    "my library": "library",
    "games library": "library",
    "store": "store",
    "downloads": "downloads",
    "settings": "settings",
    "friends": "friends",
    "home": "home",
}


def _resolve_navigation_destination(app: str, destination: str) -> str:
    """Map a spoken destination ('my library') to the app's deep-link slug."""
    value = destination.strip().lower()
    app_key = app.strip().lower()
    # App-specific vocabulary first (ms-settings pages are not generic words).
    if app_key in ("settings", "ms-settings"):
        return value.replace(" ", "")
    return _APP_NAVIGATION_WORDS.get(value, value.replace(" ", "-"))


def _single_action_workflow(
    name: str,
    action_type: DesktopActionType,
    target: str,
    description: str,
    parameters: Mapping[str, Any] | None = None,
) -> DesktopWorkflow:
    """Wrap one action in a workflow — the shared shape of every single-action plan."""
    return DesktopWorkflow(
        name=name,
        actions=(
            DesktopAction(
                type=action_type,
                target=target,
                description=description,
                parameters=parameters or {},
            ),
        ),
    )


class DesktopAutomationController:
    """Plans and executes approved desktop automation workflows."""

    def __init__(
        self,
        *,
        approval_gateway: ApprovalGateway | None = None,
        runner: DesktopCommandRunner | None = None,
        audit_log: AutomationAuditLog | None = None,
    ) -> None:
        self.approval_gateway = approval_gateway or DenyByDefaultApprovalGateway()
        # Screen coordinates of the most recent vision-guided click (None = none yet).
        # Set by LocalDesktopRunner._vision_click; consumed by verification.
        self._last_click_point: tuple[int, int] | None = None
        self.runner = runner or NoopDesktopRunner()
        self.audit_log = audit_log or InMemoryAutomationAuditLog()

    def plan_open_application(self, application: str) -> DesktopWorkflow:
        return _single_action_workflow(
            f"Open {application}", DesktopActionType.OPEN_APPLICATION, application, f"Open application {application}",
        )

    def plan_app_navigate(self, app: str, destination: str) -> DesktopWorkflow:
        """Plan an in-app navigation ("go to library" in Steam) via deep link."""
        slug = _resolve_navigation_destination(app, destination)
        return _single_action_workflow(
            f"Go to {destination} in {app}",
            DesktopActionType.APP_NAVIGATE,
            slug,
            f"Navigate to '{destination}' inside {app}.",
            {"app": app.strip().lower()},
        )

    def plan_vision_click(self, label: str, *, screenshot_path: str | None = None) -> DesktopWorkflow:
        """Plan a vision-guided click on a labeled UI element."""
        return _single_action_workflow(
            f"Click {label}",
            DesktopActionType.VISION_CLICK,
            label,
            f"Vision-locate and click '{label}' on screen.",
            {"screenshot_path": screenshot_path} if screenshot_path else {},
        )

    def plan_game_launch(self, game: str, *, app: str = "steam") -> DesktopWorkflow:
        """Plan launching a known game via its platform's deep link."""
        return _single_action_workflow(
            f"Launch {game}",
            DesktopActionType.GAME_LAUNCH,
            game,
            f"Launch '{game}' through {app.title()} (direct game deep link).",
            {"app": app},
        )

    def plan_stop(self, target: str = "") -> DesktopWorkflow:
        """Plan stopping the last launched/focused app gracefully."""
        return _single_action_workflow(
            "Stop", DesktopActionType.STOP_APP, target, f"Stop '{target or 'the last app'}' (close its window).",
        )

    def plan_execute_script(self, command: str, *, working_directory: str | None = None) -> DesktopWorkflow:
        return _single_action_workflow(
            "Execute script", DesktopActionType.EXECUTE_SCRIPT, command,
            "Execute a user-approved script or command.",
            {"working_directory": working_directory},
        )

    def plan_file_organization(
        self,
        source_directory: str,
        extension_to_directory: Mapping[str, str],
    ) -> DesktopWorkflow:
        actions = tuple(
            DesktopAction(
                type=DesktopActionType.ORGANIZE_FILES,
                target=source_directory,
                description=f"Move *{extension} files into {destination}",
                parameters={"extension": extension, "destination": destination},
            )
            for extension, destination in extension_to_directory.items()
        )
        return DesktopWorkflow(name="Organize files", actions=actions)

    def plan_screenshot(self, save_path: str = "screenshot.png") -> DesktopWorkflow:
        return _single_action_workflow(
            "Take screenshot", DesktopActionType.TAKE_SCREENSHOT, "screenshot",
            "Capture the current screen.", {"save_path": save_path},
        )

    def plan_system_info(self) -> DesktopWorkflow:
        return _single_action_workflow(
            "System information", DesktopActionType.SYSTEM_INFO, "system", "Gather system information.",
        )

    def plan_list_files(self, directory: str, *, recursive: bool = False) -> DesktopWorkflow:
        return _single_action_workflow(
            "List files", DesktopActionType.LIST_FILES, directory, f"List files in {directory}.",
            {"recursive": recursive},
        )

    def plan_keyboard_shortcut(self, shortcut: str) -> DesktopWorkflow:
        return _single_action_workflow(
            "Keyboard shortcut", DesktopActionType.KEYBOARD_SHORTCUT, shortcut, f"Press keyboard shortcut {shortcut}.",
        )

    def plan_type_text(self, text: str, *, delay_ms: int = 30) -> DesktopWorkflow:
        return _single_action_workflow(
            "Type text", DesktopActionType.TYPE_TEXT, text, f"Type '{text}' into the focused window.",
            {"delay_ms": delay_ms},
        )

    def plan_open_folder(self, folder_path: str) -> DesktopWorkflow:
        return _single_action_workflow(
            "Open folder", DesktopActionType.OPEN_FOLDER, folder_path, f"Open folder '{folder_path}'.",
        )

    def plan_search_start_menu(self, query: str) -> DesktopWorkflow:
        return _single_action_workflow(
            f"Search for {query}", DesktopActionType.SEARCH_START_MENU, query, f"Search Start Menu for '{query}'.",
        )

    def plan_web_search(self, query: str, *, browser: str | None = None) -> DesktopWorkflow:
        """Plan a real web search: a browser tab opened on the results URL.

        Used for the search step of chains like 'open chrome and search for cats'
        so the search happens in the browser, not the Windows Start Menu.
        """
        return _single_action_workflow(
            f"Search the web for {query}", DesktopActionType.WEB_SEARCH, query,
            f"Search the web for '{query}' in a browser tab.",
            {"browser": browser} if browser else {},
        )

    def plan_command(self, command: str) -> tuple[DesktopWorkflow, list[str], str]:
        """Parse a JARVIS command into steps and plan each through this seam.

        Returns (combined workflow, step descriptions, first target). The caller
        (application.py) only wraps this in the plan envelope and mints the
        approval token — it never dispatches on step kinds again.
        """
        steps = parse_desktop_command(command)
        descriptions = [step.describe() for step in steps]
        actions: list[DesktopAction] = []
        # A search step following "open <browser>" runs INSIDE that browser:
        # its query becomes a results URL opened as a tab, never a Start-Menu
        # keystroke into the wrong surface.
        last_browser: str | None = None
        for step in steps:
            if step.kind == "open":
                last_browser = _BROWSER_LAUNCH_NAMES.get(step.target.strip().lower())
            if step.kind == "search" and last_browser:
                actions.extend(self.plan_web_search(step.text, browser=last_browser).actions)
            else:
                actions.extend(step.plan(self).actions)
        combined = DesktopWorkflow(name=command[:60], actions=tuple(actions))
        first_target = steps[0].target or steps[0].text or command if steps else command
        return combined, descriptions, first_target

    async def execute_workflow(
        self,
        workflow: DesktopWorkflow,
        *,
        progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> tuple[DesktopActionResult, ...]:
        """Run each action in order, optionally announcing each one as it starts.

        The optional `progress` coroutine is awaited with a human line ("Running
        action 2/3: ...") right before the action executes — callers such as the
        web layer use it to stream live progress while slower actions (window
        launch + verification) are in flight.
        """
        results = []
        total = len(workflow.actions)
        for index, action in enumerate(workflow.actions, start=1):
            if progress is not None:
                await progress(f"Running action {index}/{total}: {action.description}")
            result = await self.execute_action(action)
            results.append(result)
            if result.status is not DesktopActionStatus.COMPLETED:
                break
        return tuple(results)

    async def execute_action(self, action: DesktopAction) -> DesktopActionResult:
        approval = await self.approval_gateway.request_approval(
            ApprovalRequest(
                action=action.description,
                reason="Desktop automation requires explicit user approval.",
                permissions=_permissions_for(action.type),
                risk=RiskLevel.HIGH,
                metadata=action.to_dict(),
            )
        )
        if not approval.approved:
            result = DesktopActionResult(
                action_id=action.id,
                status=DesktopActionStatus.DENIED,
                output={},
                error=approval.reason or "Desktop automation was not approved.",
                approval_id=approval.request_id,
            )
            await self.audit_log.append(result)
            return result

        try:
            output = await self.runner.run(action)
            result = DesktopActionResult(
                action_id=action.id,
                status=DesktopActionStatus.COMPLETED,
                output=dict(output),
                approval_id=approval.request_id,
            )
        except Exception as exc:
            result = DesktopActionResult(
                action_id=action.id,
                status=DesktopActionStatus.FAILED,
                output={},
                error=f"{type(exc).__name__}: {exc}",
                approval_id=approval.request_id,
            )
        await self.audit_log.append(result)
        return result


def _permissions_for(action_type: DesktopActionType) -> tuple[PermissionScope, ...]:
    if action_type is DesktopActionType.EXECUTE_SCRIPT:
        return (PermissionScope.DESKTOP_CONTROL, PermissionScope.SHELL_EXECUTE)
    if action_type is DesktopActionType.ORGANIZE_FILES:
        return (PermissionScope.DESKTOP_CONTROL, PermissionScope.FILESYSTEM_WRITE)
    if action_type is DesktopActionType.WRITE_FILE:
        return (PermissionScope.DESKTOP_CONTROL, PermissionScope.FILESYSTEM_WRITE)
    if action_type is DesktopActionType.READ_FILE:
        return (PermissionScope.DESKTOP_CONTROL,)
    if action_type is DesktopActionType.LIST_FILES:
        return (PermissionScope.DESKTOP_CONTROL,)
    if action_type is DesktopActionType.KEYBOARD_SHORTCUT:
        return (PermissionScope.DESKTOP_CONTROL, PermissionScope.SHELL_EXECUTE)
    if action_type is DesktopActionType.TYPE_TEXT:
        return (PermissionScope.DESKTOP_CONTROL,)
    if action_type is DesktopActionType.OPEN_FOLDER:
        return (PermissionScope.DESKTOP_CONTROL,)
    if action_type is DesktopActionType.SEARCH_START_MENU:
        return (PermissionScope.DESKTOP_CONTROL,)
    if action_type is DesktopActionType.WEB_SEARCH:
        return (PermissionScope.DESKTOP_CONTROL,)
    return (PermissionScope.DESKTOP_CONTROL,)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
