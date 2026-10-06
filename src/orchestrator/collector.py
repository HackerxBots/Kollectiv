"""Parse, structure and merge worker-agent output.

Agents answer in markdown with fenced code blocks tagged by file path::

    ```python path=src/app.py
    print("hello")
    ```

The collector turns that prose into files on disk, extracts the commit
messages and errors the agent mentioned, and merges several agents' results
into one project artifact -- detecting file conflicts (two agents editing the
same path) and unreconciled interface mismatches.

Usage::

    collector = Collector(workspace="./data/workspace")
    result = await collector.collect_result(task, raw_output)
    merged = await collector.merge_results([result, other_result])
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from config.settings import Settings, get_settings
from src.utils.logger import get_logger

LOGGER = get_logger(__name__)

#: Directories the collector refuses to write into.
FORBIDDEN_PATH_PARTS = {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache"}

#: Maximum size of a single extracted file (protects against runaway output).
MAX_FILE_BYTES = 2 * 1024 * 1024

#: Matches an opening fence with an optional info string.
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})\s*(.*)$")

#: Info strings that mean "this is not a file, it's a language snippet".
_LANGUAGE_ONLY = {
    "python",
    "py",
    "js",
    "javascript",
    "ts",
    "typescript",
    "bash",
    "sh",
    "shell",
    "json",
    "yaml",
    "yml",
    "toml",
    "sql",
    "html",
    "css",
    "text",
    "txt",
    "markdown",
    "md",
    "diff",
    "console",
    "output",
    "mermaid",
    "xml",
    "dockerfile",
    "ini",
    "env",
    "http",
}

_PATH_PATTERN = re.compile(r"(?:path|file|filename)\s*[=:]\s*[\"']?([^\s\"']+)[\"']?", re.IGNORECASE)
_LOOKS_LIKE_PATH = re.compile(r"^[\w./-]+\.(?:py|js|ts|tsx|jsx|json|ya?ml|toml|md|txt|sh|cfg|ini|env|sql|html|css|go|rs|java|rb)$")

_ERROR_PATTERNS = (
    re.compile(r"^(?:error|exception|traceback|failed|failure)\b.*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^.*\bTraceback \(most recent call last\)\b.*$", re.MULTILINE),
)
_COMMIT_PATTERNS = (
    re.compile(r"^\s*(?:commit message|commit):\s*(.+)$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*`?([a-z]+(?:\([\w./-]+\))?!?:\s.+)$", re.MULTILINE),  # conventional commit
)
_TODO_PATTERN = re.compile(r"\b(TODO|FIXME|XXX|placeholder|not implemented)\b", re.IGNORECASE)


class Collector:
    """Turn agent prose into structured, on-disk artifacts.

    Args:
        workspace: Directory that receives extracted files. Defaults to
            ``WORKSPACE_DIR/<project>/``.
        settings: Optional settings override.
        write_files: When ``False`` files are parsed but not written (dry run).
        project_id: Project the artifacts belong to (used for the workspace path).
    """

    def __init__(
        self,
        workspace: Optional[str] = None,
        settings: Optional[Settings] = None,
        write_files: bool = True,
        project_id: Optional[str] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.project_id = project_id or ""
        base = workspace or str(self.settings.workspace_path / (self.project_id or "default"))
        self.workspace = Path(base)
        self.write_files = write_files
        if self.write_files:
            self.workspace.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Single result
    # ------------------------------------------------------------------
    async def collect_result(self, task: Dict[str, Any], raw_output: str) -> Dict[str, Any]:
        """Parse one agent's output into a structured result.

        Args:
            task: The task the output answers.
            raw_output: The agent's raw response text.

        Returns:
            A result dict::

                {
                  "task_id": str, "success": bool, "raw_length": int,
                  "files": [{path, content, size, checksum, language}],
                  "code_blocks": int, "file_paths": [str],
                  "commit_messages": [str], "errors": [str],
                  "placeholders": [str], "summary": str,
                  "written": [str], "written_errors": [str]
                }
        """
        if not isinstance(raw_output, str):
            raw_output = "" if raw_output is None else str(raw_output)

        blocks = self.extract_code_blocks(raw_output)
        files: List[Dict[str, Any]] = []
        for block in blocks:
            path = block.get("path") or ""
            if not path:
                continue
            files.append(self._build_file_entry(path, block["content"], block.get("language", "")))

        # De-duplicate on path, keeping the longest (most complete) version.
        by_path: Dict[str, Dict[str, Any]] = {}
        for entry in files:
            current = by_path.get(entry["path"])
            if current is None or len(entry["content"]) > len(current["content"]):
                by_path[entry["path"]] = entry
        files = list(by_path.values())

        written: List[str] = []
        written_errors: List[str] = []
        if self.write_files:
            for entry in files:
                try:
                    await self.write_file(entry["path"], entry["content"])
                    written.append(entry["path"])
                except Exception as exc:  # noqa: BLE001 - report, never raise
                    written_errors.append(f"{entry['path']}: {exc}")
                    LOGGER.error("Could not write %s: %s", entry["path"], exc)

        errors = self.extract_errors(raw_output)
        commit_messages = self.extract_commit_messages(raw_output)
        placeholders = sorted({match.group(0) for match in _TODO_PATTERN.finditer(raw_output)})

        # A result counts as successful when it produced usable artifacts:
        # either path-tagged files, or a substantive prose answer that never
        # claimed to contain code. A one-line apology is not a success.
        has_files = bool(files)
        prose_only = "```" not in raw_output and len(raw_output.strip()) >= 200
        success = bool(raw_output.strip()) and not written_errors and (has_files or prose_only)

        result = {
            "task_id": task.get("id") or task.get("task_id") or "",
            "title": task.get("title", ""),
            "success": success,
            "produced_files": has_files,
            "raw_length": len(raw_output),
            "files": files,
            "file_paths": [entry["path"] for entry in files],
            "code_blocks": len(blocks),
            "commit_messages": commit_messages,
            "errors": errors,
            "placeholders": placeholders,
            "summary": self.summarize(raw_output, files),
            "written": written,
            "written_errors": written_errors,
            "collected_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "agent_id": task.get("assigned_agent") or task.get("agent_id") or "",
        }
        LOGGER.info(
            "Collected task %s: %s file(s), %s error line(s), %s placeholder(s)",
            result["task_id"] or "?",
            len(files),
            len(errors),
            len(placeholders),
        )
        return result

    def _build_file_entry(self, path: str, content: str, language: str) -> Dict[str, Any]:
        """Build the file entry stored in a result (with hashing)."""
        normalized = self.normalize_path(path)
        return {
            "path": normalized,
            "content": content,
            "size": len(content.encode("utf-8", errors="replace")),
            "checksum": hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:16],
            "language": language,
            "raw_path": path,
        }

    # ------------------------------------------------------------------
    # Extraction
    # ------------------------------------------------------------------
    def extract_code_blocks(self, text: str) -> List[Dict[str, Any]]:
        """Extract fenced code blocks and the file path each one targets.

        Recognised info strings, in order of precedence::

            ```python path=src/app.py     -> path from the key/value pair
            ```path=src/app.py            -> path only
            src/app.py                    -> a bare path on the info line
            ```python                     -> language only, no file

        Args:
            text: The agent output.

        Returns:
            ``[{path, content, language, info}]`` -- ``path`` is empty for
            plain snippets that do not claim to be a file.
        """
        blocks: List[Dict[str, Any]] = []
        lines = text.splitlines()
        index = 0
        while index < len(lines):
            opener = _FENCE_RE.match(lines[index].strip())
            if not opener:
                index += 1
                continue
            fence, info = opener.group(1), opener.group(2).strip()
            body: List[str] = []
            index += 1
            while index < len(lines):
                candidate = lines[index]
                if candidate.strip().startswith(fence[0] * 3) and candidate.strip().strip(fence[0]) == "":
                    break
                body.append(candidate)
                index += 1
            index += 1  # skip the closing fence

            path, language = self._parse_info_string(info)
            blocks.append({"path": path, "content": "\n".join(body), "language": language, "info": info})
        return blocks

    @staticmethod
    def _parse_info_string(info: str) -> Tuple[str, str]:
        """Split a fence info string into ``(path, language)``."""
        if not info:
            return "", ""
        match = _PATH_PATTERN.search(info)
        if match:
            path = match.group(1)
            language = info.split()[0] if info.split() else ""
            if language.lower() in _LANGUAGE_ONLY or "=" in language or ":" in language:
                language = ""
            return path, language

        tokens = info.split()
        for token in tokens:
            if token.lower() in _LANGUAGE_ONLY:
                return "", token.lower()
        for token in tokens:
            if "/" in token or _LOOKS_LIKE_PATH.match(token):
                return token, ""
        return "", tokens[0].lower() if tokens else ""

    @staticmethod
    def extract_errors(text: str) -> List[str]:
        """Extract error/traceback lines from agent output.

        Args:
            text: The agent output.

        Returns:
            Up to 20 unique error lines.
        """
        found: List[str] = []
        for pattern in _ERROR_PATTERNS:
            for match in pattern.findall(text):
                line = (match if isinstance(match, str) else " ".join(match)).strip()
                if line and line not in found:
                    found.append(line)
        # Lines inside fenced output blocks are usually the useful ones.
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("E   ", "ERROR:", "FAILED ", "!!!")) and stripped not in found:
                found.append(stripped)
        return found[:20]

    @staticmethod
    def extract_commit_messages(text: str) -> List[str]:
        """Extract commit messages the agent proposed.

        Args:
            text: The agent output.

        Returns:
            Up to 10 unique commit messages.
        """
        messages: List[str] = []
        for pattern in _COMMIT_PATTERNS:
            for match in pattern.findall(text):
                candidate = match.strip().strip("`\"'")
                if 3 < len(candidate) < 200 and candidate not in messages:
                    messages.append(candidate)
        return messages[:10]

    @staticmethod
    def summarize(text: str, files: Sequence[Dict[str, Any]]) -> str:
        """Build a one-line summary of an agent result.

        Args:
            text: The raw output.
            files: Extracted files.

        Returns:
            A short summary mentioning file count, paths and any blockers.
        """
        if not text.strip():
            return "No output."
        paths = ", ".join(entry["path"] for entry in files[:5]) or "no named files"
        placeholders = len(_TODO_PATTERN.findall(text))
        summary = f"{len(files)} file(s) [{paths}] from {len(text)} chars"
        if placeholders:
            summary += f"; {placeholders} placeholder marker(s)"
        return summary

    # ------------------------------------------------------------------
    # File system
    # ------------------------------------------------------------------
    def normalize_path(self, path: str) -> str:
        """Sanitise an agent supplied path into a safe repository relative path.

        Strips leading ``./`` and ``/``, removes any ``..`` traversal and
        refuses paths inside ``.git``/``node_modules``/&c.

        Args:
            path: The raw path from the code fence.

        Returns:
            A safe relative path.
        """
        cleaned = path.strip().strip("`\"'").replace("\\", "/")
        cleaned = cleaned.split()[0] if cleaned.split() else ""
        cleaned = re.sub(r"^[a-zA-Z]:", "", cleaned)
        cleaned = cleaned.lstrip("/")
        while cleaned.startswith("./"):
            cleaned = cleaned[2:]
        parts = [part for part in cleaned.split("/") if part not in ("", ".", "..")]
        parts = [part for part in parts if part not in FORBIDDEN_PATH_PARTS]
        if not parts:
            return "unnamed.txt"
        return "/".join(parts)

    async def write_file(self, path: str, content: str) -> str:
        """Write an extracted file into the workspace.

        Args:
            path: Repository relative path.
            content: File content.

        Returns:
            The absolute path written.

        Raises:
            ValueError: When the content exceeds :data:`MAX_FILE_BYTES`.
        """
        encoded = content.encode("utf-8", errors="replace")
        if len(encoded) > MAX_FILE_BYTES:
            raise ValueError(f"refusing to write {len(encoded)} bytes (limit {MAX_FILE_BYTES})")
        target = self.workspace / self.normalize_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)

        def _write() -> None:
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(content)

        await asyncio.to_thread(_write)
        return str(target)

    def read_file(self, path: str) -> Optional[str]:
        """Read a file from the workspace (``None`` when absent)."""
        target = self.workspace / self.normalize_path(path)
        if not target.is_file():
            return None
        try:
            return target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            LOGGER.error("Could not read %s: %s", target, exc)
            return None

    def list_workspace_files(self, subdir: str = "") -> List[Dict[str, Any]]:
        """List every file written to the workspace so far.

        Args:
            subdir: Optional sub-directory to restrict the listing.

        Returns:
            ``[{path, size, checksum}]`` relative to the workspace root.
        """
        root = self.workspace / subdir if subdir else self.workspace
        entries: List[Dict[str, Any]] = []
        if not root.exists():
            return entries
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if any(part in FORBIDDEN_PATH_PARTS for part in path.parts):
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            entries.append(
                {
                    "path": str(path.relative_to(self.workspace)),
                    "size": len(data),
                    "checksum": hashlib.sha256(data).hexdigest()[:16],
                }
            )
        return entries

    # ------------------------------------------------------------------
    # Merging
    # ------------------------------------------------------------------
    async def merge_results(self, results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        """Combine several agent results into one project artifact.

        Conflicts (the same path produced by two agents with different
        content) are reported with both checksums; the longer version wins so
        the workspace stays buildable. Missing dependencies between tasks are
        also detected (a task referencing a file nobody produced).

        Args:
            results: Result dicts from :meth:`collect_result`.

        Returns:
            ``{files, conflicts, file_count, total_bytes, tasks, errors,
            missing_dependencies, merged_at, success_rate}``.
        """
        merged: Dict[str, Dict[str, Any]] = {}
        conflicts: List[Dict[str, Any]] = []
        errors: List[str] = []
        tasks: List[Dict[str, Any]] = []
        missing: List[str] = []

        for result in results:
            if not isinstance(result, dict):
                continue
            task_id = result.get("task_id") or ""
            tasks.append(
                {
                    "task_id": task_id,
                    "title": result.get("title", ""),
                    "agent_id": result.get("agent_id", ""),
                    "success": bool(result.get("success")),
                    "files": result.get("file_paths", []),
                    "errors": result.get("errors", []),
                }
            )
            errors.extend(result.get("errors", []) or [])

            for entry in result.get("files", []) or []:
                path = entry.get("path") or ""
                if not path:
                    continue
                existing = merged.get(path)
                if existing is None:
                    merged[path] = dict(entry, producers=[task_id] if task_id else [])
                    continue
                producers = list(existing.get("producers", []))
                if task_id:
                    producers.append(task_id)
                if existing.get("checksum") == entry.get("checksum"):
                    merged[path] = dict(existing, producers=producers)
                    continue
                conflicts.append(
                    {
                        "path": path,
                        "producers": producers,
                        "checksums": [existing.get("checksum"), entry.get("checksum")],
                        "sizes": [existing.get("size"), entry.get("size")],
                        "resolution": "kept the larger version",
                    }
                )
                LOGGER.warning(
                    "Conflict on %s between %s (keeping the larger version)",
                    path,
                    ", ".join(producers) or "unknown agents",
                )
                winner = existing if (existing.get("size") or 0) >= (entry.get("size") or 0) else entry
                merged[path] = dict(winner, producers=producers)

        # Detect modules the agents imported but nobody produced. Both
        # relative paths (``src/helpers``) and dotted modules
        # (``src.helpers``) are normalised before the lookup.
        produced = set(merged)
        for entry in merged.values():
            pattern = r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))"
            for line in entry.get("content", "").splitlines():
                match = re.match(pattern, line)
                if not match:
                    continue
                name = (match.group(1) or match.group(2) or "").strip()
                if not name or name.startswith("."):
                    continue
                candidate = name.replace(".", "/") + ".py"
                if name in produced or candidate in produced:
                    continue
                if any(path.endswith(candidate) for path in produced):
                    continue
                missing.append(candidate)

        total_bytes = sum(int(entry.get("size") or 0) for entry in merged.values())
        successful = [task for task in tasks if task["success"]]
        artifact = {
            "files": list(merged.values()),
            "file_paths": sorted(merged),
            "conflicts": conflicts,
            "file_count": len(merged),
            "total_bytes": total_bytes,
            "tasks": tasks,
            "errors": errors[:50],
            "missing_dependencies": sorted(set(missing))[:30],
            "success_rate": round(len(successful) / len(tasks), 3) if tasks else 0.0,
            "merged_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        LOGGER.info(
            "Merged %s result(s): %s file(s), %s byte(s), %s conflict(s)",
            len(tasks),
            artifact["file_count"],
            artifact["total_bytes"],
            len(conflicts),
        )
        return artifact

    async def write_merged_files(self, merged: Dict[str, Any]) -> Dict[str, List[str]]:
        """Persist every file of a merged artifact to the workspace.

        Args:
            merged: Output of :meth:`merge_results`.

        Returns:
            ``{written: [...], failed: [...]}``.
        """
        written: List[str] = []
        failed: List[str] = []
        for entry in merged.get("files", []):
            try:
                await self.write_file(entry["path"], entry.get("content", ""))
                written.append(entry["path"])
            except Exception as exc:  # noqa: BLE001 - report, never raise
                failed.append(f"{entry.get('path')}: {exc}")
        return {"written": written, "failed": failed}

    def build_repo_snapshot(self, merged: Dict[str, Any], limit: int = 60) -> List[Dict[str, Any]]:
        """Return a compact file inventory suitable for the shared state.

        Args:
            merged: Output of :meth:`merge_results`.
            limit: Maximum entries.

        Returns:
            ``[{path, size, checksum}]``.
        """
        entries: Iterable[Dict[str, Any]] = merged.get("files", [])
        snapshot = [
            {"path": entry.get("path"), "size": entry.get("size", 0), "checksum": entry.get("checksum", "")}
            for entry in entries
            if entry.get("path")
        ]
        snapshot.sort(key=lambda item: item["path"] or "")
        return snapshot[:limit]


__all__ = ["Collector", "MAX_FILE_BYTES"]
