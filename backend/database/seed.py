"""Fictional demonstration data for each regional node.

All names, roads, incidents and units are invented for the FA-1 demo.

Response-unit rule used by the matching service:
    High severity   -> 2 units
    Medium severity -> 1 unit
    Low severity    -> 0 units (monitoring only)

Starting capacity (units marked AVAILABLE):
    Pune   : PT-02              (PT-01 already dispatched to INC-1001)
    Mumbai : MT-03              (MT-01 lent to Pune for INC-1001, MT-02 on INC-1002)
    Nashik : NT-01, NT-02
"""

SEED_DATA = {
    "PUNE-TR-01": {
        "meta": {"node_id": "PUNE-TR-01", "node_name": "Pune Traffic Node", "region": "Pune"},
        "units": [
            {"unit_id": "PT-01", "unit_type": "Highway Patrol", "status": "DISPATCHED",
             "assigned_incident": "INC-1001"},
            {"unit_id": "PT-02", "unit_type": "Recovery Van", "status": "AVAILABLE"},
        ],
        "incidents": [
            {"incident_id": "INC-1001", "type": "Accident", "location": "Pune - Katraj Bypass",
             "severity": "High", "description": "Two-truck collision, left lane blocked",
             "detected_by": "PUNE-TR-01", "handled_by": "PUNE-TR-01", "status": "UNITS_ASSIGNED",
             "assigned_units": [{"unit_id": "PT-01", "node_id": "PUNE-TR-01"},
                                {"unit_id": "MT-01", "node_id": "MUM-TR-01"}],
             "created_at": "2026-09-01T08:15:00"},
            {"incident_id": "INC-1002", "type": "Road Blockage", "location": "Mumbai - Sion Flyover",
             "severity": "Medium", "description": "Fallen hoarding across two lanes",
             "detected_by": "MUM-TR-01", "handled_by": "MUM-TR-01", "status": "UNITS_ASSIGNED",
             "assigned_units": [{"unit_id": "MT-02", "node_id": "MUM-TR-01"}],
             "origin": "REMOTE", "created_at": "2026-09-01T08:40:00"},
        ],
        "traffic": {"Katraj Bypass": 82.0, "Hinjewadi Phase 1": 74.0, "Nagar Road": 66.0},
    },
    "MUM-TR-01": {
        "meta": {"node_id": "MUM-TR-01", "node_name": "Mumbai Traffic Node", "region": "Mumbai"},
        "units": [
            {"unit_id": "MT-01", "unit_type": "Highway Patrol", "status": "DISPATCHED",
             "assigned_incident": "INC-1001"},
            {"unit_id": "MT-02", "unit_type": "Signal Crew", "status": "DISPATCHED",
             "assigned_incident": "INC-1002"},
            {"unit_id": "MT-03", "unit_type": "Traffic Ambulance", "status": "AVAILABLE"},
        ],
        "incidents": [
            {"incident_id": "INC-1002", "type": "Road Blockage", "location": "Mumbai - Sion Flyover",
             "severity": "Medium", "description": "Fallen hoarding across two lanes",
             "detected_by": "MUM-TR-01", "handled_by": "MUM-TR-01", "status": "UNITS_ASSIGNED",
             "assigned_units": [{"unit_id": "MT-02", "node_id": "MUM-TR-01"}],
             "created_at": "2026-09-01T08:40:00"},
            {"incident_id": "INC-1001", "type": "Accident", "location": "Pune - Katraj Bypass",
             "severity": "High", "description": "Two-truck collision, left lane blocked",
             "detected_by": "PUNE-TR-01", "handled_by": "PUNE-TR-01", "status": "UNITS_ASSIGNED",
             "assigned_units": [{"unit_id": "PT-01", "node_id": "PUNE-TR-01"},
                                {"unit_id": "MT-01", "node_id": "MUM-TR-01"}],
             "origin": "REMOTE", "created_at": "2026-09-01T08:15:00"},
        ],
        "traffic": {"Western Express Hwy": 71.0, "Sion-Panvel Hwy": 64.0, "Eastern Freeway": 48.0},
    },
    "NSK-TR-01": {
        "meta": {"node_id": "NSK-TR-01", "node_name": "Nashik Traffic Node", "region": "Nashik"},
        "units": [
            {"unit_id": "NT-01", "unit_type": "Highway Patrol", "status": "AVAILABLE"},
            {"unit_id": "NT-02", "unit_type": "Recovery Van", "status": "AVAILABLE"},
        ],
        "incidents": [
            {"incident_id": "INC-1003", "type": "Heavy Congestion", "location": "Nashik - Dwarka Circle",
             "severity": "Low", "description": "Slow-moving traffic after market hours",
             "detected_by": "NSK-TR-01", "handled_by": "NSK-TR-01", "status": "MONITORING",
             "assigned_units": [], "created_at": "2026-09-01T09:05:00"},
        ],
        "traffic": {"Dwarka Circle": 52.0, "Mumbai Naka": 41.0, "Gangapur Road": 33.0},
    },
}
