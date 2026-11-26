"""Independent artifact settlement after the official execution tree stops."""
from .service import AppService


def reconcile_delivery(store, id, *, controller_outcome=None):
    """Settle a verified artifact independently of the model's final message.

    Never replay edits/deployments or accept a stale version. This also makes a
    resume after a controller interruption reuse the already accepted candidate.
    """
    row = store.get(id)
    if row.status != "running":
        return False
    if not (row.state.get("verification") or {}).get("passed"):
        # After the owned tree stops, acceptance is a deterministic domain
        # operation, not another reasoning turn. Never accept an old candidate
        # for new operator feedback or run this before a controller activation.
        if not controller_outcome or controller_outcome.get("status") == "resume_recheck":
            return False
        candidate = row.state.get("candidate") or {}
        feedback = (row.state.get("feedback") or [{}])[-1]
        if not candidate or not row.state.get("deployment") or candidate.get("id") == feedback.get("candidate"):
            return False
        verified = AppService(store, id, "supervisor").call("verify", {})
        if not verified["ok"] or not (verified.get("result") or {}).get("passed"):
            return False
        store.event(id, "system", "verification_reconciled", {
            "candidate": candidate["id"], "controller_outcome": controller_outcome})
    result = AppService(store, id, "supervisor").call("finish", {})
    if result["ok"]:
        store.event(
            id,
            "system",
            "delivery_reconciled",
            {"candidate": row.state["candidate"]["id"], "controller_outcome": controller_outcome},
        )
    return result["ok"]
