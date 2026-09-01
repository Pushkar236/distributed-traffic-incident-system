"""Node-side incident handling + conversion between DB dicts and protobuf messages."""
from ..rpc import traffic_pb2 as pb


def incident_to_pb(inc):
    return pb.Incident(
        incident_id=inc["incident_id"], type=inc["type"], location=inc["location"],
        severity=inc["severity"], description=inc.get("description", ""),
        detected_by=inc["detected_by"], handled_by=inc["handled_by"], status=inc["status"],
        assigned_units=[pb.AssignedUnit(unit_id=u["unit_id"], node_id=u["node_id"])
                        for u in inc.get("assigned_units", [])],
        created_at=inc.get("created_at", ""), updated_at=inc.get("updated_at", ""),
        origin=inc.get("origin", "LOCAL"))


def incident_from_pb(msg):
    return {
        "incident_id": msg.incident_id, "type": msg.type, "location": msg.location,
        "severity": msg.severity, "description": msg.description,
        "detected_by": msg.detected_by, "handled_by": msg.handled_by, "status": msg.status,
        "assigned_units": [{"unit_id": u.unit_id, "node_id": u.node_id} for u in msg.assigned_units],
        "created_at": msg.created_at, "updated_at": msg.updated_at, "origin": msg.origin,
    }


def unit_to_pb(u, node_id):
    return pb.ResponseUnit(unit_id=u["unit_id"], node_id=node_id, unit_type=u["unit_type"],
                           status=u["status"], assigned_incident=u.get("assigned_incident", ""))


class IncidentService:
    def __init__(self, db, node_id):
        self.db = db
        self.node_id = node_id

    def create(self, req):
        """Store a new incident that this node owns."""
        return self.db.insert_incident({
            "incident_id": req.incident_id, "type": req.type, "location": req.location,
            "severity": req.severity, "description": req.description,
            "detected_by": req.detected_by, "handled_by": self.node_id,
            "status": "MONITORING" if req.severity == "Low" else "REPORTED",
            "origin": "LOCAL",
        })

    def store_remote_copy(self, msg):
        """Store a copy of an incident owned by another node (received by broadcast)."""
        inc = incident_from_pb(msg)
        inc["origin"] = "REMOTE"
        return self.db.insert_incident(inc)
