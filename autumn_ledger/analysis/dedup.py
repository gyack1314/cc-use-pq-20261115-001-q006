from collections import defaultdict

from ..records.contracts import contribution
from ..records.linkage import member_differences
from ..values import fingerprint
from .cubes import MEASURES, add_measures, empty_measures, enrich

SALE_KEYS = ("sale_basis", "sale_group", "adoption_revision")


def base_order(sale):
    """去重记录对应的来源原始订单头；去重口径不拼接不同来源的字段。"""
    return {key: value for key, value in sale.items() if key not in SALE_KEYS}


def sale_lineage_entry(period, sale):
    return {"period": period, "source": sale["source"], "order_id": sale["order_id"],
            "event_id": sale["event_id"], "sequence": sale["sequence"],
            "revision": sale["ingested_revision"], "digest": fingerprint(base_order(sale)),
            "basis": sale["sale_basis"], "sale_group": sale["sale_group"],
            "adoption_revision": sale["adoption_revision"]}


def group_snapshot(group, member_heads, request):
    members = []
    heads = []
    for member in group["members"]:
        head = member_heads.get((member["source"], member["order_id"]))
        if head is not None:
            heads.append(head)
        members.append({"source": member["source"], "order_id": member["order_id"],
                        "in_request_sources": member["source"] in request["sources"],
                        "head_sequence": head["sequence"] if head else None,
                        "head_digest": head["digest"] if head else None})
    return {"id": group["id"], "evidence_id": group["evidence_id"], "status": group["status"],
            "created_revision": group["created_revision"], "members": members,
            "decision": group["decision"], "differences": member_differences(heads),
            "counted": None, "note": None}


def exclusion(order, group, reason):
    return {"source": order["source"], "order_id": order["order_id"],
            "sequence": order["sequence"], "event_id": order["event_id"],
            "group_id": group["id"], "reason": reason}


def explain_exclusions(snapshots, excluded):
    explanations = []
    grouped = defaultdict(list)
    for item in excluded:
        grouped[(item["group_id"], item["reason"])].append(item)
    for (group_id, reason), items in sorted(grouped.items()):
        decision = snapshots[group_id]["decision"] or {}
        if reason == "duplicate_of_adopted":
            explanations.append(
                f"销售组{group_id}：{len(items)}笔成员订单与采信修订"
                f"（{decision.get('source')}:{decision.get('order_id')}第{decision.get('sequence')}版）"
                "为同一销售，去重口径不再重复计入")
        elif reason == "adopted_out_of_scope":
            explanations.append(
                f"销售组{group_id}：采信修订属于请求来源之外，"
                f"{len(items)}笔范围内成员订单不计入去重口径")
        else:
            explanations.append(
                f"销售组{group_id}：核对未解决（{snapshots[group_id]['status']}），"
                f"{len(items)}笔成员订单未计入去重口径")
    return explanations


def period_reconciliation(selected, request, groups, member_heads, held):
    """在保留来源口径的同时计算去重销售口径。

    未分组订单直接计入；已采信组只计入被采信的那份完整修订；待核对或
    采信已失效的组不计入去重口径，并留待核对人员处理。
    """
    by_member = {}
    for group in groups:
        for member in group["members"]:
            by_member[(member["source"], member["order_id"])] = group
    sales = []
    excluded = []
    snapshots = {}
    for order in selected:
        group = by_member.get((order["source"], order["order_id"]))
        if group is None:
            sales.append({**order, "sale_basis": "singleton", "sale_group": None,
                          "adoption_revision": None})
            continue
        snapshot = snapshots.setdefault(group["id"], group_snapshot(group, member_heads, request))
        decision = group["decision"]
        if group["status"] == "adopted":
            if decision["source"] == order["source"] and decision["order_id"] == order["order_id"]:
                sales.append({**order, "sale_basis": "adopted", "sale_group": group["id"],
                              "adoption_revision": decision["revision"]})
                snapshot["counted"] = {"source": order["source"], "order_id": order["order_id"],
                                       "sequence": order["sequence"]}
            else:
                reason = ("duplicate_of_adopted" if decision["source"] in request["sources"]
                          else "adopted_out_of_scope")
                excluded.append(exclusion(order, group, reason))
        else:
            excluded.append(exclusion(order, group, "unresolved_group"))
    source_measures = empty_measures()
    for order in selected:
        add_measures(source_measures, contribution(order))
    dedup_measures = empty_measures()
    for sale in sales:
        add_measures(dedup_measures, contribution(sale))
    difference = {name: source_measures[name] - dedup_measures[name] for name in MEASURES}
    selected_keys = {(order["source"], order["order_id"]) for order in selected}
    relevant_held = [item for item in held
                     if any((member["source"], member["order_id"]) in selected_keys
                            for member in item["members"])]
    unresolved = sorted(group_id for group_id, snapshot in snapshots.items()
                        if snapshot["status"] in {"pending", "review"})
    explanations = explain_exclusions(snapshots, excluded)
    if difference["orders"] or unresolved or relevant_held:
        explanations.insert(0, f"来源口径{source_measures['orders']}笔订单，"
                               f"去重口径{dedup_measures['orders']}笔销售，"
                               f"差异{difference['orders']}笔来自跨来源重复或未解决核对")
    return {"source_view": {"totals": enrich(source_measures)},
            "dedup_view": {"totals": enrich(dedup_measures), "sale_count": len(sales), "sales": sales},
            "difference": {"measures": difference, "excluded": excluded,
                           "explanations": explanations},
            "groups": sorted(snapshots.values(), key=lambda item: item["id"]),
            "unresolved_groups": unresolved,
            "held_evidence": relevant_held}
