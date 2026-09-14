"""Resident-first model selection (shared design with news-scan/resident.py).

The GPU holds one Gemma at a time, and Ollama reloads the runner on ANY
mismatch of model or num_ctx (measured 2026-09-09: same model with a SMALLER
num_ctx still triggered a full reload). When another tenant, the red-recomp
chain, already has a model loaded, adopting that model AND its exact context
size avoids the eviction ping-pong that made scoring ~13x slower (150-250
evictions per morning, 9 s + 28 s per swap cycle; 38-50 s/posting instead of
3 s). Nothing loaded, or nothing from an allowed family, means the configured
default applies.
"""
from __future__ import annotations

import logging

import requests

from .config import OllamaConfig

log = logging.getLogger(__name__)


def _family(name: str) -> str:
    return name.split(":", 1)[0].lower()


def resident_runner(host: str, families: list[str], timeout: int = 5) -> tuple[str, int] | None:
    """(model, context_length) of the largest loaded model from an allowed
    family, or None. Embedding models are never candidates."""
    try:
        models = requests.get(f"{host}/api/ps", timeout=timeout).json().get("models") or []
    except (requests.RequestException, ValueError):
        return None
    fams = {f.lower() for f in families}
    cands = [m for m in models
             if m.get("name") and _family(m["name"]) in fams and "embed" not in m["name"].lower()]
    if not cands:
        return None
    best = max(cands, key=lambda m: int(m.get("size_vram") or 0))
    return best["name"], int(best.get("context_length") or 0)


def apply_resident(cfg: OllamaConfig) -> bool:
    """Mutate cfg to match the resident runner. Returns True if adopted.

    Adopts only when the loaded context is at least the configured one: a
    smaller loaded context would truncate the resume + posting prompt, and
    matching it would not avoid a reload anyway."""
    if not cfg.prefer_resident:
        return False
    res = resident_runner(cfg.host, cfg.resident_families)
    if res is None:
        return False
    name, ctx = res
    if ctx and ctx < cfg.num_ctx:
        log.warning("resident %s has num_ctx %d < needed %d; keeping default %s",
                    name, ctx, cfg.num_ctx, cfg.model)
        return False
    if name == cfg.model and (not ctx or ctx == cfg.num_ctx):
        return False
    log.info("resident-first: adopting %s @ num_ctx %d (default was %s @ %d)",
             name, ctx or cfg.num_ctx, cfg.model, cfg.num_ctx)
    cfg.model = name
    if ctx:
        cfg.num_ctx = ctx
    return True
