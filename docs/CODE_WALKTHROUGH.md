# Code Walkthrough (for the viva)

The whole backend is ~900 lines of plain Python. Read the files in this order.

## 1. `backend/config.py` — who the nodes are
A dictionary `NODES` with three entries. Each has a **name**, **region**, **port**, **db file** and a
**peers** list (nearest first). `address(node_id)` turns an id into `host:port` — this is the
*name vs address* separation: code always uses the id, only this function knows the location.

## 2. `proto/traffic.proto` — the RPC contract
One gRPC service `TrafficNode` with 13 procedures. `tools/gen_proto.py` generates
`backend/rpc/traffic_pb2.py` (message classes) and `traffic_pb2_grpc.py` (client stub + server base class).

## 3. `backend/database/db.py` — one SQLite file per node
Class `NodeDatabase` with small methods: `insert_incident`, `list_incidents`, `reserve_unit`, `release_unit`,
`tick_traffic`, `reset`. `reserve_unit` is one `UPDATE … WHERE status='AVAILABLE'` so a unit can never be
booked twice. `seed.py` holds the fictional demo data.

## 4. `backend/node.py` — a regional node (one process)
Class `TrafficNodeServicer` implements every RPC from the proto. Each method starts with
`await self._check_up(context)`: if the node is in *simulated failure* it aborts with `UNAVAILABLE`.
- `CreateIncident` → store locally, publish `INCIDENT_CREATED` to the broker
- `RequestResponseSupport` → `MatchingService.find_units` (local, then P2P)
- `SearchResponseUnits` / `ReserveUnit` / `ReleaseUnit` → answered for **peers** (P2P server side)
- `NotifyIncident` → store a REMOTE copy of another node's incident
- `SetAvailability` → the only RPC that is *not* guarded; used by Fail/Recover buttons
`serve()` starts the gRPC server on the node's port and a background `traffic_ticker`.

## 5. `backend/services/matching_service.py` — finding response units
`find_units(inc)`: how many units are needed (`High=2, Medium=1, Low=0`) → reserve local free units →
for each peer (nearest first) call `PeerClient.search_units` then `reserve_unit` until satisfied →
update status (`UNITS_ASSIGNED` / `PARTIALLY_ASSIGNED`) and publish `INCIDENT_UPDATED`.
Every step is appended to a `trace` list that the dashboard shows.

## 6. `backend/communication/p2p.py` — node talks to node
`PeerClient` owns a `StubPool` and calls the peer's gRPC procedures directly (no coordinator involved).
It also publishes `RESPONSE_REQUESTED` / `RESPONSE_AVAILABLE` so the exchange appears in the log.

## 7. `backend/rpc/client.py` — making RPC calls
`StubPool.call(node_id, "MethodName", request)` resolves the address, keeps one channel per node and
turns any gRPC failure into a `NodeUnavailable` exception — callers then just try the next node.

## 8. `backend/communication/messaging.py` — the message broker
`MessageBroker.publish()` stores the message and puts it on an `asyncio.Queue`; a worker task delivers
each message to all `subscribers` (the WebSocket hub and the coordinator). Nodes are separate processes,
so they publish via `BrokerPublisher`, which POSTs to `/internal/events`.

## 9. `backend/services/fault_tolerance.py` — heartbeats
`HealthMonitor._loop()` calls `GetNodeStatus` on each node every 2 s. No answer → `mark_offline`
(publishes `NODE_FAILED`). Answer after being offline → publishes `NODE_RECOVERED`.

## 10. `backend/coordinator.py` — the middleware
- `report_incident()`: allocate `INC-xxxx`; loop over `failover_order()` — skip OFFLINE nodes, call
  `CreateIncident`, on failure mark offline and continue; publish `REQUEST_REDIRECTED` if a different node
  served it; then call `RequestResponseSupport`.
- `on_broker_message()`: on `INCIDENT_CREATED` broadcast to other nodes via `NotifyIncident`; nodes that are
  offline get the incident appended to `pending[node]` and receive it on `NODE_RECOVERED` (store-and-forward).
- `list_incidents()`: ask every reachable node, merge, owner copy wins.
- `fail_node()` / `recover_node()`: call `SetAvailability`, then run one heartbeat immediately.

## 11. `backend/communication/websocket.py` + `backend/main.py` — gateway
`WebSocketHub.broadcast()` sends a JSON frame to every open dashboard; `handle()` serves one connection and
relays WebRTC `signal` frames to the other tab. `main.py` wires everything together, defines the REST
endpoints (`/api/nodes`, `/api/incidents`, `/api/traffic`, `/api/events`, `/fail`, `/recover`, `/api/reset`)
and runs `snapshot_stream()` every 2 s.

## 12. `frontend/app.js` — dashboard
Five sections: (1) WebSocket connect + frame handling, (2) render functions for tables/bars/log,
(3) diagram animation (a dot moves along the link for each event), (4) form + buttons calling the API,
(5) WebRTC: `rtcConnect` creates the offer, `rtcOnSignal` handles offer/answer/ICE, data channel messages
go browser-to-browser.

## Typical viva questions
- *Where is RPC?* `coordinator.py` calls `pool.call(node, "CreateIncident", req)`; the node implements it in `node.py`.
- *Where is P2P?* `matching_service.py` → `p2p.py` → the **peer's** `SearchResponseUnits` in `node.py`.
- *Where is message-oriented communication?* `messaging.py`; producers call `publish`, consumers are `subscribe`d.
- *Where is the stream?* `websocket.py` + `snapshot_stream()` in `main.py`; `app.js` `ws.onmessage`.
- *How is failure detected?* `fault_tolerance.py` heartbeats; *how is it handled?* the loop in `report_incident()`.
- *Why one DB per node?* distributed data ownership — only the owning node's process opens its file.
