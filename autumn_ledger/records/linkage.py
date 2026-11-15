import json
import uuid

from ..errors import conflict, missing, require
from ..values import canonical, fingerprint, identifier, integer, page_size, text, utc_now

MEMBER_LIMIT = 10
DIFF_FIELDS = ("state", "origin", "destination", "travel_day", "family",
               "adults", "children", "gross_cents", "refund_cents")
GROUP_STATUSES = {"pending", "adopted", "review", "dissolved"}
EVIDENCE_STATUSES = {"accepted", "duplicate", "held", "rejected"}


def normalize_members(members):
    require(isinstance(members, list) and 2 <= len(members) <= MEMBER_LIMIT,
            "invalid_members", "关联证据需要2至10笔来源内订单")
    result = []
    sources = set()
    pairs = set()
    for item in members:
        require(isinstance(item, dict), "invalid_members", "证据成员必须是对象")
        unknown = sorted(set(item) - {"source", "order_id"})
        require(not unknown, "unknown_fields", "证据成员存在未知字段", details={"fields": unknown})
        require("source" in item and "order_id" in item, "missing_fields", "证据成员缺少来源或订单号")
        source = identifier(item["source"], "source")
        order_id = identifier(item["order_id"], "order_id")
        require(source not in sources, "source_repeated", "一个销售组每个来源最多一笔订单",
                details={"source": source})
        require((source, order_id) not in pairs, "duplicate_id", "证据成员存在重复订单")
        sources.add(source)
        pairs.add((source, order_id))
        result.append({"source": source, "order_id": order_id})
    return sorted(result, key=lambda item: (item["source"], item["order_id"]))


def member_differences(heads):
    """比较各成员当前订单头的关键字段，只列出存在分歧的字段；不做任何字段合并。"""
    result = []
    for field in DIFF_FIELDS:
        values = [{"source": head["source"], "order_id": head["order_id"],
                   "sequence": head["sequence"], "value": head[field]} for head in heads]
        if len({canonical(item["value"]) for item in values}) > 1:
            result.append({"field": field, "values": values})
    return result


class Linkage:
    """跨来源同一销售的核对关系：关联证据、销售组与采信决定。

    所有状态变化都写入审计流并推进数据版本，因此任意历史版本都能回放当时的
    组、决定与订单修订；拆开或撤销后重新计算即可恢复，已生成的分析不受影响。
    """

    def __init__(self, database, intake):
        self.database = database
        self.intake = intake

    # ---------- 内部读取 ----------

    def _head(self, connection, source, order_id, revision=None):
        clause = "SELECT * FROM events WHERE source=? AND order_id=?"
        parameters = [source, order_id]
        if revision is not None:
            clause += " AND revision<=?"
            parameters.append(revision)
        row = connection.execute(clause + " ORDER BY sequence DESC LIMIT 1", parameters).fetchone()
        if row is None:
            return None
        return {**json.loads(row["payload"]), "source": source,
                "ingested_revision": row["revision"], "digest": row["digest"]}

    def _require_orders(self, connection, members):
        for member in members:
            key = f"{member['source']}:{member['order_id']}"
            require(self._head(connection, member["source"], member["order_id"]) is not None,
                    "not_found", f"找不到订单：{key}", 404)

    def _active_member_index(self, connection, revision=None):
        """(source, order_id) -> 组id，仅包含快照版本下未拆开的组。"""
        revision = self.database.snapshot_revision(connection, revision)
        rows = connection.execute(
            "SELECT m.source,m.order_id,m.group_id FROM sale_group_members m "
            "JOIN sale_groups g ON g.id=m.group_id "
            "WHERE g.created_revision<=? AND (g.dissolved_revision IS NULL OR g.dissolved_revision>?) "
            "ORDER BY m.group_id,m.source",
            (revision, revision),
        ).fetchall()
        return {(row["source"], row["order_id"]): row["group_id"] for row in rows}

    def _group_row(self, connection, group_id):
        row = connection.execute("SELECT * FROM sale_groups WHERE id=?", (group_id,)).fetchone()
        if row is None:
            raise missing("销售组", group_id)
        return row

    def _members_of(self, connection, group_id):
        rows = connection.execute(
            "SELECT source,order_id FROM sale_group_members WHERE group_id=? ORDER BY source", (group_id,)
        ).fetchall()
        return [{"source": row["source"], "order_id": row["order_id"]} for row in rows]

    def _decisions_of(self, connection, group_id, revision=None):
        clause = "SELECT * FROM adoption_decisions WHERE group_id=?"
        parameters = [group_id]
        if revision is not None:
            clause += " AND revision<=?"
            parameters.append(revision)
        rows = connection.execute(clause + " ORDER BY revision,id", parameters).fetchall()
        return [dict(row) for row in rows]

    def _effective_decision(self, connection, group_id, revision):
        decisions = self._decisions_of(connection, group_id, revision)
        return decisions[-1] if decisions else None

    def _status(self, connection, revision, group_row, decision):
        if group_row["dissolved_revision"] is not None and group_row["dissolved_revision"] <= revision:
            return "dissolved"
        if decision is None or decision["action"] == "revoke":
            return "pending"
        head = self._head(connection, decision["source"], decision["order_id"], revision)
        if head is None or head["sequence"] != decision["sequence"]:
            return "review"
        return "adopted"

    def _group_dict(self, connection, revision, row, with_heads=True):
        group_id = row["id"]
        decision = self._effective_decision(connection, group_id, revision)
        status = self._status(connection, revision, row, decision)
        members = []
        heads = []
        for member in self._members_of(connection, group_id):
            head = self._head(connection, member["source"], member["order_id"], revision) if with_heads else None
            if head is not None:
                heads.append(head)
            members.append({**member,
                            "head_sequence": head["sequence"] if head else None,
                            "head_digest": head["digest"] if head else None})
        result = {"id": group_id, "evidence_id": row["evidence_id"], "status": status,
                  "created_revision": row["created_revision"], "dissolved_revision": row["dissolved_revision"],
                  "created_at": row["created_at"], "members": members,
                  "decision": self._decision_dict(decision), "revision": revision}
        if with_heads and status != "dissolved":
            result["differences"] = member_differences(heads)
        return result

    @staticmethod
    def _decision_dict(row):
        if row is None:
            return None
        return {"action": row["action"], "source": row["source"], "order_id": row["order_id"],
                "sequence": row["sequence"], "head_digest": row["head_digest"],
                "revision": row["revision"], "actor": row["actor"],
                "note": row["note"], "created_at": row["created_at"]}

    @staticmethod
    def _evidence_dict(row, revision=None):
        status = row["status"]
        settled = row["settled_revision"]
        group_id = row["group_id"]
        if revision is not None and settled is not None and settled > revision:
            # 快照版本时证据尚未处理：状态、处理版本与组建档都按当时呈现
            status, settled, group_id = "held", None, None
        return {"id": row["id"], "status": status, "group_id": group_id,
                "members": json.loads(row["members"]), "revision": row["revision"],
                "settled_revision": settled, "actor": row["actor"],
                "note": row["note"], "created_at": row["created_at"]}

    def _create_group(self, connection, evidence_id, members, actor, note, created):
        group_id = "grp-" + uuid.uuid4().hex
        revision = self.database.advance(
            connection, "linkage.group", group_id, actor,
            {"evidence_id": evidence_id, "members": members, "note": note})
        connection.execute("INSERT INTO sale_groups VALUES (?,?,?,?,?)",
                           (group_id, evidence_id, revision, None, created))
        for member in members:
            connection.execute("INSERT INTO sale_group_members VALUES (?,?,?)",
                               (group_id, member["source"], member["order_id"]))
        return group_id, revision

    # ---------- 证据提交与处理 ----------

    def submit_evidence(self, evidence_id, members, note="", actor="operator"):
        identifier(evidence_id, "evidence_id")
        normalized = normalize_members(members)
        note = text(note or "同一销售关联证据", "证据说明", 500)
        actor = text(actor, "操作人", 100)
        digest = fingerprint(normalized)
        with self.database.transaction(write=True) as connection:
            existing = connection.execute("SELECT * FROM link_evidence WHERE id=?", (evidence_id,)).fetchone()
            if existing:
                if existing["digest"] != digest:
                    raise conflict("证据号已用于不同内容")
                return {**self._evidence_dict(existing), "replayed": True}
            self._require_orders(connection, normalized)
            active = self._active_member_index(connection)
            overlapped = {active[(m["source"], m["order_id"])]
                          for m in normalized if (m["source"], m["order_id"]) in active}
            created = utc_now()
            status, group_id, revision = "held", None, None
            if not overlapped:
                group_id, revision = self._create_group(connection, evidence_id, normalized, actor, note, created)
                status = "accepted"
            else:
                same_group = len(overlapped) == 1
                if same_group:
                    candidate = next(iter(overlapped))
                    current = {(m["source"], m["order_id"]) for m in self._members_of(connection, candidate)}
                    incoming = {(m["source"], m["order_id"]) for m in normalized}
                    if incoming <= current:
                        status, group_id = "duplicate", candidate
                revision = self.database.advance(
                    connection, "linkage.evidence", evidence_id, actor,
                    {"members": normalized, "status": status, "note": note,
                     "conflicts_with": sorted(overlapped) if status == "held" else []})
            connection.execute("INSERT INTO link_evidence VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (evidence_id, digest, revision, status, None, group_id,
                                canonical(normalized), actor, note, created))
            return {**self._evidence_dict(connection.execute(
                "SELECT * FROM link_evidence WHERE id=?", (evidence_id,)).fetchone()), "replayed": False}

    def settle(self, evidence_id, action, actor="operator", note=""):
        identifier(evidence_id, "evidence_id")
        require(action in {"accept", "reject"}, "invalid_action", "证据处理只支持accept/reject")
        actor = text(actor, "操作人", 100)
        note = text(note or "处理留待证据", "处理说明", 500)
        with self.database.transaction(write=True) as connection:
            row = connection.execute("SELECT * FROM link_evidence WHERE id=?", (evidence_id,)).fetchone()
            if row is None:
                raise missing("关联证据", evidence_id)
            require(row["status"] == "held", "invalid_transition", "仅留待处理的证据可以处理", 409)
            members = json.loads(row["members"])
            group_id = None
            if action == "accept":
                active = self._active_member_index(connection)
                conflicts = sorted({active[(m["source"], m["order_id"])]
                                    for m in members if (m["source"], m["order_id"]) in active})
                require(not conflicts, "conflict", "关联仍与现有销售组冲突", 409,
                        {"groups": conflicts})
                group_id, revision = self._create_group(connection, evidence_id, members, actor, note, utc_now())
                status = "accepted"
            else:
                revision = self.database.advance(
                    connection, "linkage.settle", evidence_id, actor,
                    {"action": action, "note": note})
                status = "rejected"
            connection.execute("UPDATE link_evidence SET status=?,settled_revision=?,group_id=? WHERE id=?",
                               (status, revision, group_id, evidence_id))
            return self.get_evidence(evidence_id, connection=connection)

    # ---------- 采信、撤销、拆开 ----------

    def _require_active(self, connection, group_id):
        row = self._group_row(connection, group_id)
        require(row["dissolved_revision"] is None, "invalid_transition", "销售组已拆开，不能再操作", 409)
        return row

    def adopt(self, group_id, source, actor="operator", note="", expected_sequence=None):
        identifier(group_id, "group_id")
        identifier(source, "source")
        actor = text(actor, "核对人", 100)
        note = text(note or "采信该来源完整修订", "采信说明", 500)
        if expected_sequence is not None:
            integer(expected_sequence, "expected_sequence", 1)
        with self.database.transaction(write=True) as connection:
            self._require_active(connection, group_id)
            members = self._members_of(connection, group_id)
            member = next((item for item in members if item["source"] == source), None)
            require(member is not None, "not_a_member", "该来源不是销售组成员，不能采信", 409,
                    {"members": members})
            head = self._head(connection, source, member["order_id"])
            if expected_sequence is not None:
                require(head["sequence"] == expected_sequence, "conflict",
                        "订单头已变化，请重新核对后再采信", 409,
                        {"current_sequence": head["sequence"]})
            revision = self.database.advance(
                connection, "linkage.adopt", group_id, actor,
                {"source": source, "order_id": member["order_id"],
                 "sequence": head["sequence"], "head_digest": head["digest"], "note": note})
            connection.execute("INSERT INTO adoption_decisions VALUES (NULL,?,?,?,?,?,?,?,?,?,?)",
                               (group_id, revision, "adopt", source, member["order_id"],
                                head["sequence"], head["digest"], actor, note, utc_now()))
            return self.get_group(group_id, connection=connection)

    def revoke(self, group_id, actor="operator", note=""):
        identifier(group_id, "group_id")
        actor = text(actor, "核对人", 100)
        note = text(note or "撤销采信", "撤销说明", 500)
        with self.database.transaction(write=True) as connection:
            self._require_active(connection, group_id)
            revision_now = self.database.revision(connection)
            decision = self._effective_decision(connection, group_id, revision_now)
            require(decision is not None and decision["action"] == "adopt",
                    "no_active_adoption", "当前没有生效的采信决定", 409)
            revision = self.database.advance(
                connection, "linkage.revoke", group_id, actor,
                {"previous": self._decision_dict(decision), "note": note})
            connection.execute("INSERT INTO adoption_decisions VALUES (NULL,?,?,?,?,?,?,?,?,?,?)",
                               (group_id, revision, "revoke", None, None, None, None,
                                actor, note, utc_now()))
            return self.get_group(group_id, connection=connection)

    def dissolve(self, group_id, actor="operator", note=""):
        identifier(group_id, "group_id")
        actor = text(actor, "核对人", 100)
        note = text(note or "拆开错误关联", "拆开说明", 500)
        with self.database.transaction(write=True) as connection:
            self._require_active(connection, group_id)
            revision = self.database.advance(
                connection, "linkage.dissolve", group_id, actor, {"note": note})
            connection.execute("UPDATE sale_groups SET dissolved_revision=? WHERE id=?",
                               (revision, group_id))
            return self.get_group(group_id, connection=connection)

    # ---------- 查询 ----------

    def get_group(self, group_id, revision=None, connection=None):
        identifier(group_id, "group_id")
        if connection is None:
            with self.database.transaction() as opened:
                return self.get_group(group_id, revision, opened)
        revision = self.database.snapshot_revision(connection, revision)
        row = self._group_row(connection, group_id)
        require(row["created_revision"] <= revision, "not_found", f"找不到销售组：{group_id}", 404)
        return self._group_dict(connection, revision, row)

    def list_groups(self, status=None, revision=None, limit=100, offset=0):
        page_size(limit, offset)
        if status is not None:
            require(status in GROUP_STATUSES, "invalid_state", "销售组状态无效")
        with self.database.transaction() as connection:
            revision = self.database.snapshot_revision(connection, revision)
            rows = connection.execute(
                "SELECT * FROM sale_groups WHERE created_revision<=? ORDER BY created_revision,id", (revision,)
            ).fetchall()
            groups = [self._group_dict(connection, revision, row, with_heads=False) for row in rows]
            if status is not None:
                groups = [group for group in groups if group["status"] == status]
            return {"revision": revision, "total": len(groups),
                    "items": groups[offset:offset + limit]}

    def group_history(self, group_id):
        identifier(group_id, "group_id")
        with self.database.transaction() as connection:
            row = self._group_row(connection, group_id)
            decisions = self._decisions_of(connection, group_id)
            evidence = connection.execute(
                "SELECT * FROM link_evidence WHERE group_id=? OR id=? ORDER BY revision",
                (group_id, row["evidence_id"])).fetchall()
        return {"id": group_id, "evidence_id": row["evidence_id"],
                "created_revision": row["created_revision"],
                "dissolved_revision": row["dissolved_revision"],
                "decisions": [self._decision_dict(item) for item in decisions],
                "evidence": [self._evidence_dict(item) for item in evidence]}

    def get_evidence(self, evidence_id, connection=None):
        identifier(evidence_id, "evidence_id")
        if connection is None:
            with self.database.transaction() as opened:
                return self.get_evidence(evidence_id, opened)
        row = connection.execute("SELECT * FROM link_evidence WHERE id=?", (evidence_id,)).fetchone()
        if row is None:
            raise missing("关联证据", evidence_id)
        return self._evidence_dict(row)

    def list_evidence(self, status=None, limit=100, offset=0):
        page_size(limit, offset)
        if status is not None:
            require(status in EVIDENCE_STATUSES, "invalid_state", "证据状态无效")
        clause = " WHERE status=?" if status else ""
        parameters = [status] if status else []
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM link_evidence" + clause + " ORDER BY revision,id LIMIT ? OFFSET ?",
                parameters + [limit, offset]).fetchall()
        return [self._evidence_dict(row) for row in rows]

    # ---------- 分析引擎使用的快照状态 ----------

    def state_at(self, revision, connection):
        """快照版本下的有效核对状态：未拆开的组、生效决定与推导状态。"""
        rows = connection.execute(
            "SELECT * FROM sale_groups WHERE created_revision<=? AND "
            "(dissolved_revision IS NULL OR dissolved_revision>?) ORDER BY created_revision,id",
            (revision, revision)).fetchall()
        groups = []
        for row in rows:
            decision = self._effective_decision(connection, row["id"], revision)
            groups.append({"id": row["id"], "evidence_id": row["evidence_id"],
                           "created_revision": row["created_revision"],
                           "members": self._members_of(connection, row["id"]),
                           "decision": self._decision_dict(decision),
                           "status": self._status(connection, revision, row,
                                                  dict(decision) if decision else None)})
        return groups

    def member_heads(self, connection, revision, groups):
        """组内全部成员在快照版本下的订单头，含本次请求来源之外的成员。"""
        result = {}
        for group in groups:
            for member in group["members"]:
                key = (member["source"], member["order_id"])
                if key not in result:
                    head = self._head(connection, member["source"], member["order_id"], revision)
                    if head is not None:
                        result[key] = head
        return result

    def held_evidence(self, connection, revision):
        rows = connection.execute(
            "SELECT * FROM link_evidence WHERE revision<=? ORDER BY revision,id", (revision,)).fetchall()
        result = []
        for row in rows:
            item = self._evidence_dict(row, revision)
            if item["status"] == "held":
                result.append(item)
        return result
