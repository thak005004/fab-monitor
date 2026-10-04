"""Citation checks on a model's diagnosis (spec §10). Code only, no LLM.

Proves every citation is real, on the right tool, and not later than the
problem. Does NOT prove the reasoning is right; that is why a person decides.
"""

from __future__ import annotations

from agents.diagnosis.evidence import bundle_items
from config_loader import Config
from sim.clock import Clock


def verify(output: dict, bundle: dict, clock: Clock, config: Config) -> str | None:
    """Returns None if the diagnosis passes, else the rejection reason."""
    factors, cited = output["likely_factors"], output["cited_evidence"]
    if output["status"] == "diagnosed":
        if not factors:
            return "diagnosed with no likely contributing factors"
        if not cited:
            return "diagnosed with no cited evidence"
    elif factors:
        return "abstained but listed likely contributing factors"

    for cid in cited:
        problem = check_citation(cid, bundle, clock, config)
        if problem:
            return problem
    return None


def check_citation(cid: str, bundle: dict, clock: Clock, config: Config) -> str | None:
    """One citation against the bundle that was sent: None if it passes, else why not."""
    item = bundle_items(bundle).get(cid)
    tool = bundle["incident"]["tool_id"]
    latest_allowed = clock.shift_iso(bundle["incident"]["onset_ts"], config.onset_grace_ticks)
    if item is None:
        return f"cited {cid}, which is not in the evidence that was sent"
    if item["tool_id"] != tool:
        return f"cited {cid}, which belongs to {item['tool_id']}, not {tool}"
    if item["kind"] in ("maintenance", "recipe_changes") and item["ts"] > latest_allowed:
        return f"cited {cid} at {item['ts']}, after the onset ({bundle['incident']['onset_ts']}) plus grace"
    return None
