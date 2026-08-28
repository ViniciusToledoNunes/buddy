from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .memory import STOP_WORDS, _tokens

# BM25 length normalisation. Cosine normalisation punished long files so hard that a
# short test file outranked the module it tests; b=0.5 keeps size honest without that.
BM25_K1 = 1.2
BM25_B = 0.5
# A hit in the path is stronger evidence than a hit in the body: a meeting about
# storage means storage.py, not whichever file happens to say the word most.
PATH_WEIGHT = 2.5

IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")
IDENTIFIER_PARTS = re.compile(r"_+|(?<=[a-z0-9])(?=[A-Z])")


def _code_tokens(text: str) -> set[str]:
    """Tokens for source, where the meeting's vocabulary hides inside identifiers.

    A meeting says "invoice"; the code says reconcile_invoice or reconcileInvoice.
    Splitting snake_case and camelCase is what connects the two.
    """
    tokens = set(_tokens(text))
    for identifier in IDENTIFIER.findall(text):
        for part in IDENTIFIER_PARTS.split(identifier):
            if len(part) >= 3:
                tokens.add(part.lower())
    return tokens - STOP_WORDS


def _code_tokens_list(text: str) -> list[str]:
    """_code_tokens without deduplication, so BM25 can see term frequency."""
    tokens: list[str] = []
    for identifier in IDENTIFIER.findall(text):
        for part in IDENTIFIER_PARTS.split(identifier):
            lowered = part.lower()
            if len(lowered) >= 3 and lowered not in STOP_WORDS:
                tokens.append(lowered)
    return tokens

# Directories that hold dependencies, build output, or recorded meetings. Indexing them
# buries real project code under vendored text.
SKIP_DIRECTORIES = {
    ".git", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".ruff_cache", "__pycache__",
    "node_modules", "dist", "build", "site-packages", ".idea", ".vscode", ".tox",
    "meetings", "models", "tools", "htmlcov", ".eggs",
}

# Only text formats a reader would call source or documentation.
SOURCE_SUFFIXES = {
    ".py", ".pyi", ".md", ".rst", ".txt", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".json", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".css", ".scss", ".html",
    ".sh", ".bash", ".ps1", ".swift", ".kt", ".rs", ".go", ".java", ".rb", ".php",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".sql", ".proto", ".graphql", ".dockerfile",
}

# Excerpts are sent to the model, so anything that carries credentials is unreachable by
# construction rather than by ranking. Matched against the lowercased file name.
SECRET_NAMES = {"credentials", "secrets", "secret", "id_rsa", "id_ed25519", "keyfile"}
SECRET_SUFFIXES = {".pem", ".key", ".pfx", ".p12", ".jks", ".keystore", ".crt", ".cer", ".asc", ".gpg"}


def _is_secret(path: Path) -> bool:
    name = path.name.lower()
    if name.startswith(".env"):
        return True
    if path.suffix.lower() in SECRET_SUFFIXES:
        return True
    stem = path.stem.lower()
    return stem in SECRET_NAMES or any(marker in stem for marker in ("password", "credential", "apikey"))


class ProjectIndex:
    """Ranks project files against the meeting so the copilot can cite code directly.

    This is the same weighted term-overlap ranking MeetingMemoryIndex uses, pointed at
    source instead of meeting memory. It buys grounded suggestions for the cost of one
    prompt block, with no tool round-trip in the real-time path.
    """

    def __init__(
        self,
        root: Path,
        max_files: int = 600,
        max_file_bytes: int = 120_000,
        max_excerpt_lines: int = 40,
    ) -> None:
        self.root = Path(root).expanduser()
        self.max_files = max(1, max_files)
        self.max_file_bytes = max(1_000, max_file_bytes)
        self.max_excerpt_lines = max(4, max_excerpt_lines)

    def _candidates(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        found: list[Path] = []
        stack = [self.root]
        while stack and len(found) < self.max_files:
            directory = stack.pop()
            try:
                entries = sorted(directory.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.is_dir():
                    if entry.name not in SKIP_DIRECTORIES and not entry.name.startswith(".") or entry.name == ".github":
                        stack.append(entry)
                    continue
                if entry.suffix.lower() not in SOURCE_SUFFIXES or _is_secret(entry):
                    continue
                try:
                    if entry.stat().st_size > self.max_file_bytes:
                        continue
                except OSError:
                    continue
                found.append(entry)
                if len(found) >= self.max_files:
                    break
        return found

    @staticmethod
    def _read(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, ValueError, UnicodeDecodeError):
            return ""

    def _excerpt(self, text: str, query_tokens: set[str]) -> str:
        """The densest window of matching lines, so the model sees the relevant code."""
        lines = text.splitlines()
        if len(lines) <= self.max_excerpt_lines:
            return "\n".join(lines)
        scores = [len(query_tokens & _code_tokens(line)) for line in lines]
        window = self.max_excerpt_lines
        best_start, best_score = 0, -1
        running = sum(scores[:window])
        for start in range(len(lines) - window + 1):
            if start:
                running += scores[start + window - 1] - scores[start - 1]
            if running > best_score:
                best_start, best_score = start, running
        return "\n".join(lines[best_start : best_start + window])

    def find_related(self, query: str, limit: int = 3) -> list[dict[str, Any]]:
        query_tokens = _tokens(query)
        if not query_tokens:
            return []
        documents: list[tuple[str, str, Counter[str], set[str]]] = []
        document_frequency: Counter[str] = Counter()
        for path in self._candidates():
            text = self._read(path)
            if not text.strip():
                continue
            relative = path.relative_to(self.root).as_posix()
            counts = Counter(_code_tokens_list(text))
            path_tokens = _code_tokens(relative)
            counts.update(path_tokens)
            if not counts:
                continue
            documents.append((relative, text, counts, path_tokens))
            document_frequency.update(counts.keys())
        if not documents:
            return []
        total = len(documents)
        average_length = sum(sum(counts.values()) for _, _, counts, _ in documents) / total
        ranked: list[dict[str, Any]] = []
        for relative, text, counts, path_tokens in documents:
            overlap = query_tokens & set(counts)
            if not overlap:
                continue
            length = sum(counts.values())
            normalisation = BM25_K1 * (1 - BM25_B + BM25_B * length / max(1.0, average_length))
            score = 0.0
            for token in overlap:
                frequency = document_frequency[token]
                idf = math.log(1 + (total - frequency + 0.5) / (frequency + 0.5))
                occurrences = counts[token]
                weight = PATH_WEIGHT if token in path_tokens else 1.0
                score += idf * weight * occurrences * (BM25_K1 + 1) / (occurrences + normalisation)
            ranked.append(
                {
                    "path": relative,
                    "score": round(score, 4),
                    "excerpt": self._excerpt(text, query_tokens),
                }
            )
        ranked.sort(key=lambda item: (-item["score"], item["path"]))
        return ranked[: max(1, min(limit, 10))]

    @staticmethod
    def render(matches: list[dict[str, Any]]) -> str:
        """Render matches for the cached prompt prefix; identical matches render identically."""
        if not matches:
            return "(no matching project files)"
        blocks = [f"--- {match['path']}\n{match['excerpt']}" for match in matches]
        return "\n\n".join(blocks)
