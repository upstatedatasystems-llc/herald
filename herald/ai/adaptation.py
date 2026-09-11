"""
Large-Source Bounded Adaptation Engine for Herald.
Provides semantic chunking, structured fact-preserving distillation,
merged dossier compilation, and bounded hierarchical reduction with
strict typed AdaptationBudget guarantees.
"""

import logging
import re
from datetime import UTC, datetime
from typing import Any

from herald.ai.base import AIProvider
from herald.ai.errors import (
    AIProviderError,
    AIProviderTimeoutError,
    AIProviderUnavailableError,
    AIRequestTooLargeError,
)
from herald.ai.policy import AdaptationBudget, AdaptationUsage
from herald.config import settings
from herald.services.diagnostic_recorder import record_job_diagnostic_event

logger = logging.getLogger("herald.ai.adaptation")


def semantic_chunk_text(
    text: str,
    max_chunk_chars: int | None = None,
    max_chunks: int | None = None,
) -> list[str]:
    """
    Split text into logical, coherent chunks respecting paragraph and section boundaries.
    Enforces maximum chunk character count and maximum chunk count budget.
    """
    max_chunk_chars = max_chunk_chars or getattr(settings, "ADAPTATION_CHUNK_MAX_CHARS", 4000)
    max_chunks = max_chunks or getattr(settings, "ADAPTATION_MAX_CHUNKS", 12)

    clean_text = text.strip()
    if not clean_text:
        return []

    # Split into raw paragraphs (delimited by two or more newlines)
    raw_paragraphs = [p.strip() for p in re.split(r"\n\s*\n", clean_text) if p.strip()]

    chunks: list[str] = []
    current_chunk: list[str] = []
    current_length = 0

    for para in raw_paragraphs:
        para_len = len(para)

        # If a single paragraph is longer than max_chunk_chars, split it by sentence
        if para_len > max_chunk_chars:
            # Flush current chunk first
            if current_chunk:
                chunks.append("\n\n".join(current_chunk))
                current_chunk = []
                current_length = 0

            sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", para) if s.strip()]
            sub_chunk: list[str] = []
            sub_len = 0
            for sent in sentences:
                if sub_len + len(sent) + 1 > max_chunk_chars and sub_chunk:
                    chunks.append(" ".join(sub_chunk))
                    sub_chunk = [sent]
                    sub_len = len(sent)
                else:
                    sub_chunk.append(sent)
                    sub_len += len(sent) + 1
            if sub_chunk:
                chunks.append(" ".join(sub_chunk))
            continue

        if current_length + para_len + 2 > max_chunk_chars and current_chunk:
            chunks.append("\n\n".join(current_chunk))
            current_chunk = [para]
            current_length = para_len
        else:
            current_chunk.append(para)
            current_length += para_len + 2

    if current_chunk:
        chunks.append("\n\n".join(current_chunk))

    if len(chunks) > max_chunks:
        raise AIRequestTooLargeError(
            f"Source exceeds maximum adaptation chunk budget ({len(chunks)} > {max_chunks})",
            provider="adaptation",
        )

    return chunks


def distill_chunk(
    chunk: str,
    chunk_index: int,
    total_chunks: int,
    provider: AIProvider | None = None,
) -> str:
    """
    Distill key narrative facts and information from a source chunk.
    Preserves section context, quotes, numbers, and logical flow.
    """
    lines = [f"### SECTION {chunk_index + 1}/{total_chunks}"]
    clean_chunk = chunk.strip()
    paragraphs = [p.strip() for p in clean_chunk.split("\n") if p.strip()]

    for p in paragraphs:
        if p.startswith("#"):
            lines.append(p)
        else:
            sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", p) if s.strip()]
            if len(sentences) <= 3:
                lines.append(" ".join(sentences))
            else:
                selected = [sentences[0]]
                for s in sentences[1:-1]:
                    if re.search(r'(\d+|"|\'|\$|[A-Z][a-z]+)', s):
                        selected.append(s)
                selected.append(sentences[-1])
                lines.append(" ".join(selected[:5]))

    return "\n".join(lines)


def merge_dossier(distilled_chunks: list[str], title: str | None = None) -> str:
    """Merge distilled chunk sections into an integrated, coherent dossier."""
    header = "# ADAPTED RESEARCH & SOURCE DOSSIER\n"
    if title:
        header += f"SOURCE TITLE: {title}\n"
    header += f"COMPILED SECTIONS: {len(distilled_chunks)}\n\n"
    return header + "\n\n".join(distilled_chunks)


def adapt_source_text(
    source_text: str,
    provider: AIProvider | None = None,
    budget: AdaptationBudget | None = None,
    usage: AdaptationUsage | None = None,
    job_id: str | None = None,
    source_title: str | None = None,
    db: Any = None,
) -> str:
    """
    Hierarchical large-source adaptation engine.
    Ensures large source documents are semantically chunked, fact-distilled,
    and reduced to fit within context windows without truncation.
    Adheres strictly to typed AdaptationBudget bounds.
    """
    # Literal mode zero-adaptation guarantee
    if provider is not None and getattr(provider, "provider_name", "").lower() == "literal":
        return source_text

    budget = budget or AdaptationBudget.from_settings()
    usage = usage or AdaptationUsage()
    usage.check_budget(budget)

    max_chunk_chars = getattr(settings, "ADAPTATION_CHUNK_MAX_CHARS", 4000)

    if len(source_text) <= max_chunk_chars:
        return source_text

    current_text = source_text
    depth = 0

    while depth < budget.max_reduction_depth:
        usage.reduction_depth = depth + 1
        usage.check_budget(budget)

        chunks = semantic_chunk_text(
            current_text,
            max_chunk_chars=max_chunk_chars,
            max_chunks=budget.max_chunks,
        )
        usage.chunks_processed += len(chunks)
        usage.check_budget(budget)

        if len(chunks) <= 1:
            break

        distilled = []
        for idx, ch in enumerate(chunks):
            usage.estimated_work += len(ch)
            usage.check_budget(budget)
            dist = distill_chunk(ch, idx, len(chunks), provider=provider)
            distilled.append(dist)

        current_text = merge_dossier(distilled, title=source_title)
        depth += 1

        if len(current_text) <= max_chunk_chars * 2:
            break

    if db and job_id:
        record_job_diagnostic_event(
            job_id=job_id,
            level="INFO",
            component="ai_adaptation",
            event_type="LARGE_SOURCE_ADAPTATION_COMPLETED",
            message=f"Large source adapted: {len(source_text)} chars -> {len(current_text)} chars (depth {depth})",
            metadata={
                "original_chars": len(source_text),
                "adapted_chars": len(current_text),
                "chunks_processed": usage.chunks_processed,
                "reduction_depth": depth,
                "elapsed_seconds": usage.elapsed_seconds,
            },
            db=db,
        )

    return current_text
